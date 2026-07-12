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


def test_dashboard_fresh_jobs_lead_shortlist():
    """A job posted today must appear ABOVE a higher-scoring week-old job —
    fresh first is the default; priority only ranks within the same day."""
    from datetime import timedelta
    from sqlmodel import select as _select
    now = datetime.utcnow()
    with get_session() as s:
        for j in s.exec(_select(Job).where(Job.external_id.like("freshfirst-%"))).all():
            for a in s.exec(_select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()
        old_high = Job(source=JobSource.GREENHOUSE, external_id="freshfirst-old",
                       company="OldHighCo", title="Staff ML Engineer", url="http://oh",
                       description="x", rerank_score=95, blended_score=95,
                       posted_at=now - timedelta(days=6))
        new_low = Job(source=JobSource.GREENHOUSE, external_id="freshfirst-new",
                      company="NewLowCo", title="ML Engineer", url="http://nl",
                      description="x", rerank_score=60, blended_score=60,
                      posted_at=now - timedelta(hours=2))
        s.add(old_high); s.add(new_low); s.commit()
        s.refresh(old_high); s.refresh(new_low)
        s.add(Application(job_id=old_high.id, status=ApplicationStatus.SHORTLISTED, apply_track="autofill"))
        s.add(Application(job_id=new_low.id, status=ApplicationStatus.SHORTLISTED, apply_track="autofill"))
        s.commit()

    html = _client().get("/dashboard").text
    i_new, i_old = html.find("NewLowCo"), html.find("OldHighCo")
    assert i_new != -1 and i_old != -1
    assert i_new < i_old, "today's posting must render above the week-old high scorer"

    with get_session() as s:
        for j in s.exec(_select(Job).where(Job.external_id.like("freshfirst-%"))).all():
            for a in s.exec(_select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


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


def test_dashboard_caps_two_per_company():
    """Three shortlisted roles at the same company → dashboard shows at most 2."""
    from sqlmodel import select as _select
    with get_session() as s:
        for j in s.exec(_select(Job).where(Job.external_id.like("capdisp-%"))).all():
            for a in s.exec(_select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()
        ids = []
        for i in range(3):
            j = Job(source=JobSource.GREENHOUSE, external_id=f"capdisp-{i}",
                    company="FloodCorp", title=f"Engineer {i}", url=f"http://f/{i}",
                    description="x", rerank_score=70 + i, blended_score=70 + i)
            s.add(j); s.commit(); s.refresh(j)
            s.add(Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, apply_track="autofill"))
            ids.append(j.id)
        s.commit()

    html = _client().get("/dashboard").text
    # Only the 2 highest-priority roles (Engineer 2, Engineer 1) should render;
    # the lowest (Engineer 0) is dropped by the per-company cap.
    shown = sum(1 for i in range(3) if f"Engineer {i}" in html)
    assert shown == 2, f"shortlist must cap to 2 roles per company, showed {shown}"
    assert "Engineer 0" not in html  # lowest priority dropped

    with get_session() as s:
        for j in s.exec(_select(Job).where(Job.external_id.like("capdisp-%"))).all():
            for a in s.exec(_select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_api_jobs_excludes_closed():
    """Purged (is_closed) jobs must not appear in the Jobs table API."""
    from sqlmodel import select as _select
    with get_session() as s:
        for j in s.exec(_select(Job).where(Job.external_id.like("closed-%"))).all():
            s.delete(j)
        s.commit()
        s.add(Job(source=JobSource.GREENHOUSE, external_id="closed-open", company="OpenCo",
                  title="Open Role", url="http://o", description="x", rerank_score=75,
                  blended_score=75, is_closed=False))
        s.add(Job(source=JobSource.GREENHOUSE, external_id="closed-shut", company="ShutCo",
                  title="Closed Role", url="http://c", description="x", rerank_score=75,
                  blended_score=75, is_closed=True))
        s.commit()

    data = _client().get("/api/jobs?limit=500").json()
    companies = {j["company"] for j in data["jobs"]}
    assert "OpenCo" in companies
    assert "ShutCo" not in companies

    with get_session() as s:
        for j in s.exec(_select(Job).where(Job.external_id.like("closed-%"))).all():
            s.delete(j)
        s.commit()


def test_empty_company_jobs_not_collapsed():
    """Jobs with blank company must NOT be capped together — each distinct role
    should render (reproduces the '19 shortlisted but only 2 shown' bug)."""
    from sqlmodel import select as _select
    with get_session() as s:
        for j in s.exec(_select(Job).where(Job.external_id.like("blankco-%"))).all():
            for a in s.exec(_select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()
        for i in range(5):
            j = Job(source=JobSource.REMOTEOK, external_id=f"blankco-{i}", company="",
                    title=f"Remote Role {i}", url=f"http://r/{i}", description="x",
                    rerank_score=70 + i, blended_score=70 + i)
            s.add(j); s.commit(); s.refresh(j)
            s.add(Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, apply_track="manual"))
        s.commit()

    html = _client().get("/dashboard").text
    shown = sum(1 for i in range(5) if f"Remote Role {i}" in html)
    assert shown == 5, f"all 5 blank-company roles should show, showed {shown}"

    with get_session() as s:
        for j in s.exec(_select(Job).where(Job.external_id.like("blankco-%"))).all():
            for a in s.exec(_select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()
