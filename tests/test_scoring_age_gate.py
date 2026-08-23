"""Scoring age gate: unscored jobs past a freshness bound drain in one bulk
stamp at $0 instead of paying prescores/finals during a backlog catch-up. Fresh
jobs, already-scored jobs, and the shared pool are untouched.

This file covers the DRAIN mechanics (what the gate touches, and that draining
is free). The two-bound SEMANTICS — known age vs source posting age, and why
conflating them expired most of the funnel on arrival — live in
tests/test_expiry_semantics.py."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import Job, JobSource
from app.discovery.pipeline import SHARED_POOL_USER
from app.strategy.scoring_lane import _expire_stale_unscored


def _job(session, user_id, ext, age_days, score=None):
    j = Job(source=JobSource.GREENHOUSE, external_id=ext, company="Acme",
            title=f"Role {ext}", url=f"https://x.test/{ext}", user_id=user_id,
            rerank_score=score,
            first_seen=datetime.utcnow() - timedelta(days=age_days))
    session.add(j)
    session.commit()
    session.refresh(j)
    return j.id


def _cleanup():
    with get_session() as session:
        session.exec(delete(Job))
        session.commit()


def test_old_unscored_expire_fresh_and_scored_survive(monkeypatch):
    init_db()
    _cleanup()
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 14)
    with get_session() as session:
        old_unscored = _job(session, "u1", "old", 30)
        fresh_unscored = _job(session, "u1", "fresh", 3)
        old_scored = _job(session, "u1", "scored", 30, score=72.0)
        shared_old = _job(session, SHARED_POOL_USER, "shared", 30)

    assert _expire_stale_unscored()["total"] == 1

    with get_session() as session:
        j = session.get(Job, old_unscored)
        assert j.rerank_score == 8.0 and "Expired unscored" in j.rerank_reasoning
        assert session.get(Job, fresh_unscored).rerank_score is None   # still queued
        assert session.get(Job, old_scored).rerank_score == 72.0       # untouched
        assert session.get(Job, shared_old).rerank_score is None       # shared excluded
    _cleanup()


def test_zero_disables(monkeypatch):
    init_db()
    _cleanup()
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 0)
    # Both bounds off — the posted bound is a separate knob now, and leaving it
    # on would expire this row for a different (correct) reason.
    monkeypatch.setattr(settings, "scoring_max_posted_age_days", 0)
    with get_session() as session:
        jid = _job(session, "u1", "ancient", 300)
    assert _expire_stale_unscored()["total"] == 0
    with get_session() as session:
        assert session.get(Job, jid).rerank_score is None
    _cleanup()


def test_expired_jobs_leave_the_queue(monkeypatch):
    from app.strategy.scoring_lane import _user_queue
    init_db()
    _cleanup()
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 14)
    with get_session() as session:
        _job(session, "u1", "old-1", 40)
        fresh = _job(session, "u1", "fresh-1", 1)
    _expire_stale_unscored()
    assert _user_queue("u1", cap=10) == [fresh]   # only the fresh job remains
    _cleanup()
