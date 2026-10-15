"""The one-time review request (founder, 2026-10-15).

After a user's first application submitted through SpotApply the dashboard
asks once for a review. Showing it is recorded on the server, so whether they
review or close it with ×, it is never asked again — on any device. Someone
who already wrote a review is never asked, and an application imported from
email (applied outside SpotApply) does not count.

Rows this file writes carry the ``reviewasktest-`` prefix and are removed by it.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete, select

from app.api import server
from app.db.init_db import get_session
from app.db.models import (Application, ApplicationStatus, Job, JobSource,
                           UserProfile, UserReview)

_UID = "reviewasktest-user"
DASH = (Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html").read_text()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "_require_user", lambda request: _UID)
    with get_session() as s:
        s.add(UserProfile(user_id=_UID, first_name="Rae"))
        s.commit()
    yield TestClient(server.app)
    with get_session() as s:
        s.exec(delete(UserReview).where(UserReview.user_id == _UID))
        s.exec(delete(Application).where(Application.user_id == _UID))
        s.exec(delete(Job).where(Job.user_id == _UID))
        s.exec(delete(UserProfile).where(UserProfile.user_id == _UID))
        s.commit()


def _apply(track="autofill", submitted=True):
    with get_session() as s:
        job = Job(user_id=_UID, source=JobSource.GREENHOUSE,
                  external_id=f"reviewasktest-{track}-{submitted}", company="Acme",
                  title="Engineer", url="https://x/rat", description="A role.")
        s.add(job)
        s.commit()
        s.refresh(job)
        s.add(Application(user_id=_UID, job_id=job.id, apply_track=track,
                          status=ApplicationStatus.SUBMITTED if submitted else ApplicationStatus.SHORTLISTED,
                          submitted_at=datetime.utcnow() if submitted else None))
        s.commit()


def test_not_asked_before_any_application(client):
    _apply(submitted=False)
    assert client.get("/api/review-prompt").json() == {"show": False}


def test_asked_after_the_first_application_and_never_again(client):
    _apply()
    assert client.get("/api/review-prompt").json() == {"show": True}
    assert client.post("/api/review-prompt/seen").status_code == 200
    assert client.get("/api/review-prompt").json() == {"show": False}
    # A second application does not bring it back.
    _apply(track="manual")
    assert client.get("/api/review-prompt").json() == {"show": False}
    with get_session() as s:
        first = s.exec(select(UserProfile).where(UserProfile.user_id == _UID)).one().review_prompted_at
    client.post("/api/review-prompt/seen")
    with get_session() as s:
        assert s.exec(select(UserProfile).where(UserProfile.user_id == _UID)).one().review_prompted_at == first


def test_someone_who_already_reviewed_is_not_asked(client):
    _apply()
    with get_session() as s:
        s.add(UserReview(user_id=_UID, user_name="Rae", rating=5, content="Great", is_public=False))
        s.commit()
    assert client.get("/api/review-prompt").json() == {"show": False}


def test_an_email_import_is_not_an_application_through_spotapply(client):
    _apply(track="email_import")
    assert client.get("/api/review-prompt").json() == {"show": False}


def test_showing_it_is_not_counted_as_the_user_being_active():
    assert "/api/review-prompt/seen" in server._PASSIVE_WRITE_PATHS


def test_the_dashboard_asks_on_load_and_after_each_submit_but_shows_once():
    assert "document.addEventListener('DOMContentLoaded', () => maybeAskForReview(4000));" in DASH
    assert DASH.count("if (window.maybeAskForReview) maybeAskForReview(1500);") == 3
    block = DASH[DASH.index("window.maybeAskForReview = async function"):][:1600]
    assert "_reviewAskShown = true;" in block and "openReviewModal({ prompted: true });" in block
    assert block.index("_reviewAskShown = true;") < block.index("/api/review-prompt/seen")
    assert 'id="review-modal-title"' in DASH and 'aria-label="Close"' in DASH
