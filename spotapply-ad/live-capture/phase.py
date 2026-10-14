"""Flip the demo between capture phases (fictional data, local SQLite only).

    phase.py untailored   hero application = SHORTLISTED, no documents (board + match report)
    phase.py tailored     hero application = TAILORED, documents linked (studio / review)
    phase.py oneclick on|off   UserProfile.one_click_submit
    phase.py submitted        hero role SUBMITTED, receipt stamped
    phase.py needs_sponsorship | citizen   work authorization for the sponsorship capture
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _stubs  # noqa: E402,F401
import _netguard  # noqa: E402,F401
import logging  # noqa: E402

logging.disable(logging.INFO)

from sqlmodel import select  # noqa: E402

from app.db.init_db import get_session  # noqa: E402
from app.db.models import Application, ApplicationStatus, UserProfile  # noqa: E402

state = json.load(open(os.path.join(os.environ["LIVE_DIR"], "seed_state.json")))
hero = state["app_ids"]["lumen"]
cmd = sys.argv[1]
with get_session() as s:
    if cmd in ("untailored", "tailored"):
        a = s.get(Application, hero)
        if cmd == "tailored":
            a.status = ApplicationStatus.TAILORED
            a.tailored_resume_path = state["lumen_docs"]["resume"]
            a.cover_letter_path = state["lumen_docs"]["cover"]
            a.tailored_at = datetime.utcnow() - timedelta(minutes=40)
        else:
            a.status = ApplicationStatus.SHORTLISTED
            a.tailored_resume_path = None
            a.cover_letter_path = None
            a.tailored_at = None
        a.viewed_at = None
        s.add(a)
    elif cmd == "submitted":
        # The hero role, submitted 1h 48m after it was posted (still inside
        # the seed's "posted 2 hours ago"), receipt written by the app's own
        # earliness.stamp.
        from app.db.models import Job
        from app.strategy import earliness
        a = s.get(Application, hero)
        job = s.get(Job, a.job_id)
        posted = job.posted_at or job.first_seen
        a.status = ApplicationStatus.SUBMITTED
        a.tailored_resume_path = state["lumen_docs"]["resume"]
        a.cover_letter_path = state["lumen_docs"]["cover"]
        a.viewed_at = posted + timedelta(minutes=40)
        a.tailored_at = posted + timedelta(hours=1, minutes=5)
        a.submitted_at = posted + timedelta(hours=1, minutes=48)
        a.updated_at = a.submitted_at
        earliness.stamp(a, job, now=a.submitted_at)
        print("receipt", a.applied_after_min, a.applied_ref)
        s.add(a)
    elif cmd in ("needs_sponsorship", "citizen"):
        # One capture for the explainer's sponsorship chapter uses a profile
        # that NEEDS sponsorship; "citizen" restores the demo candidate.
        p = s.exec(select(UserProfile)).first()
        if cmd == "needs_sponsorship":
            p.work_authorization, p.work_auth_status, p.requires_sponsorship = "F-1 OPT", "F-1 OPT", True
        else:
            p.work_authorization, p.work_auth_status, p.requires_sponsorship = "Citizen", "Citizen", False
        s.add(p)
    elif cmd == "oneclick":
        p = s.exec(select(UserProfile)).first()
        p.one_click_submit = (sys.argv[2] == "on")
        s.add(p)
    else:
        raise SystemExit(f"unknown phase {cmd!r}")
    s.commit()
print("phase:", " ".join(sys.argv[1:]))
