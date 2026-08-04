"""Is UserCard v2 + the semantic skill route actually live? — read-only.

Answers the one question you cannot answer by reading the deploy log: did the
cards recompile, do they carry claims, and is the embedding route reaching them.
Nothing here mints a card, calls an LLM, or writes a row — safe to run any time,
including with the Anthropic balance at zero.

    python -m scripts.card_status                # full check
    python -m scripts.card_status --no-model     # skip loading MiniLM (~2s, ~90MB)
    python -m scripts.card_status --since 2026-08-04   # ledger seam date

This does NOT need CARD_MATCH_ENABLED. That flag makes g() authoritative over
Claude for real user-facing decisions and is gated on calibration holdouts
(docs/CARDRACE_DESIGN.md §3.4); UserCard v2 and the semantic route already run
under CARD_MATCH_SHADOW, which is on by default.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.init_db import get_session  # noqa: E402
from app.db.models import CardMatchShadow, JobCardRow, UserCardRow  # noqa: E402
from app.matching.cards import USER_CARD_VERSION  # noqa: E402

OK, BAD, WARN = "OK  ", "FAIL", "WARN"


def _load(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _flags() -> None:
    print("== flags ==")
    for name in ("card_match_shadow", "card_match_enabled", "card_embed_enabled",
                 "card_graph_enabled"):
        print(f"  {name:22s} {getattr(settings, name, None)}")
    if settings.card_match_enabled:
        print(f"  {WARN} card_match_enabled is ON — g() is authoritative. §3.4 says "
              "this needs passing calibration holdouts first.")
    if not settings.card_match_shadow:
        print(f"  {BAD} card_match_shadow is OFF — no ledger rows are being written, "
              "so none of this can be measured.")


def _embedding(skip: bool) -> bool:
    """Is the semantic route reachable in THIS container? The route degrades to
    a silent no-op without the ML stack, which is exactly the failure that would
    look like 'the change did nothing'."""
    print("\n== semantic route ==")
    if not settings.card_embed_enabled:
        print(f"  {WARN} CARD_EMBED_ENABLED=0 — route off by configuration.")
        return False
    if skip:
        print("  (skipped: --no-model)")
        return False
    from app.matching import skill_graph as sg
    model = sg._encoder()
    if model is None:
        print(f"  {BAD} no embedding model in this process — the route is inert. "
              "Expected in CI (ML stack stripped); in prod it means torch / "
              "sentence-transformers or the MiniLM weights are missing.")
        return False
    sg.embed_prewarm(["container orchestration",
                      "managed kubernetes workloads and ci/cd pipelines on gcp"])
    a = sg._embed_vec("container orchestration")
    b = sg._embed_vec("managed kubernetes workloads and ci/cd pipelines on gcp")
    if a is None or b is None:
        print(f"  {BAD} model loaded but encoding failed.")
        return False
    sim = float(a @ b)
    verdict = OK if sim >= sg.EMBED_FLOOR else WARN
    print(f"  {OK} model reachable via matcher._get_embed_model()")
    print(f"  {verdict} sanity cosine (no shared token, should clear the floor): "
          f"{sim:.3f}  [floor {sg.EMBED_FLOOR}, full {sg.EMBED_FULL}]")
    return True


def _cards() -> list[tuple[str, dict]]:
    print(f"\n== user cards (current schema: v{USER_CARD_VERSION}) ==")
    from app.matching.card_match import _claim_map
    with get_session() as s:
        rows = list(s.exec(select(UserCardRow).order_by(UserCardRow.user_id)))
    if not rows:
        print("  no user_card rows yet — nothing has been scored since the deploy.")
        return []
    live = []
    for r in rows:
        card = _load(r.payload)
        claims = _claim_map(card)
        skills = len(card.get("skills") or [])
        if r.version < USER_CARD_VERSION:
            tag, why = WARN, "v1 — not recompiled yet (happens on this user's next score)"
        elif not claims:
            tag, why = BAD, "v2 but ZERO claims — the mint ignored the evidence list"
        else:
            tag, why = OK, f"{len(claims)} claims, mean strength " \
                           f"{sum(claims.values()) / len(claims):.2f}"
        print(f"  {tag} {r.user_id[:24]:24s} v{r.version}  skills={skills:<3d} {why}")
        print(f"       updated {r.updated_at:%Y-%m-%d %H:%M}  model={r.model}")
        for c in list(claims)[:3]:
            print(f"       · {c[:88]}")
        if claims:
            live.append((r.user_id, card))
    return live


def _probe(live: list[tuple[str, dict]]) -> None:
    """Score a real card pair and show whether the route moved anything.

    `spread` is the tell: the semantic route is gated on use_inference, so any
    credit it pays lands in expanded and nowhere else."""
    print("\n== live pair probe ==")
    if not live:
        print("  skipped: no v2 card with claims to probe.")
        return
    from app.matching.card_match import match_cards
    from app.matching.skill_graph import load_graph
    with get_session() as s:
        jc_rows = list(s.exec(select(JobCardRow).order_by(
            JobCardRow.updated_at.desc()).limit(3)))
    if not jc_rows:
        print("  skipped: no job_card rows yet.")
        return
    uid, card = live[0]
    graph = load_graph() if settings.card_graph_enabled else None
    for jr in jc_rows:
        job_card = _load(jr.payload)
        if not job_card.get("capabilities"):
            continue
        res = match_cards(card, job_card, graph=graph)
        print(f"  {uid[:20]} x {jr.card_key[:34]}")
        print(f"    direct={res.direct:5.1f}  expanded={res.expanded:5.1f}  "
              f"spread={res.spread:+5.1f}")
        print(f"    skills: {res.breakdown['skills']['score']:.1f} — "
              f"{res.breakdown['skills']['note'][:100]}")
    print("  spread > 0 with a claim named in the note = the route is paying out.")


def _ledger(since: str | None) -> None:
    print("\n== shadow ledger ==")
    cut = None
    if since:
        try:
            cut = datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            print(f"  {WARN} unparseable --since {since!r}; showing all rows.")
    with get_session() as s:
        rows = list(s.exec(select(CardMatchShadow)))
    if not rows:
        print("  empty — no Claude final has been scored with shadow on yet.")
        return
    bar = settings.shortlist_score_threshold

    def stats(rs, label):
        if not rs:
            print(f"  {label:16s} (none)")
            return
        agree = sum((r.llm_score >= bar) == (r.expanded_score >= bar) for r in rs)
        sk = [_load(r.breakdown).get("skills", {}).get("score", 0.0) for r in rs]
        print(f"  {label:16s} n={len(rs):<5d} agree@{bar:.0f}="
              f"{100.0 * agree / len(rs):5.1f}%  mean spread="
              f"{sum(r.spread for r in rs) / len(rs):5.2f}  mean g() skills="
              f"{sum(sk) / len(sk):5.1f}")

    if cut:
        stats([r for r in rows if r.created_at < cut], "before cutoff")
        stats([r for r in rows if r.created_at >= cut], "after cutoff")
        print("  Rows either side of a cutoff may score different functions "
              "(docs/CARDRACE_DESIGN.md §9.2, §9.2.3) — compare only deliberately.")
        print("  NOT a UserCard v2 seam unless the 'user cards' section above "
              "shows v2 rows: a jump here with every card still v1 is the "
              "Aug-3 phrase-resolver fix, not the semantic route.")
    else:
        stats(rows, "all rows")
        print("  Pass --since <deploy date> to split this at the v2 seam.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="deploy date — splits the ledger at the UserCard v2 seam")
    ap.add_argument("--no-model", action="store_true",
                    help="skip loading MiniLM (faster; leaves the route unverified)")
    args = ap.parse_args()

    _flags()
    _embedding(args.no_model)
    live = _cards()
    _probe(live)
    _ledger(args.since)
    print("\nNothing was written. CARD_MATCH_ENABLED was not consulted and does "
          "not need to change for any of the above to work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
