"""Shortlist hygiene: postings older than the freshness window leave the board."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.strategy.shortlist_hygiene import prune_stale_shortlist


def _clean(session):
    for m in (Application, Job):
        session.exec(delete(m))
    session.commit()


def _job(session, ext, *, posted_days_ago=None, first_seen_days_ago=None):
    now = datetime.utcnow()
    j = Job(title="ML Engineer", company=f"Co{ext}", url=f"http://x/{ext}",
            description="x", source=JobSource.GREENHOUSE, external_id=str(ext),
            posted_at=(now - timedelta(days=posted_days_ago)) if posted_days_ago is not None else None,
            first_seen=(now - timedelta(days=first_seen_days_ago)) if first_seen_days_ago is not None else None)
    session.add(j); session.commit(); session.refresh(j)
    return j


def _app(session, job, status=ApplicationStatus.SHORTLISTED):
    session.add(Application(job_id=job.id, status=status, apply_track="autofill"))
    session.commit()


def _status(session, job_id):
    return session.exec(select(Application).where(Application.job_id == job_id)).first().status


def test_old_shortlisted_job_is_removed():
    with get_session() as s:
        _clean(s)
        old = _job(s, 1, posted_days_ago=20)   # older than 14
        fresh = _job(s, 2, posted_days_ago=3)  # within 14
        _app(s, old); _app(s, fresh)

    assert prune_stale_shortlist(max_age_days=14) == 1

    with get_session() as s:
        old = s.exec(select(Job).where(Job.external_id == "1")).first()
        fresh = s.exec(select(Job).where(Job.external_id == "2")).first()
        assert _status(s, old.id) == ApplicationStatus.SKIPPED   # removed
        assert _status(s, fresh.id) == ApplicationStatus.SHORTLISTED  # kept


def test_falls_back_to_first_seen_when_no_posted_at():
    with get_session() as s:
        _clean(s)
        j = _job(s, 3, first_seen_days_ago=30)  # no posted_at, old first_seen
        _app(s, j)
    assert prune_stale_shortlist(max_age_days=14) == 1
    with get_session() as s:
        j = s.exec(select(Job).where(Job.external_id == "3")).first()
        assert _status(s, j.id) == ApplicationStatus.SKIPPED


def test_tailored_and_submitted_are_not_pruned():
    with get_session() as s:
        _clean(s)
        jt = _job(s, 4, posted_days_ago=40)
        js = _job(s, 5, posted_days_ago=40)
        _app(s, jt, status=ApplicationStatus.TAILORED)     # user invested → keep
        _app(s, js, status=ApplicationStatus.SUBMITTED)    # already applied → keep
    assert prune_stale_shortlist(max_age_days=14) == 0
    with get_session() as s:
        jt = s.exec(select(Job).where(Job.external_id == "4")).first()
        js = s.exec(select(Job).where(Job.external_id == "5")).first()
        assert _status(s, jt.id) == ApplicationStatus.TAILORED
        assert _status(s, js.id) == ApplicationStatus.SUBMITTED


def test_disabled_when_zero():
    with get_session() as s:
        _clean(s)
        j = _job(s, 6, posted_days_ago=100)
        _app(s, j)
    assert prune_stale_shortlist(max_age_days=0) == 0
    with get_session() as s:
        j = s.exec(select(Job).where(Job.external_id == "6")).first()
        assert _status(s, j.id) == ApplicationStatus.SHORTLISTED


def test_jobs_without_dates_are_left_alone():
    with get_session() as s:
        _clean(s)
        j = _job(s, 7)  # no posted_at, no first_seen → can't judge age
        _app(s, j)
    assert prune_stale_shortlist(max_age_days=14) == 0
    with get_session() as s:
        j = s.exec(select(Job).where(Job.external_id == "7")).first()
        assert _status(s, j.id) == ApplicationStatus.SHORTLISTED
