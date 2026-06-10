"""Clean up orphan applications and obvious test-data rows.

Why this exists:
  Job rows were deleted at some point (test cleanup, earlier dedup, manual edits)
  without removing the Application rows that referenced them. SQLite does not
  cascade deletes by default and the FK has no ondelete rule, so those
  applications became "orphans": they still count toward shortlist/stat totals
  (inflating e.g. "19 shortlisted") but never render on the dashboard, which
  inner-joins Job. This script removes them so counts match what you see.

It also removes well-known seed/test companies (FunnelCo) that can leak into a
real DB if the test suite is ever run against it.

Usage:
    python -m scripts.cleanup_orphans           # dry run (counts only)
    python -m scripts.cleanup_orphans --apply    # perform the cleanup
"""
from __future__ import annotations

import sys

from sqlmodel import select

from app.db.init_db import get_session, init_db
from app.db.models import Job, Application

# Companies that only ever come from the test suite / seed fixtures.
_TEST_COMPANIES = {"funnelco"}


def main(apply: bool) -> None:
    init_db()
    with get_session() as session:
        apps = session.exec(select(Application)).all()
        job_ids = {j.id for j in session.exec(select(Job)).all()}

        orphans = [a for a in apps if a.job_id not in job_ids]

        # Test-data jobs + their applications.
        test_jobs = [
            j for j in session.exec(select(Job)).all()
            if (j.company or "").strip().lower() in _TEST_COMPANIES
        ]
        test_job_ids = {j.id for j in test_jobs}
        test_apps = [a for a in apps if a.job_id in test_job_ids]

        print(f"Orphan applications (Job deleted): {len(orphans)}")
        print(f"Test-data jobs ({', '.join(sorted(_TEST_COMPANIES))}): {len(test_jobs)} "
              f"({len(test_apps)} applications)")

        if not apply:
            print("\nDry run only. Re-run with --apply to delete the above.")
            return

        for a in orphans:
            session.delete(a)
        for a in test_apps:
            session.delete(a)
        for j in test_jobs:
            session.delete(j)
        session.commit()
        print(f"\nDeleted {len(orphans)} orphan apps, {len(test_apps)} test apps, "
              f"{len(test_jobs)} test jobs.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
