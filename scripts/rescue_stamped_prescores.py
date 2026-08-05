#!/usr/bin/env python3
"""Re-queue jobs a known-bad Tier-1 prompt window falsely drained.

The v2 Tier-1 prompt (live between the banded-prompt deploy and the v3 fix)
keyword-matched "hybrid" as a blocker: clean in-country hybrid jobs prescored
~20-30, landed below the gate, and were stamped out of the corpus permanently.
The forward bleeding stopped with v3; this tool rescues the jobs stamped
DURING the window by clearing their scores back to NULL, which is the lanes'
queue — the scoring lane re-prescores them under the fixed prompt on its next
cycle (~$0.0002 each; only genuine fits go on to pay a Tier-2 final).

Deliberately narrow: only rows that are (a) Tier-1 drain stamps — reasoning
starts with 'Pre-screened' — never ghost/rule/door stamps or Claude finals,
(b) prescore <= --max-prescore, (c) location matching --location-like,
(d) stamped inside the window, (e) open, non-shared-pool rows.

    python -m scripts.rescue_stamped_prescores --since 2026-08-04            # report
    python -m scripts.rescue_stamped_prescores --since 2026-08-04 --yes      # rescue
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from sqlmodel import select

from app.db.init_db import get_session, init_db
from app.db.models import Job


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="window start, YYYY-MM-DD "
                    "(the bad prompt's deploy date)")
    ap.add_argument("--until", default=None, help="window end, YYYY-MM-DD "
                    "(default: now — use the v3 deploy date once known)")
    ap.add_argument("--max-prescore", type=float, default=30.0,
                    help="rescue only stamps at or below this (default 30 — the "
                         "keyword-blocker parking spot)")
    ap.add_argument("--location-like", default="hybrid",
                    help="substring the location must contain (default 'hybrid'; "
                         "'' disables the location filter)")
    ap.add_argument("--yes", action="store_true", help="actually clear (default: report)")
    args = ap.parse_args()

    try:
        since = datetime.strptime(args.since, "%Y-%m-%d")
        until = datetime.strptime(args.until, "%Y-%m-%d") if args.until else datetime.utcnow()
    except ValueError as e:
        print(f"bad date: {e}")
        return 2

    init_db()
    from app.discovery.pipeline import SHARED_POOL_USER
    with get_session() as session:
        q = select(Job).where(
            Job.prescore.is_not(None),
            Job.prescore <= args.max_prescore,
            Job.rerank_reasoning.like("Pre-screened%"),   # Tier-1 drains ONLY
            Job.rerank_breakdown.is_(None),               # never a Claude final
            Job.is_closed == False,                       # noqa: E712
            Job.user_id != SHARED_POOL_USER,
            Job.last_seen >= since,
            Job.last_seen <= until,
        )
        if args.location_like:
            q = q.where(Job.location.ilike(f"%{args.location_like}%"))
        rows = session.exec(q).all()
        for r in rows:
            session.expunge(r)

    print(f"{len(rows)} falsely-drained candidate(s) in the window "
          f"({args.since} .. {args.until or 'now'}, prescore<={args.max_prescore:.0f}, "
          f"location~'{args.location_like}')")
    for r in rows[:20]:
        print(f"  job {r.id} [{r.user_id}]: prescore={r.prescore:.0f} "
              f"'{r.title}' @ {r.company} ({r.location})")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")
    if not rows:
        return 0
    if not args.yes:
        est = len(rows) * 0.0002
        print(f"\nDry run. --yes clears their scores back to NULL so the lanes "
              f"re-prescore them under the fixed prompt (~${est:.2f} in Tier-1; "
              f"only genuine fits then pay a Tier-2 final).")
        return 0

    cleared = 0
    with get_session() as session:
        for r in rows:
            job = session.get(Job, r.id)
            # Idempotency: another lane may have re-scored it since the report.
            if not job or job.rerank_breakdown is not None or job.is_closed:
                continue
            job.rerank_score = None
            job.rerank_reasoning = None
            job.prescore = None
            session.add(job)
            cleared += 1
        session.commit()
    print(f"Cleared {cleared} job(s) back to the unscored queue. The 90s scoring "
          f"lane picks them up next cycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
