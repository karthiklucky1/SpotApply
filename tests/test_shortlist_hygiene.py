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


# ── the freshness clause across every status and age ─────────────────────────
# Reported 2026-08-02: 6-7 week old jobs sitting in the shortlist. The board data
# settled it — all 25 offending rows were TAILORED, none SHORTLISTED, and
# filter_age matched displayed_age exactly. So it was not a measurement bug: the
# clause exempted invested work from the window ENTIRELY. That sounded protective
# and was not. Those postings are long filled, and they buried the current week's
# matches under two months of dead listings. Invested work now gets a LONGER
# window, not an unlimited one.

import pytest  # noqa: E402
from datetime import datetime as _dt, timedelta as _td  # noqa: E402


@pytest.mark.parametrize("status_name,age_days,should_show,why", [
    ("SHORTLISTED",     2,  True,  "inside the 5-day match window"),
    ("SHORTLISTED",     9,  False, "past the 5-day window"),
    ("TAILORED",        9,  True,  "invested work inside the 14-day grace"),
    ("TAILORED",        45, False, "the 32-52 day rows that prompted this"),
    ("READY_TO_SUBMIT", 45, False, "autofill review is invested work, not immortal"),
    ("AWAITING_USER",   3,  True,  "fresh autofill review"),
    ("SUBMITTED",       45, True,  "pipeline history — the window must not touch it"),
    ("INTERVIEWING",    90, True,  "never hide an interview"),
])
def test_freshness_window_by_status_and_age(status_name, age_days, should_show, why):
    from sqlmodel import select

    from app.api.server import _shortlist_fresh_clause
    from app.db.models import Application, ApplicationStatus, Job, JobSource

    status = getattr(ApplicationStatus, status_name)
    ext = f"fw-{status_name}-{age_days}"
    with get_session() as s:
        for a in s.exec(select(Application).where(Application.user_id == "fw-user")).all():
            s.delete(a)
        for j in s.exec(select(Job).where(Job.user_id == "fw-user")).all():
            s.delete(j)
        s.commit()
        job = Job(user_id="fw-user", source=JobSource.GREENHOUSE, external_id=ext,
                  company="C", title="T", url=f"https://x/{ext}", description="d",
                  posted_at=_dt.utcnow() - _td(days=age_days))
        s.add(job)
        s.commit()
        s.refresh(job)
        s.add(Application(user_id="fw-user", job_id=job.id, status=status))
        s.commit()
        rows = s.exec(
            select(Job.external_id)
            .join(Application, Application.job_id == Job.id)
            .where(Application.user_id == "fw-user")
            .where(_shortlist_fresh_clause())
        ).all()
    shown = ext in {r if isinstance(r, str) else r[0] for r in rows}
    assert shown is should_show, (
        f"{status_name} at {age_days}d: visible={shown}, expected {should_show} — {why}")


def test_invested_work_gets_a_longer_window_than_a_plain_match():
    """The whole point: tailoring something buys it more time, not immunity."""
    from app.config import settings
    assert settings.tailored_max_age_days > settings.shortlist_max_age_days
    assert settings.tailored_max_age_days > 0, (
        "0 restores the never-hide behaviour that let 52-day-old postings bury "
        "the current week's matches")
