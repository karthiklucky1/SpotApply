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


# ══════════════════════════════════════════════════════════════════════════
# The sweep runs FIRST in every cycle, so it must never spend the cycle
# ══════════════════════════════════════════════════════════════════════════
# Production incident, visible on two consecutive builds: this sweep's SELECT
# hit Supabase's statement timeout at ~150s against a 120s cycle deadline, so
# every cycle between 02:30 and 03:00 UTC logged `queued: 200, scored: 0,
# drained: 0`. The lane was alive and on schedule and bought nothing — all of
# its budget went to housekeeping before a single job was scored.
#
# Stopping this sweep early is CHEAP: `_user_queue` bounds by the same
# freshness expression, so a row it did not reach is still invisible to the
# workers. Stopping SCORING early is the thing users feel.

def test_a_past_deadline_stops_the_sweep_before_it_asks_anything(monkeypatch):
    import time as _time
    init_db()
    _cleanup()
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 14)
    with get_session() as session:
        jid = _job(session, "u1", "old-deadline", 40)

    out = _expire_stale_unscored(deadline=_time.monotonic() - 1)
    assert out["total"] == 0
    assert out["stopped"] == "cycle_deadline"
    with get_session() as session:
        assert session.get(Job, jid).rerank_score is None, "left for the next cycle"
    _cleanup()


def test_a_spent_slice_stops_between_batches_and_keeps_what_it_did(monkeypatch):
    """Partial progress is the point: the rows it reached are stamped, the
    rest wait, and scoring gets the remainder of the cycle."""
    from app.strategy import scoring_lane as sl
    init_db()
    _cleanup()
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 14)
    monkeypatch.setattr(settings, "scoring_max_posted_age_days", 0)
    with get_session() as session:
        for n in range(5):
            _job(session, "u1", f"old-slice-{n}", 40)

    class _Clock:
        """Every reading advances 8s, so a 20s slice allows two batches."""
        def __init__(self):
            self.t = 1000.0

        def __call__(self):
            self.t += 8.0
            return self.t

    monkeypatch.setattr(sl.time, "monotonic", _Clock())
    out = _expire_stale_unscored(batch=1, max_seconds=20)
    assert out["stopped"] == "slice_spent"
    assert 0 < out["total"] < 5, f"partial progress expected, got {out['total']}"

    # Nothing is lost — the next cycle finishes the job.
    monkeypatch.undo()
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 14)
    monkeypatch.setattr(settings, "scoring_max_posted_age_days", 0)
    assert _expire_stale_unscored(batch=10)["stopped"] == ""
    with get_session() as session:
        assert all(j.rerank_score is not None for j in session.exec(
            __import__("sqlmodel").select(Job)).all())
    _cleanup()


def test_an_unbounded_slice_is_still_available(monkeypatch):
    """0 means unbounded — the old behaviour stays reachable by config."""
    init_db()
    _cleanup()
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 14)
    monkeypatch.setattr(settings, "scoring_expiry_max_seconds", 0)
    with get_session() as session:
        jid = _job(session, "u1", "old-unbounded", 40)
    out = _expire_stale_unscored()
    assert out["stopped"] == ""
    with get_session() as session:
        assert session.get(Job, jid).rerank_score is not None
    _cleanup()
