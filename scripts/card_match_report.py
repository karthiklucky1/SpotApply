"""What is CardRace v2 actually saying? — read-only, zero LLM spend.

Reads the `card_match_shadow` ledger, which already holds one row per real
Claude final scored beside the deterministic g(). Nothing here calls a model,
mints a card, or writes anything — it is safe to run at any time, including
with the Anthropic balance at zero.

NOT scripts/shadow_report.py — that one reads FunnelEvent stage="shadow_score",
which belongs to the distilled cross-encoder track, not CardRace.

    python -m scripts.card_match_report                 # aggregate + worst misses
    python -m scripts.card_match_report --limit 20      # more disagreement detail
    python -m scripts.card_match_report --job 873897    # one job, side by side

Read the numbers as: would flipping CARD_MATCH_ENABLED=1 have changed which
jobs reached the board? `build_calibration --dry-run` answers whether the bands
would CERTIFY; this answers whether the raw score agrees at all.
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
from app.db.models import CardMatchShadow  # noqa: E402


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    —"


def _fmt_breakdown(raw: str | None) -> str:
    try:
        b = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return "—"
    parts = []
    for k in ("skills", "experience", "location", "work_auth"):
        v = b.get(k)
        if isinstance(v, dict) and "score" in v:
            parts.append(f"{k[:4]}={v['score']:.0f}")
    return " ".join(parts) or "—"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=10,
                    help="how many worst disagreements to print (default 10)")
    ap.add_argument("--job", type=int, help="show every shadow row for one job id")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="only rows from this date on. After a change to g() or "
                         "the skill graph, the older rows score a function that "
                         "no longer exists — this is how you read the new one.")
    args = ap.parse_args()

    bar = settings.shortlist_score_threshold
    with get_session() as s:
        q = select(CardMatchShadow)
        if args.job:
            q = q.where(CardMatchShadow.job_id == args.job)
        if args.since:
            try:
                q = q.where(CardMatchShadow.created_at
                            >= datetime.strptime(args.since, "%Y-%m-%d"))
            except ValueError:
                print(f"--since {args.since!r} is not YYYY-MM-DD")
                return 2
        rows = list(s.exec(q).all())

    if not rows:
        print("No card_match_shadow rows yet.")
        print("Shadow records one row per real Claude final, so this fills as the")
        print("scoring lane runs. Check CARD_MATCH_SHADOW=1 and that finals are")
        print("being scored (the lane needs Anthropic credit).")
        return 1

    if args.job:
        print(f"=== job {args.job} — {len(rows)} shadow row(s) ===\n")
        for r in rows:
            print(f"  user={r.user_id}  {r.created_at:%Y-%m-%d %H:%M}")
            print(f"    Claude  : {r.llm_score:5.1f}   {_fmt_breakdown(r.llm_breakdown)}")
            print(f"    g() dir : {r.direct_score:5.1f}")
            print(f"    g() exp : {r.expanded_score:5.1f}   {_fmt_breakdown(r.breakdown)}")
            print(f"    spread  : {r.spread:5.2f}  (share of the score resting on"
                  f" skill-graph inference rather than stated evidence)")
            # calibrated is NULL until data/calibration.json exists — the
            # fail-safe state, not an error, so print it as one.
            cal = f"{r.calibrated:.1f}" if r.calibrated is not None else "— (uncalibrated)"
            print(f"    band    : {r.band}   calibrated={cal}")
            print()
        return 0

    n = len(rows)
    err = [abs(r.llm_score - r.expanded_score) for r in rows]
    mae = sum(err) / n
    within10 = sum(1 for e in err if e <= 10)
    agree = sum(1 for r in rows
                if (r.llm_score >= bar) == (r.expanded_score >= bar))
    # The two failure directions are NOT equally bad.
    false_in = sum(1 for r in rows if r.expanded_score >= bar > r.llm_score)
    false_out = sum(1 for r in rows if r.llm_score >= bar > r.expanded_score)
    bands: dict[str, int] = {}
    for r in rows:
        bands[r.band] = bands.get(r.band, 0) + 1

    print(f"=== CardRace v2 shadow — {n} rows, "
          f"{min(r.created_at for r in rows):%Y-%m-%d} → "
          f"{max(r.created_at for r in rows):%Y-%m-%d} ===\n")
    print(f"  shortlist bar          {bar}")
    print(f"  MAE vs Claude          {mae:.1f} points")
    print(f"  within 10 points       {_pct(within10, n)}")
    print(f"  decision agreement     {_pct(agree, n)}  <- the number that matters")
    print()
    print(f"  would have ADMITTED a job Claude rejected   {false_in:4d}  "
          f"({_pct(false_in, n)})  <- shortlist contamination")
    print(f"  would have DROPPED a job Claude accepted    {false_out:4d}  "
          f"({_pct(false_out, n)})  <- missed matches, the worse error")
    print()
    print("  band distribution (no calibration file => everything BAND => Claude decides):")
    for b, c in sorted(bands.items(), key=lambda kv: -kv[1]):
        print(f"    {b:10} {c:5d}  {_pct(c, n)}")

    cal = Path("data/calibration.json")
    print(f"\n  data/calibration.json  {'present' if cal.exists() else 'ABSENT'}"
          f"{'' if cal.exists() else '  (so bands are inert — this is the fail-safe)'}")
    print(f"  CARD_MATCH_ENABLED     {settings.card_match_enabled}"
          f"{'' if settings.card_match_enabled else '  (shadow only — decides nothing)'}")

    worst = sorted(rows, key=lambda r: -abs(r.llm_score - r.expanded_score))[:args.limit]
    print(f"\n=== {len(worst)} largest disagreements — where g() is wrong today ===")
    print(f"{'job':>8}  {'Claude':>6} {'g()':>6} {'diff':>6} {'spread':>6}  factors")
    for r in worst:
        d = r.expanded_score - r.llm_score
        print(f"{r.job_id:>8}  {r.llm_score:6.1f} {r.expanded_score:6.1f} "
              f"{d:+6.1f} {r.spread:6.2f}  {_fmt_breakdown(r.breakdown)}")
    print("\n  Inspect one with:  python -m scripts.card_match_report --job <id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
