"""Orphan applications (Job deleted) must not inflate stats."""
from __future__ import annotations

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, Application, ApplicationStatus

_ORPHAN_JOB_ID = 999_000_001


def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _shortlisted_count() -> int:
    return _client().get("/stats").json()["applications"].get("shortlisted", 0)


def test_orphan_app_does_not_change_stats():
    # Clean any prior orphan, take a baseline count.
    with get_session() as s:
        for a in s.exec(select(Application).where(Application.job_id == _ORPHAN_JOB_ID)).all():
            s.delete(a)
        s.commit()
    before = _shortlisted_count()

    # Add an orphan shortlisted application (references a non-existent job).
    with get_session() as s:
        s.add(Application(job_id=_ORPHAN_JOB_ID, status=ApplicationStatus.SHORTLISTED, apply_track="manual"))
        s.commit()

    after = _shortlisted_count()
    assert after == before, "orphan application must not be counted in stats"

    with get_session() as s:
        for a in s.exec(select(Application).where(Application.job_id == _ORPHAN_JOB_ID)).all():
            s.delete(a)
        s.commit()


def test_valid_app_does_change_stats():
    """Sanity check: a non-orphan shortlisted app DOES increment the count."""
    before = _shortlisted_count()
    with get_session() as s:
        j = Job(source=JobSource.REMOTEOK, external_id="orph-valid", company="ValidCo",
                title="Valid Role", url="http://v", description="x", rerank_score=75)
        s.add(j); s.commit(); s.refresh(j)
        s.add(Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, apply_track="manual"))
        s.commit()
        jid = j.id
    try:
        assert _shortlisted_count() == before + 1
    finally:
        with get_session() as s:
            for a in s.exec(select(Application).where(Application.job_id == jid)).all():
                s.delete(a)
            jj = s.get(Job, jid)
            if jj:
                s.delete(jj)
            s.commit()
