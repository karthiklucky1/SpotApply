"""Fresh-job instant alerts + freshness stats + job-check SSRF guard."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import FunnelEvent, Job, JobSource, UserNotification


def _mk_job(session, i, hours_old, score=80.0, source=JobSource.LEVER):
    job = Job(
        user_id=None, source=source, external_id=f"fa-{i}", company=f"Co{i}",
        title="Backend Engineer", url=f"https://jobs.lever.co/co{i}/x",
        description="jd", rerank_score=score, blended_score=score,
        posted_at=datetime.utcnow() - timedelta(hours=hours_old),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _clean(session):
    session.exec(delete(UserNotification))
    session.exec(delete(FunnelEvent))
    session.exec(delete(Job))
    session.commit()


def test_fresh_alert_created_and_deduped():
    from app.strategy.fresh_alerts import dispatch_fresh_alerts
    with get_session() as session:
        _clean(session)
        fresh = _mk_job(session, 1, hours_old=2)
        stale = _mk_job(session, 2, hours_old=72)
        ids = [fresh.id, stale.id]

    assert dispatch_fresh_alerts("local", ids) == 1
    with get_session() as session:
        notes = session.exec(select(UserNotification)).all()
        assert len(notes) == 1
        assert notes[0].type == "fresh_job"
        assert "posted 2h ago" in notes[0].message
        events = session.exec(select(FunnelEvent).where(FunnelEvent.stage == "fresh_alert")).all()
        assert len(events) == 1 and "latency_min" in (events[0].reason or "")

    # Second dispatch for the same jobs: deduped, nothing new
    assert dispatch_fresh_alerts("local", ids) == 0
    with get_session() as session:
        assert len(session.exec(select(UserNotification)).all()) == 1


def test_greenhouse_edited_old_post_not_alerted(monkeypatch):
    import app.strategy.fresh_alerts as fa
    with get_session() as session:
        _clean(session)
        # Greenhouse job that LOOKS fresh (updated_at yesterday) but was
        # actually first published months ago.
        job = _mk_job(session, 3, hours_old=1, source=JobSource.GREENHOUSE)
        job_id = job.id

    monkeypatch.setattr(fa, "_verify_greenhouse_first_published",
                        lambda job: datetime.utcnow() - timedelta(days=90))
    assert fa.dispatch_fresh_alerts("local", [job_id]) == 0
    with get_session() as session:
        assert session.exec(select(UserNotification)).first() is None
        # posted_at corrected to the true publish time
        row = session.get(Job, job_id)
        assert row.posted_at < datetime.utcnow() - timedelta(days=80)


def test_freshness_stats_endpoint():
    from fastapi.testclient import TestClient
    from app.api.server import app as fastapi_app
    with get_session() as session:
        _clean(session)
        _mk_job(session, 4, hours_old=6)
    from app.strategy.fresh_alerts import dispatch_fresh_alerts
    with get_session() as session:
        job = session.exec(select(Job)).first()
    dispatch_fresh_alerts("local", [job.id])

    client = TestClient(fastapi_app)
    r = client.get("/api/freshness-stats")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["scored_feed_jobs"] == 1
    assert d["median_feed_age_hours"] is not None and d["median_feed_age_hours"] <= 7
    assert d["fresh_alerts_7d"] == 1
    assert d["median_post_to_alert_min"] is not None


def test_job_check_blocks_private_hosts():
    from app.intelligence.job_check import check_job_url
    for url in ("http://localhost:8000/admin", "http://127.0.0.1/x",
                "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/internal"):
        out = check_job_url(url)
        assert out["live"] is None, url  # blocked, never fetched
