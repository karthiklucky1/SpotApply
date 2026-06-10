"""Quick health snapshot: job sources, open/closed, and application statuses.
Run:  python -m scripts.status_check
"""
from collections import Counter
from app.db.init_db import init_db, get_session
from app.db.models import Job, Application
from sqlmodel import select

init_db()
with get_session() as s:
    jobs = s.exec(select(Job)).all()
    apps = s.exec(select(Application)).all()

open_jobs = [j for j in jobs if not j.is_closed]
print(f"JOBS: {len(jobs)} total | {len(open_jobs)} open | {len(jobs)-len(open_jobs)} closed")
print("  open by source:", dict(Counter(j.source.value for j in open_jobs)))
print(f"\nAPPLICATIONS: {len(apps)} total")
print("  by status:", dict(Counter(a.status.value for a in apps)))

active = [a for a in apps if a.status.value in ("shortlisted","tailored")]
print(f"\nSHORTLISTED/TAILORED (what the dashboard shows): {len(active)}")
if not active:
    print("  --> Shortlist is EMPTY. Run discovery + matching to repopulate:")
    print("      python -m app.discovery.pipeline   (job-first: pulls aggregator jobs)")
    print("      python -m app.matching.pipeline    (scores + shortlists them)")
