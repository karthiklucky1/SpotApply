"""The user journey is measured from actions, never from polling, and carries
no personal data (audit 2026-09-25, findings 12 and 14). Synthetic users."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.analytics import journey
from app.db.init_db import get_session
from app.db.models import FunnelEvent


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        with get_session() as s:
            s.exec(delete(FunnelEvent).where(FunnelEvent.stage.in_(
                [journey.STAGE, "contact_research"])))
            s.commit()
    wipe()
    yield
    wipe()


def _rows():
    with get_session() as s:
        return s.exec(select(FunnelEvent).where(FunnelEvent.stage == journey.STAGE)).all()


def test_the_user_key_is_not_the_user_id():
    k = journey.user_key("user-123@example.invalid")
    assert "user-123" not in k and len(k) == 16
    assert k == journey.user_key("user-123@example.invalid")


def test_once_only_milestones_and_daily_active():
    assert journey.record("jr_u1", "signup")
    assert not journey.record("jr_u1", "signup"), "once per user, ever"
    d = datetime(2026, 9, 26, 10)
    assert journey.record("jr_u1", "active_day", day=d)
    assert not journey.record("jr_u1", "active_day", day=d + timedelta(hours=5))
    assert journey.record("jr_u1", "active_day", day=d + timedelta(days=1))
    for r in _rows():
        assert "jr_u1" not in (r.reason or "") and "jr_u1" not in (r.metadata_json or "")


def test_internal_accounts_and_unknown_labels_are_not_recorded(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "internal_user_ids", "staff-1, qa-2", raising=False)
    assert not journey.record("staff-1", "signup")
    assert not journey.record("jr_u2", "not_a_milestone")
    assert not journey.record("jr_u2", "outcome_recorded", outcome="free text here")
    assert journey.record("jr_u2", "outcome_recorded", outcome="interviewing")
    meta = json.loads(_rows()[-1].metadata_json)
    assert meta == {"outcome": "interviewing"}


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/application/12/viewed", "job_opened"),
    ("POST", "/run/tailor/12", "resume_requested"),
    ("GET", "/application/12/review", "review_opened"),
    ("GET", "/application/12/download-resume", "document_downloaded"),
    ("POST", "/application/12/submit", "application_recorded"),
    ("POST", "/api/search/resume", "search_resumed"),
    ("GET", "/api/pipeline/live", None),         # polling never counts
    ("GET", "/api/notifications", None),
    ("GET", "/dashboard", None),                 # a page view is not a milestone
    ("GET", "/application/12/viewed", None),     # wrong method
])
def test_only_action_routes_map_to_milestones(method, path, expected):
    assert journey.milestone_for(method, path) == expected


def test_summary_counts_people_and_returns():
    t0 = datetime.utcnow() - timedelta(days=10)
    journey.record("jr_a", "signup", day=t0)
    journey.record("jr_b", "signup", day=t0)
    journey.record("jr_a", "active_day", day=t0 + timedelta(days=3))
    journey.record("jr_a", "first_shortlist", day=t0 + timedelta(days=1))
    out = journey.summary(30)
    assert out["distinct_users"]["signup"] == 2
    assert out["distinct_users"]["first_shortlist"] == 1
    assert out["returns"]["day_3"] == {"cohort": 2, "returned": 1}
    assert "interview probability" in out["note"]


def test_the_middleware_records_a_milestone_for_a_real_action(monkeypatch):
    """End to end through the app (local mode): opening a job records it."""
    from fastapi.testclient import TestClient
    from app.api.server import app
    from app.db.models import Application, ApplicationStatus, Job, JobSource
    with get_session() as s:
        j = Job(user_id=None, source=JobSource.GREENHOUSE, external_id="jr_job",
                company="Jr", title="Engineer", url="u", description="d")
        s.add(j)
        s.commit()
        s.refresh(j)
        a = Application(job_id=j.id, user_id=None, status=ApplicationStatus.SHORTLISTED)
        s.add(a)
        s.commit()
        s.refresh(a)
        jid, aid = j.id, a.id
    try:
        c = TestClient(app)
        assert c.post(f"/application/{aid}/viewed").status_code == 200
        c.get("/api/pipeline/live")
        names = [r.reason.split(":")[0] for r in _rows()]
        assert "job_opened" in names
        assert len(names) == 1, "the poll recorded nothing"
    finally:
        with get_session() as s:
            s.exec(delete(Application).where(Application.id == aid))
            s.exec(delete(Job).where(Job.id == jid))
            s.commit()


def test_contact_research_observations_persist_without_identifiers(monkeypatch):
    from app.config import settings
    from app.analytics.research_log import persisted_summary
    from app.intelligence import contact_research as cr
    monkeypatch.setattr(settings, "research_log_sync", True, raising=False)
    stage = next(iter(cr.STAGES))
    cr.record(stage, cr.EMPTY, elapsed_ms=812, results=0, provider_call=True)
    cr.record(stage, cr.OK, elapsed_ms=400, results=3, provider_call=True)
    out = persisted_summary(30)["stages"][stage]
    assert out["by_outcome"] == {"empty": 1, "ok": 1}
    assert out["provider_calls"] == 2 and out["results_total"] == 3
    assert out["verified_contacts"] == 0
    assert out["latency_ms"]["n"] == 2
