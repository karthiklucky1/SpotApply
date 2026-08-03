"""Re-score g() over the EXISTING shadow ledger — accuracy today, zero spend.

A shadow row is half stale and half ground truth. When g() or the skill graph
changes, `expanded_score` describes a function that no longer exists — but
`llm_score` is Claude's real final and stays valid, and both cards were
persisted (`job_card.payload` by card_key, `user_card.payload` by user_id).
So the whole ledger can be re-scored offline: rehydrate the pair, run today's
match_cards(), and compare against the Claude score already sitting there.

This is the answer to "did the fix actually work?" without waiting for new
finals to accrue. It makes NO LLM calls, mints nothing, and writes nothing.

NOT scripts/compiler_replay.py — that fits linear programs over its own skill
vocabulary against Job.rerank_reasoning, and never touches a card.

    python -m scripts.card_match_replay                    # whole ledger
    python -m scripts.card_match_replay --since 2026-07-29
    python -m scripts.card_match_replay --verbose          # per-row movement

Read the OLD column as the number the shadow report prints today, and NEW as
what it would print if every one of those finals were re-scored now.
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
from app.matching.card_match import match_cards  # noqa: E402
from app.matching.conformal import assign_band  # noqa: E402
from app.matching.skill_graph import load_graph  # noqa: E402


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    —"


FACTORS = ("skills", "experience", "location", "work_auth")
# card_match._overall: any factor at/below BLOCKER_FLOOR caps the overall here.
BLOCKER_FLOOR, BLOCKER_CAP = 15.0, 25.0


def _factors(raw: str | None) -> dict:
    """Per-factor scores out of a stored breakdown JSON."""
    try:
        b = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return {k: v for k, v in b.items()
            if isinstance(v, dict) and isinstance(v.get("score"), (int, float))}


def _delta(old: float, new: float, better: str = "up") -> str:
    """Sign the movement so the direction is unmissable in the table."""
    d = new - old
    if abs(d) < 0.05:
        return "     ="
    good = (d > 0) if better == "up" else (d < 0)
    return f"{d:+6.1f}{'' if good else '  (worse)'}"


class Stats:
    """Both sides of the comparison scored identically, so the diff is the fix."""

    def __init__(self, bar: float):
        self.bar = bar
        self.n = 0
        self.abs_err = 0.0
        self.within10 = 0
        self.agree = 0
        self.false_in = 0
        self.false_out = 0
        self.spread_sum = 0.0
        self.spread_nonzero = 0

    def add(self, llm: float, g: float, spread: float) -> None:
        self.n += 1
        self.abs_err += abs(llm - g)
        self.within10 += 1 if abs(llm - g) <= 10 else 0
        self.agree += 1 if (llm >= self.bar) == (g >= self.bar) else 0
        self.false_in += 1 if g >= self.bar > llm else 0
        self.false_out += 1 if llm >= self.bar > g else 0
        self.spread_sum += spread
        self.spread_nonzero += 1 if abs(spread) > 0.001 else 0

    @property
    def mae(self) -> float:
        return self.abs_err / self.n if self.n else 0.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _autopsy(flips: list[dict], bar: float) -> None:
    """Why did each decision flip? Two mechanisms, opposite fixes.

    (A) SKILLS OVER-CREDIT — the new skills score alone carries the row over the
        bar. The resolver is too generous; tighten what decomposition can earn.
    (B) BLOCKER RELEASE — the old row sat at exactly BLOCKER_CAP because
        skills<=BLOCKER_FLOOR tripped the hard-blocker cap. Nothing about the
        row's merits was ever evaluated; the cap was rejecting it, and the cap
        fired because skills was broken. Tightening decomposition does NOT
        address these, and would suppress the good flips along with them.
    """
    broke = [f for f in flips if f["verdict"] == "broke"]
    fixed = [f for f in flips if f["verdict"] == "fixed"]

    def _mech(f: dict) -> str:
        old_sk = (f["old_bd"].get("skills") or {}).get("score")
        was_capped = (abs(f["old"] - BLOCKER_CAP) < 0.05
                      and old_sk is not None and old_sk <= BLOCKER_FLOOR)
        return "blocker-release" if was_capped else "skills-over-credit"

    print("\n=== flip autopsy — what actually moved these rows ===")
    for label, group in (("BROKE (away from Claude)", broke),
                         ("FIXED (toward Claude)", fixed)):
        if not group:
            continue
        mechs: dict[str, int] = {}
        for f in group:
            mechs[_mech(f)] = mechs.get(_mech(f), 0) + 1
        print(f"\n  {label}: {len(group)}")
        for m, c in sorted(mechs.items(), key=lambda kv: -kv[1]):
            print(f"    {m:20} {c:4d}  {_pct(c, len(group))}")
        for k in FACTORS:
            vals = [(f["new_bd"].get(k) or {}).get("score") for f in group]
            vals = [v for v in vals if isinstance(v, (int, float))]
            print(f"      mean NEW {k:11} {_mean(vals):5.1f}")

    if broke:
        print("\n  BROKE rows in full (old -> new per factor):")
        print(f"  {'job':>8} {'Claude':>7} {'old':>6} {'new':>6}  "
              + " ".join(f"{k[:5]:>13}" for k in FACTORS) + "   mechanism")
        for f in sorted(broke, key=lambda f: f["llm"]):
            cells = []
            for k in FACTORS:
                o = (f["old_bd"].get(k) or {}).get("score")
                n = (f["new_bd"].get(k) or {}).get("score")
                cells.append(f"{'—' if o is None else f'{o:.0f}':>5}->{'—' if n is None else f'{n:.0f}':<6}")
            print(f"  {f['job']:>8} {f['llm']:7.1f} {f['old']:6.1f} {f['new']:6.1f}  "
                  + " ".join(cells) + f"   {_mech(f)}")
        # The discriminator: is skills alone carrying them over, or is it the
        # other three factors holding a row up once the cap lets go?
        solo = [f for f in broke
                if isinstance((f["new_bd"].get("skills") or {}).get("score"), (int, float))
                and (f["new_bd"]["skills"]["score"] >= bar)]
        print(f"\n    of {len(broke)} broke rows, skills alone is over the bar in "
              f"{len(solo)} ({_pct(len(solo), len(broke))})")
        print("    high share => the resolver over-credits; low share => the other")
        print("    three factors carry them once the blocker cap releases, and the")
        print("    fix belongs in weighting or a skills gate, not in decomposition.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="ignore rows before this date")
    ap.add_argument("--limit", type=int, default=15,
                    help="how many largest movements to print (default 15)")
    ap.add_argument("--verbose", action="store_true",
                    help="print every replayed row, not just the movers")
    ap.add_argument("--flips", action="store_true",
                    help="per-factor autopsy of every row whose SHORTLIST DECISION "
                         "changed. This is the diagnostic view: it separates 'g() "
                         "now over-credits skills' from 'the blocker cap was the "
                         "only thing rejecting this row', which look identical in "
                         "the summary and need opposite fixes.")
    args = ap.parse_args()

    bar = float(settings.shortlist_score_threshold)

    with get_session() as s:
        q = select(CardMatchShadow)
        if args.since:
            try:
                q = q.where(CardMatchShadow.created_at
                            >= datetime.strptime(args.since, "%Y-%m-%d"))
            except ValueError:
                print(f"--since {args.since!r} is not YYYY-MM-DD")
                return 2
        rows = list(s.exec(q).all())
        if not rows:
            print("No shadow rows in range — nothing to replay.")
            return 1
        jobs = {r.card_key for r in rows if r.card_key}
        users = {r.user_id for r in rows if r.user_id}
        job_cards = {c.card_key: c for c in
                     s.exec(select(JobCardRow).where(JobCardRow.card_key.in_(jobs))).all()}
        user_cards = {c.user_id: c for c in
                      s.exec(select(UserCardRow).where(UserCardRow.user_id.in_(users))).all()}

    graph = load_graph() if settings.card_graph_enabled else None
    old, new = Stats(bar), Stats(bar)
    moved: list[tuple] = []
    flips: list[dict] = []
    skipped = {"no job card": 0, "no user card": 0, "unparseable payload": 0,
               "match_cards raised": 0}
    bands: dict[str, int] = {}

    for r in rows:
        jc_row = job_cards.get(r.card_key or "")
        uc_row = user_cards.get(r.user_id or "")
        if jc_row is None:
            skipped["no job card"] += 1
            continue
        if uc_row is None:
            skipped["no user card"] += 1
            continue
        try:
            job_card = json.loads(jc_row.payload or "{}")
            user_card = json.loads(uc_row.payload or "{}")
        except (ValueError, TypeError):
            skipped["unparseable payload"] += 1
            continue
        if not job_card or not user_card:
            skipped["unparseable payload"] += 1
            continue
        try:
            res = match_cards(user_card, job_card, graph=graph)
        except Exception as e:                       # a replay must not die on one row
            skipped["match_cards raised"] += 1
            if args.verbose:
                print(f"  job {r.job_id}: match_cards raised {e!r}")
            continue

        old.add(r.llm_score, r.expanded_score, r.spread)
        new.add(r.llm_score, res.expanded, res.spread)
        d = assign_band(res.direct, res.expanded, res.spread, res.low_confidence)
        bands[d.band] = bands.get(d.band, 0) + 1

        # Did the SHORTLIST DECISION change, and did it change toward Claude?
        was = r.expanded_score >= bar
        now = res.expanded >= bar
        truth = r.llm_score >= bar
        verdict = ("fixed" if was != now and now == truth else
                   "broke" if was != now and now != truth else "")
        moved.append((abs(res.expanded - r.expanded_score), r.job_id, r.llm_score,
                      r.expanded_score, res.expanded, r.spread, res.spread, verdict))
        if verdict:
            flips.append({"job": r.job_id, "llm": r.llm_score, "verdict": verdict,
                          "old": r.expanded_score, "new": res.expanded,
                          "old_bd": _factors(r.breakdown), "new_bd": res.breakdown,
                          "blockers": res.blockers, "low_conf": res.low_confidence})

    replayed = old.n
    if not replayed:
        print(f"Found {len(rows)} shadow rows but replayed none:")
        for k, v in skipped.items():
            if v:
                print(f"    {k:22} {v}")
        print("\nThe cards are gone, so the ledger cannot be re-scored — the only")
        print("path to a post-fix number is new finals through the app.")
        return 1

    print(f"=== CardRace replay — {replayed} of {len(rows)} rows re-scored, "
          f"{min(r.created_at for r in rows):%Y-%m-%d} → "
          f"{max(r.created_at for r in rows):%Y-%m-%d} ===")
    print("    OLD = as stored (the resolver at the time)   "
          "NEW = today's code, same cards, same Claude finals\n")
    if any(skipped.values()):
        print("  not replayed:")
        for k, v in skipped.items():
            if v:
                print(f"    {k:22} {v:5d}  {_pct(v, len(rows))}")
        print()

    print(f"  {'metric':34} {'OLD':>7} {'NEW':>7}  {'change':>8}")
    print(f"  {'-' * 34} {'-' * 7} {'-' * 7}  {'-' * 8}")
    print(f"  {'MAE vs Claude (points)':34} {old.mae:7.1f} {new.mae:7.1f}  "
          f"{_delta(old.mae, new.mae, better='down')}")
    print(f"  {'within 10 points':34} {_pct(old.within10, replayed):>7} "
          f"{_pct(new.within10, replayed):>7}  "
          f"{_delta(100 * old.within10 / replayed, 100 * new.within10 / replayed)}")
    print(f"  {'decision agreement':34} {_pct(old.agree, replayed):>7} "
          f"{_pct(new.agree, replayed):>7}  "
          f"{_delta(100 * old.agree / replayed, 100 * new.agree / replayed)}"
          "   <- the number that matters")
    print(f"  {'DROPPED a job Claude accepted':34} {_pct(old.false_out, replayed):>7} "
          f"{_pct(new.false_out, replayed):>7}  "
          f"{_delta(100 * old.false_out / replayed, 100 * new.false_out / replayed, 'down')}")
    print(f"  {'ADMITTED a job Claude rejected':34} {_pct(old.false_in, replayed):>7} "
          f"{_pct(new.false_in, replayed):>7}  "
          f"{_delta(100 * old.false_in / replayed, 100 * new.false_in / replayed, 'down')}")
    print(f"  {'rows with non-zero spread':34} {_pct(old.spread_nonzero, replayed):>7} "
          f"{_pct(new.spread_nonzero, replayed):>7}  "
          f"{_delta(100 * old.spread_nonzero / replayed, 100 * new.spread_nonzero / replayed)}")

    flips_fixed = sum(1 for m in moved if m[7] == "fixed")
    flips_broke = sum(1 for m in moved if m[7] == "broke")
    print(f"\n  shortlist decisions that FLIPPED: {flips_fixed + flips_broke}"
          f"   toward Claude {flips_fixed}   away from Claude {flips_broke}")

    print("\n  would-be bands under today's code "
          "(no calibration file => everything BAND => Claude decides):")
    for b, c in sorted(bands.items(), key=lambda kv: -kv[1]):
        print(f"    {b:10} {c:5d}  {_pct(c, replayed)}")

    # Card drift: user_card is unique per user and updated IN PLACE on recompile,
    # so a résumé edit since the ledger window means we replayed a different
    # candidate than the one Claude scored. Silent, and it would look like a
    # g() regression, so say it out loud.
    oldest = min(r.created_at for r in rows)
    drifted = [u for u, c in user_cards.items() if c.updated_at > oldest]
    if drifted:
        print(f"\n  CAVEAT: {len(drifted)} user card(s) were recompiled after "
              f"{oldest:%Y-%m-%d} ({', '.join(sorted(drifted)[:3])}"
              f"{'...' if len(drifted) > 3 else ''}).")
        print("  Those rows were replayed against a NEWER candidate than Claude saw,")
        print("  so part of any movement is the résumé changing, not the resolver.")

    if args.flips and flips:
        _autopsy(flips, bar)

    show = sorted(moved, reverse=True)[:args.limit] if not args.verbose else moved
    print(f"\n=== {len(show)} largest movements ===")
    print(f"{'job':>8} {'Claude':>7} {'g() old':>8} {'g() new':>8} {'move':>7} "
          f"{'sprd old':>8} {'sprd new':>8}  decision")
    for _, jid, llm, g_old, g_new, sp_old, sp_new, verdict in show:
        print(f"{jid:>8} {llm:7.1f} {g_old:8.1f} {g_new:8.1f} {g_new - g_old:+7.1f} "
              f"{sp_old:8.2f} {sp_new:8.2f}  {verdict}")

    print(f"\n  bar={bar:.0f}   CARD_MATCH_ENABLED={settings.card_match_enabled}"
          f"{'' if settings.card_match_enabled else '  (shadow only — decides nothing)'}")
    print("  Nothing was written and no model was called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
