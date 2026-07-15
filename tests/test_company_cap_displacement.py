"""Company-cap displacement: a stronger new job evicts the weakest cap-holder
that is still merely SHORTLISTED — never anything the user/agent acted on."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlmodel import delete, select

import app.matching.pipeline as mp
import app.strategy.scoring_lane as sl
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    Application, ApplicationStatus, FunnelEvent, Job, JobSource,
    UserNotification, UserProfile,
)


def _clean(session):
    for model in (Application, UserNotification, FunnelEvent, Job, UserProfile):
        session.exec(delete(model))
    session.commit()


def _mk_job(session, ext, score=None, company="Cohere", uid="ua", title=None):
    j = Job(title=title or f"Role {ext}", company=company, location="Remote",
            remote=True, description="LLMs", source=JobSource.GREENHOUSE,
            external_id=str(ext), url=f"https://x/{ext}", user_id=uid,
            rerank_score=score, first_seen=datetime.utcnow())
    session.add(j)
    session.commit()
    session.refresh(j)
    return j


def _mk_app(session, job, status=ApplicationStatus.SHORTLISTED):
    a = Application(job_id=job.id, status=status, apply_url=job.url,
                    apply_track="manual", user_id=job.user_id)
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


def _fill_cap(session, scores, status=ApplicationStatus.SHORTLISTED):
    """Create len(scores) cap-holding applications at Cohere."""
    apps = []
    for i, s in enumerate(scores):
        j = _mk_job(session, f"h{i}", score=s)
        apps.append(_mk_app(session, j, status=status))
    return apps


def test_stronger_job_displaces_weakest_shortlisted(monkeypatch):
    monkeypatch.setattr(settings, "company_cap", 3)
    monkeypatch.setattr(settings, "company_cap_displace_enabled", True)
    monkeypatch.setattr(settings, "company_cap_displace_margin", 5)
    with get_session() as session:
        _clean(session)
        apps = _fill_cap(session, [40.0, 50.0, 60.0])
        new_job = _mk_job(session, "new", score=72.0, title="Senior ML Engineer")

        assert mp._check_and_enforce_company_cap(session, new_job, 72.0) is True
        session.commit()

        weakest = session.get(Application, apps[0].id)
        assert weakest.status == ApplicationStatus.SKIPPED
        assert "Displaced by higher-scoring 'Senior ML Engineer'" in (weakest.notes or "")
        # The other two holders are untouched.
        assert session.get(Application, apps[1].id).status == ApplicationStatus.SHORTLISTED
        assert session.get(Application, apps[2].id).status == ApplicationStatus.SHORTLISTED


def test_margin_prevents_churn(monkeypatch):
    monkeypatch.setattr(settings, "company_cap", 3)
    monkeypatch.setattr(settings, "company_cap_displace_enabled", True)
    monkeypatch.setattr(settings, "company_cap_displace_margin", 5)
    with get_session() as session:
        _clean(session)
        apps = _fill_cap(session, [40.0, 50.0, 60.0])
        new_job = _mk_job(session, "new", score=42.0)

        # 42 beats 40 but not by the 5-point margin → blocked, nothing displaced.
        assert mp._check_and_enforce_company_cap(session, new_job, 42.0) is False
        assert session.get(Application, apps[0].id).status == ApplicationStatus.SHORTLISTED


def test_invested_applications_are_never_displaced(monkeypatch):
    monkeypatch.setattr(settings, "company_cap", 3)
    monkeypatch.setattr(settings, "company_cap_displace_enabled", True)
    monkeypatch.setattr(settings, "company_cap_displace_margin", 5)
    with get_session() as session:
        _clean(session)
        for i, (s, st) in enumerate([(30.0, ApplicationStatus.SUBMITTED),
                                     (35.0, ApplicationStatus.TAILORED),
                                     (38.0, ApplicationStatus.AWAITING_USER)]):
            _mk_app(session, _mk_job(session, f"h{i}", score=s), status=st)
        new_job = _mk_job(session, "new", score=95.0)

        # Even a 95 can't take a slot from work already in flight.
        assert mp._check_and_enforce_company_cap(session, new_job, 95.0) is False
        assert not session.exec(select(Application).where(
            Application.status == ApplicationStatus.SKIPPED)).all()


def test_mixed_holders_only_shortlisted_is_evicted(monkeypatch):
    monkeypatch.setattr(settings, "company_cap", 3)
    monkeypatch.setattr(settings, "company_cap_displace_enabled", True)
    monkeypatch.setattr(settings, "company_cap_displace_margin", 5)
    with get_session() as session:
        _clean(session)
        _mk_app(session, _mk_job(session, "h0", score=20.0), status=ApplicationStatus.SUBMITTED)
        _mk_app(session, _mk_job(session, "h1", score=25.0), status=ApplicationStatus.SUBMITTED)
        sl_app = _mk_app(session, _mk_job(session, "h2", score=50.0))
        new_job = _mk_job(session, "new", score=60.0)

        assert mp._check_and_enforce_company_cap(session, new_job, 60.0) is True
        session.commit()
        # The SHORTLISTED holder went, not the (lower-scoring) SUBMITTED ones.
        assert session.get(Application, sl_app.id).status == ApplicationStatus.SKIPPED


def test_displacement_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "company_cap", 3)
    monkeypatch.setattr(settings, "company_cap_displace_enabled", False)
    with get_session() as session:
        _clean(session)
        apps = _fill_cap(session, [40.0, 50.0, 60.0])
        new_job = _mk_job(session, "new", score=99.0)
        assert mp._check_and_enforce_company_cap(session, new_job, 99.0) is False
        assert session.get(Application, apps[0].id).status == ApplicationStatus.SHORTLISTED


def test_displaced_job_does_not_bounce_back(monkeypatch):
    """The displaced job keeps a SKIPPED application row, which blocks both
    re-shortlist paths — no flip-flop between two same-company jobs."""
    monkeypatch.setattr(settings, "company_cap", 1)
    monkeypatch.setattr(settings, "company_cap_displace_enabled", True)
    monkeypatch.setattr(settings, "company_cap_displace_margin", 5)
    with get_session() as session:
        _clean(session)
        old_job = _mk_job(session, "old", score=40.0)
        _mk_app(session, old_job)
        new_job = _mk_job(session, "new", score=60.0)
        assert mp._check_and_enforce_company_cap(session, new_job, 60.0) is True
        session.commit()

        # Displaced job still has its (now SKIPPED) application row.
        row = session.exec(select(Application).where(
            Application.job_id == old_job.id)).first()
        assert row is not None and row.status == ApplicationStatus.SKIPPED

    # The re-shortlist backstop skips any job that has an application row.
    ids, _ = mp._reshortlist_scored_jobs("ua", today_count=0)
    assert old_job.id not in ids


# ── write-back race (three-phase _score_job) ─────────────────────────────────
def test_stamp_job_loses_race_gracefully():
    with get_session() as session:
        _clean(session)
        j = _mk_job(session, "raced", score=88.0)     # already scored by another lane
    assert sl._stamp_job(j.id, None, 70.0, "late write") is False
    with get_session() as session:
        assert session.get(Job, j.id).rerank_score == 88.0  # first score kept


def test_stamp_job_writes_score_and_ghost():
    with get_session() as session:
        _clean(session)
        j = _mk_job(session, "fresh", score=None)
    assert sl._stamp_job(j.id, (0.2, '["ok"]'), 64.0, "scored") is True
    with get_session() as session:
        row = session.get(Job, j.id)
        assert row.rerank_score == 64.0
        assert row.ghost_score == 0.2 and row.ghost_flags == '["ok"]'
