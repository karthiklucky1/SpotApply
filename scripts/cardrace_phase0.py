"""CardRace v2 Phase-0 measurement report — docs/CARDRACE_DESIGN.md §5 Phase 0.

Read-only. Answers the unknowns the design's math depends on, from THIS
deployment's own database:

  1. How many stored Claude finals survived the job purge (calibration food)?
  2. Tier-1 advance rate `a` under the current gate (cost-model input).
  3. Per-user adopted inflow d (jobs/user/day, 7-day window).
  4. Distinct-posting pool J_pool proxy (distinct shared postings, 7 days).
  5. Shadow-ledger progress: rows, MAE, decision agreement, band mix.

Usage: python -m scripts.cardrace_phase0
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import func, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.init_db import get_session, init_db  # noqa: E402
from app.db.models import CardMatchShadow, Job  # noqa: E402


def main() -> int:
    init_db()
    bar = float(settings.shortlist_score_threshold)
    week_ago = datetime.utcnow() - timedelta(days=7)
    print("=== CardRace v2 — Phase 0 measurements ===\n")

    with get_session() as session:
        # 1. Surviving Claude finals (rerank_breakdown marks a real Tier-2 final)
        finals = session.exec(select(func.count(Job.id)).where(
            Job.rerank_breakdown.is_not(None))).one()
        finals_with_jd = session.exec(select(func.count(Job.id)).where(
            Job.rerank_breakdown.is_not(None), Job.description != "")).one()
        print(f"1. Claude finals stored: {finals}  (with JD text: {finals_with_jd})")
        if finals:
            print(f"   → calibration food surviving the purge: "
                  f"{100.0 * finals_with_jd / finals:.0f}%")

        # 2. Advance rate a: finals vs Tier-1 drains (stamped 'Pre-screened')
        drained = session.exec(select(func.count(Job.id)).where(
            Job.rerank_reasoning.like("Pre-screened%"))).one()
        seen = finals + drained
        if seen:
            print(f"2. Tier-1 advance rate a ≈ {finals}/{seen} = {finals / seen:.2f}"
                  f"  (finals vs prescore-drained)")
        else:
            print("2. Tier-1 advance rate: no scored rows yet")

        # 3. Adopted inflow d per user per day (7-day window, shared pool excluded)
        rows = session.exec(
            select(Job.user_id, func.count(Job.id))
            .where(Job.discovered_at >= week_ago, Job.user_id != "__shared__",
                   Job.user_id.is_not(None))
            .group_by(Job.user_id)).all()
        if rows:
            per_day = [c / 7.0 for _u, c in rows]
            per_day.sort()
            mid = per_day[len(per_day) // 2]
            print(f"3. Adopted inflow d: {len(rows)} active users, "
                  f"median {mid:.0f} jobs/user/day, max {max(per_day):.0f}")
        else:
            print("3. Adopted inflow: no per-user jobs in the last 7 days")

        # 4. J_pool proxy: distinct shared postings in 7 days
        jpool = session.exec(select(func.count(func.distinct(Job.content_hash)))
                             .where(Job.discovered_at >= week_ago,
                                    Job.user_id == "__shared__")).one()
        print(f"4. J_pool proxy (distinct shared postings, 7d): {jpool}"
              f"  → ~{jpool / 7.0:.0f}/day")

        # 5. Shadow ledger progress
        srows = session.exec(select(CardMatchShadow)).all()
        if srows:
            mae = sum(abs(r.llm_score - r.expanded_score) for r in srows) / len(srows)
            agree = sum(1 for r in srows
                        if (r.llm_score >= bar) == (r.expanded_score >= bar)) / len(srows)
            bands = {}
            for r in srows:
                bands[r.band] = bands.get(r.band, 0) + 1
            print(f"5. Shadow ledger: n={len(srows)}  MAE={mae:.1f}  "
                  f"decision-agree@{bar:.0f}={agree:.1%}  bands={bands}")
            need = max(0, 500 - len(srows))
            if need:
                print(f"   → {need} more rows before scripts/build_calibration.py "
                      f"will fit without --force")
        else:
            print("5. Shadow ledger: empty — set CARD_MATCH_SHADOW=1 and let the "
                  "scoring lane run beside real finals")

    print("\nGates to cutover (§3.4): fit calibration → holdout decision-agree "
          "≥ target, AUTO-IN precision ≥ 95% — then and only then consider "
          "CARD_MATCH_ENABLED=1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
