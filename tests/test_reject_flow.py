"""Manual rejection: /application/{id}/reject sets REJECTED and the dashboard
collects rejected roles in their own section."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, Application, ApplicationStatus


def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _seed(status):
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("rejf-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()
        j = Job(source=JobSource.REMOTEOK, external_id="rejf-1", company="RejFlowCo",
                title="Rejectable Role", url="http://r", description="x", rerank_score=70)
        s.add(j); s.commit(); s.refresh(j)
        a = Application(job_id=j.id, status=status, apply_track="manual",
                        submitted_at=datetime.utcnow())
        s.add(a); s.commit(); s.refresh(a)
        return a.id


def _cleanup():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("rejf-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_reject_endpoint_sets_status():
    aid = _seed(ApplicationStatus.SUBMITTED)
    try:
        r = _client().post(f"/application/{aid}/reject")
        assert r.status_code == 200 and r.json()["success"]
        with get_session() as s:
            assert s.get(Application, aid).status == ApplicationStatus.REJECTED
    finally:
        _cleanup()


def test_dashboard_shows_rejected_section():
    _seed(ApplicationStatus.REJECTED)
    try:
        html = _client().get("/dashboard").text
        assert ">Rejected<" in html
        assert "Rejectable Role" in html      # the rejected card renders
        assert "Removed / Cancelled" in html  # separate section still present
    finally:
        _cleanup()
