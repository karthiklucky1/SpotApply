"""Verify the dashboard ranks shortlisted roles by blended priority score,
and that the JSON endpoints expose the hire-probability / blended fields.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, Application, ApplicationStatus


@pytest.fixture
def _seeded_jobs():
    """Two shortlisted jobs: A has higher raw fit but low hiring intent;
    B has lower fit but strong hiring intent → B's blended score wins."""
    created = []
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("prio-%"))).all():
            s.delete(j)
        s.commit()

        jA = Job(source=JobSource.GREENHOUSE, external_id="prio-A", company="AlphaCorp",
                 title="ML Engineer", url="http://a", description="x",
                 rerank_score=80, hire_probability_score=0.1,
                 blended_score=round(0.65 * 80 + 0.35 * 10, 1), posted_at=datetime(2026, 1, 1))
        jB = Job(source=JobSource.LEVER, external_id="prio-B", company="BetaAI",
                 title="AI Engineer", url="http://b", description="x",
                 rerank_score=72, hire_probability_score=0.9,
                 blended_score=round(0.65 * 72 + 0.35 * 90, 1), posted_at=datetime(2026, 6, 1))
        s.add(jA); s.add(jB); s.commit(); s.refresh(jA); s.refresh(jB)
        s.add(Application(job_id=jA.id, status=ApplicationStatus.SHORTLISTED, apply_track="autofill"))
        s.add(Application(job_id=jB.id, status=ApplicationStatus.SHORTLISTED, apply_track="autofill"))
        s.commit()
        created = [jA.id, jB.id]
    yield
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("prio-%"))).all():
            s.delete(j)
        s.commit()


def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def test_dashboard_orders_by_blended_priority(_seeded_jobs):
    html = _client().get("/dashboard").text
    iA, iB = html.find("AlphaCorp"), html.find("BetaAI")
    assert iB != -1 and iA != -1
    # BetaAI (blended 78.3) must appear before AlphaCorp (blended 55.5),
    # even though AlphaCorp has the higher raw rerank score.
    assert iB < iA


def test_dashboard_renders_hiring_badges(_seeded_jobs):
    html = _client().get("/dashboard").text
    assert "Hiring" in html       # hiring-intent badge text
    assert "★" in html            # blended priority star badge


def test_api_jobs_exposes_new_fields(_seeded_jobs):
    data = _client().get("/api/jobs?limit=10").json()
    assert data["jobs"], "expected at least one job"
    top = data["jobs"][0]
    assert "blended" in top
    assert "hire_probability" in top
    # highest blended should sort first
    assert top["company"] == "BetaAI"
