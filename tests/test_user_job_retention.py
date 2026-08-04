"""Per-user age-close retention (the missing half that let the job table grow
to 3.8 GB): stale untouched per-user rows close; anything with an Application,
anything fresh, and the shared pool are never touched."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete

from app.db.init_db import get_session, init_db
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.discovery.pipeline import SHARED_POOL_USER
from app.strategy.job_retention import close_stale_user_jobs


def _job(session, user_id, ext, age_days):
    j = Job(source=JobSource.GREENHOUSE, external_id=ext, company="Acme",
            title=f"Role {ext}", url=f"https://x.test/{ext}", user_id=user_id,
            first_seen=datetime.utcnow() - timedelta(days=age_days))
    session.add(j)
    session.commit()
    session.refresh(j)
    return j.id


def _cleanup():
    with get_session() as session:
        session.exec(delete(Application))
        session.exec(delete(Job))
        session.commit()


def test_stale_untouched_user_jobs_close_and_the_rest_survive():
    init_db()
    _cleanup()
    with get_session() as session:
        stale = _job(session, "u1", "stale", 50)
        fresh = _job(session, "u1", "fresh", 10)
        shared = _job(session, SHARED_POOL_USER, "shared-old", 50)
        applied = _job(session, "u1", "applied-old", 50)
        session.add(Application(job_id=applied, user_id="u1",
                                status=ApplicationStatus.SHORTLISTED))
        session.commit()

    n = close_stale_user_jobs(days=45)
    assert n == 1

    with get_session() as session:
        assert session.get(Job, stale).is_closed
        assert "per-user retention" in session.get(Job, stale).closed_reason
        assert not session.get(Job, fresh).is_closed       # too young
        assert not session.get(Job, shared).is_closed      # shared pool excluded
        assert not session.get(Job, applied).is_closed     # user touched it
    _cleanup()


def test_zero_days_disables():
    init_db()
    _cleanup()
    with get_session() as session:
        jid = _job(session, "u1", "old", 400)
    assert close_stale_user_jobs(days=0) == 0
    with get_session() as session:
        assert not session.get(Job, jid).is_closed
    _cleanup()


def test_batching_drains_a_backlog():
    init_db()
    _cleanup()
    with get_session() as session:
        for i in range(7):
            _job(session, "u1", f"old-{i}", 60)
    assert close_stale_user_jobs(days=45, batch=3, max_batches=10) == 7
    _cleanup()
