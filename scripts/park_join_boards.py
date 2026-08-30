"""Park the never-yielding join.com dataset import out of the poll rotation.

Production census (2026-08, from 7 days of adapter logs): 17,271 join.com
boards — 40% of the polled registry — with job_count == 0, zero live in the
whole window, and not one net-new posting EVER. They are a bulk dataset seed
(~23.5K companies, docs/research/job-sites-review-2026-08.md) that has never
produced anything, yet at the 72h zero-yield cadence they still buy ~240
polls/hour and crowd the stalest-first rotations.

This parks them SOFTLY: ``next_poll_at`` is pushed ~30 days out (id-derived
jitter spreads the re-probes across a further week so they trickle back, never
stampede). The boards stay ``is_active`` — nothing is retired, the monthly
probe still runs, and any board that ever yields auto-promotes to the fast
cadence the moment ``last_new_job_at`` moves. Re-run monthly (idempotent): a
board that came back and yielded no longer matches the filter.

Dry-run by default; pass ``--apply`` to write.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from sqlalchemy import update as _update
from sqlmodel import select

from app.common.db_retry import run_with_deadlock_retry
from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource

PARK_DAYS = 30
JITTER_DAYS = 7          # spread the monthly re-probes over a week
_BATCH = 500             # rows per executemany write


def _candidates() -> list[int]:
    with get_session() as session:
        rows = session.exec(
            select(CompanyRegistry.id)
            .where(CompanyRegistry.ats == JobSource.JOIN)
            .where(CompanyRegistry.is_active == True)  # noqa: E712
            .where((CompanyRegistry.job_count == 0)
                   | (CompanyRegistry.job_count == None))  # noqa: E711
            .where(CompanyRegistry.last_new_job_at == None)  # noqa: E711
            .order_by(CompanyRegistry.id.asc())
        ).all()
    return list(rows)


def park(apply: bool) -> int:
    ids = _candidates()
    print(f"join.com boards matching (active, job_count=0, never yielded): {len(ids):,}")
    if not ids or not apply:
        if ids:
            print("dry run — pass --apply to park them (~30d re-probe, jittered)")
        return 0
    now = datetime.utcnow()
    rows = [{"id": i,
             "next_poll_at": now + timedelta(days=PARK_DAYS,
                                             seconds=(i % (JITTER_DAYS * 86400)))}
            for i in ids]
    written = 0
    # Ascending primary key, the fixed lock order every multi-row registry
    # writer uses (see pulse_lane._flush_polls) — _candidates() already reads
    # ORDER BY id, and the batches preserve it.
    for start in range(0, len(rows), _BATCH):
        batch = rows[start:start + _BATCH]

        def _write(b=batch):
            with get_session() as session:
                session.execute(_update(CompanyRegistry), b)
                session.commit()

        run_with_deadlock_retry("park-join-boards", _write)
        written += len(batch)
        print(f"  parked {written:,}/{len(rows):,}")
    return written


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the schedule change (default: dry run)")
    args = ap.parse_args()
    init_db()
    park(apply=args.apply)
