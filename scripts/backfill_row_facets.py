"""One-off backfill for the columns the board reads instead of the posting.

    python -m scripts.backfill_row_facets            # every active user, report
    python -m scripts.backfill_row_facets --yes      # actually write
    python -m scripts.backfill_row_facets --yes --user <uuid> --seconds 1800

`Job.on_role`, `Job.salary_text` and `Job.sponsorship_json` are stamped at write
time for new rows and backfilled at boot for old ones. A pool of ~170k rows takes
a while, and until a row is stamped its card shows no pay or visa signal and its
role filter falls back to the slow ILIKE path — so this exists to finish the job
now rather than over the next N restarts.

Safe to re-run: it only touches rows that have never been stamped, works in
small chunks with a pause between them, and stops on the first error.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import func
from sqlmodel import select

from app.db.init_db import get_session, init_db
from app.db.models import Job, UserProfile


def _users(explicit: str | None) -> list[tuple[str | None, list[str]]]:
    if explicit:
        with get_session() as s:
            p = s.exec(select(UserProfile).where(UserProfile.user_id == explicit)).first()
        roles = [r.strip() for r in ((p.target_roles if p else "") or "").split(",") if r.strip()]
        return [(explicit, roles)]
    out = []
    with get_session() as s:
        for p in s.exec(select(UserProfile)).all():
            roles = [r.strip() for r in (p.target_roles or "").split(",") if r.strip()]
            out.append((p.user_id, roles))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=None, help="one user_id (default: every profile)")
    ap.add_argument("--seconds", type=float, default=900.0, help="wall-clock budget per user")
    ap.add_argument("--chunk", type=int, default=500)
    ap.add_argument("--pause", type=float, default=0.15, help="seconds between chunks")
    ap.add_argument("--yes", action="store_true", help="write (default: report only)")
    args = ap.parse_args()

    init_db()
    from app.strategy import job_facets, on_role

    targets = _users(args.user)
    print(f"{len(targets)} user(s)\n")
    total = 0
    for uid, roles in targets:
        with get_session() as s:
            jobs = s.exec(select(func.count(Job.id)).where(Job.user_id == uid)).first()
            missing_role = s.exec(select(func.count(Job.id)).where(
                Job.user_id == uid, Job.on_role.is_(None))).first()
            missing_facets = s.exec(select(func.count(Job.id)).where(
                Job.user_id == uid, Job.sponsorship_json.is_(None))).first()
        _n = lambda v: (v[0] if isinstance(v, tuple) else v) or 0  # noqa: E731
        print(f"  {uid}: {_n(jobs):>7} jobs | on_role missing {_n(missing_role):>7} | "
              f"facets missing {_n(missing_facets):>7} | roles={roles or '—'}")
        if not args.yes:
            continue
        if roles:
            done = on_role.backfill(uid, roles)
            print(f"    on_role stamped: {done}")
            total += done
        done = job_facets.backfill(uid, max_seconds=args.seconds,
                                   chunk=args.chunk, pause=args.pause)
        print(f"    facets stamped:  {done}")
        total += done

    if not args.yes:
        print("\nDry run. Re-run with --yes to write.")
    else:
        print(f"\nStamped {total} row(s). Re-run until 'missing' reaches 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
