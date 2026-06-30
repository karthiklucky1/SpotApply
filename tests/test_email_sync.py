"""Tests for the extension email sync endpoint: /api/sync-emails."""
from __future__ import annotations

from datetime import datetime
import json
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, Application, ApplicationStatus


def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _seed():
    with get_session() as s:
        # Cleanup past test records
        for j in s.exec(select(Job).where(Job.external_id.like("sync-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()

        # Seed a test job and application
        j = Job(
            source=JobSource.REMOTEOK,
            external_id="sync-test-1",
            company="Acme Corp",
            title="Software Engineer",
            url="http://acme",
            description="Loves Python, Kubernetes, and FastAPI",
            rerank_score=80
        )
        s.add(j)
        s.commit()
        s.refresh(j)

        a = Application(
            job_id=j.id,
            status=ApplicationStatus.SUBMITTED,
            apply_track="manual",
            submitted_at=datetime.utcnow()
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        return a.id, j.id


def _cleanup():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("sync-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_sync_emails_rejection_detection():
    app_id, _ = _seed()
    try:
        payload = {
            "emails": [
                {
                    "subject": "Your application to Acme Corp",
                    "sender": "jobs@acme.com",
                    "body": "Unfortunately, we have decided to move forward with other candidates.",
                    "date": "2026-06-30",
                    "company_guess": "Acme Corp"
                }
            ]
        }
        r = _client().post("/api/sync-emails", json=payload)
        assert r.status_code == 200
        res = r.json()
        assert res["success"] is True
        assert res["processed"] == 1
        assert res["matched"] == 1
        assert res["rejections"] == 1
        assert res["interviews"] == 0
        assert res["unmatched"] == 0

        # Verify application status was updated to REJECTED
        with get_session() as s:
            app_obj = s.get(Application, app_id)
            assert app_obj.status == ApplicationStatus.REJECTED
            assert "Auto-detected rejection" in app_obj.notes
    finally:
        _cleanup()


def test_sync_emails_interview_detection():
    app_id, _ = _seed()
    try:
        payload = {
            "emails": [
                {
                    "subject": "Acme Corp Interview Invitation",
                    "sender": "hiring@acme.com",
                    "body": "We would love to schedule a call or interview to discuss next steps.",
                    "date": "2026-06-30",
                    "company_guess": "Acme Corp"
                }
            ]
        }
        r = _client().post("/api/sync-emails", json=payload)
        assert r.status_code == 200
        res = r.json()
        assert res["success"] is True
        assert res["processed"] == 1
        assert res["matched"] == 1
        assert res["rejections"] == 0
        assert res["interviews"] == 1
        assert res["unmatched"] == 0

        # Verify application status was updated to INTERVIEWING
        with get_session() as s:
            app_obj = s.get(Application, app_id)
            assert app_obj.status == ApplicationStatus.INTERVIEWING
            assert "Auto-detected interview signal" in app_obj.notes
    finally:
        _cleanup()


def _cleanup_email_imports():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("email:%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_sync_emails_no_company_guess_is_unmatched():
    """Emails with no company guess can't be tracked, so they stay unmatched."""
    _seed()
    try:
        payload = {
            "emails": [
                {
                    "subject": "Random Email",
                    "sender": "newsletter@gmail.com",
                    "body": "Check out these top links from the community.",
                    "date": "2026-06-30",
                    "company_guess": ""
                }
            ]
        }
        r = _client().post("/api/sync-emails", json=payload)
        assert r.status_code == 200
        res = r.json()
        assert res["success"] is True
        assert res["processed"] == 1
        assert res["matched"] == 0
        assert res["imported"] == 0
        assert res["unmatched"] == 1
    finally:
        _cleanup()


def test_sync_emails_imports_unmatched_as_tracked():
    """A job email for a company with no existing application is imported
    as a tracked application so it surfaces in the dashboard."""
    _seed()
    try:
        payload = {
            "emails": [
                {
                    "subject": "Thank you for applying to NimbusAI - Backend Engineer",
                    "sender": "no-reply@nimbusai.com",
                    "body": "We received your application and will review it shortly.",
                    "date": "2026-06-30",
                    "company_guess": "NimbusAI"
                }
            ]
        }
        r = _client().post("/api/sync-emails", json=payload)
        assert r.status_code == 200
        res = r.json()
        assert res["success"] is True
        assert res["matched"] == 0
        assert res["imported"] == 1
        assert res["unmatched"] == 0

        # Verify a tracked application now exists
        with get_session() as s:
            j = s.exec(select(Job).where(Job.company == "NimbusAI")).first()
            assert j is not None
            assert j.source == JobSource.MANUAL
            a = s.exec(select(Application).where(Application.job_id == j.id)).first()
            assert a is not None
            assert a.apply_track == "email_import"
            assert a.status == ApplicationStatus.SUBMITTED

        # Re-syncing the same email must NOT create a duplicate
        r2 = _client().post("/api/sync-emails", json=payload)
        assert r2.json()["imported"] == 0
        with get_session() as s:
            jobs = s.exec(select(Job).where(Job.company == "NimbusAI")).all()
            assert len(jobs) == 1
    finally:
        _cleanup_email_imports()
        _cleanup()
