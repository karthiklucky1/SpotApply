"""Public profiles and recruiter search are OPT-IN (2026-10).

Every user used to get a public page at /u/<handle> — name, title, city, work
authorisation, whether they need sponsorship, salary range, relocation plans —
minted the first time the dashboard loaded, and every user with a trust tier
was returned by recruiter search. Nobody was asked, and the handle's 4-hex
suffix (65,536 per name) could be enumerated. Now both follow ONE setting,
`UserProfile.public_profile`, off by default.

Rows this file writes carry the ``visibilitytest-`` prefix and are removed by it.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete, select

from app.api import server
from app.db.init_db import get_session
from app.db.models import UserProfile

_P = "visibilitytest-"


@pytest.fixture()
def people():
    rows = {
        "private": UserProfile(user_id=_P + "private", first_name="Pri", last_name="Vate",
                               public_handle=_P + "private-handle", trust_tier="Verified",
                               current_title="Backend Engineer", key_skills="Python",
                               salary_min=150000, work_authorization="H-1B"),
        "public": UserProfile(user_id=_P + "public", first_name="Pub", last_name="Lic",
                              public_handle=_P + "public-handle", trust_tier="Verified",
                              current_title="Backend Engineer", key_skills="Python",
                              public_profile=True),
    }
    with get_session() as s:
        for r in rows.values():
            s.add(r)
        s.commit()
    yield rows
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id.like(_P + "%")))
        s.commit()


def test_the_setting_defaults_to_private():
    assert UserProfile().public_profile is False
    assert "public_profile" in server.ProfileUpdate.model_fields


def test_a_private_profile_page_does_not_exist_to_the_world(people):
    c = TestClient(server.app)
    r = c.get(f"/u/{_P}private-handle", headers={"Accept": "text/html"})
    assert r.status_code == 404
    assert "Pri Vate" not in r.text and "H-1B" not in r.text
    # Same answer as a handle that was never minted, so handles can't be probed.
    assert c.get(f"/u/{_P}no-such-handle").status_code == 404


def test_a_profile_the_user_made_public_is_shown(people):
    r = TestClient(server.app).get(f"/u/{_P}public-handle")
    assert r.status_code == 200 and "Pub Lic" in r.text


def test_recruiter_search_returns_only_people_who_chose_to_be_found(people, monkeypatch):
    r = TestClient(server.app).post("/api/recruiter/search",
                                    json={"job_description": "Python backend engineer"})
    assert r.status_code == 200, r.text
    ids = {row["candidate_user_id"] for row in r.json()["results"]}
    assert _P + "public" in ids
    assert _P + "private" not in ids


def test_the_search_query_itself_filters_on_the_setting():
    import inspect
    src = inspect.getsource(server.recruiter_search)
    assert "UserProfile.public_profile == True" in src


def test_new_handles_carry_forty_random_bits(people):
    with get_session() as s:
        p = UserProfile(user_id=_P + "fresh", first_name="New", last_name="Person")
        s.add(p)
        s.commit()
        s.refresh(p)
        handle = server._ensure_public_handle(p, s)
    assert re.fullmatch(r"new-person-[0-9a-f]{10}", handle)


def test_the_trust_payload_only_shares_a_link_for_a_public_profile():
    import inspect
    src = inspect.getsource(server.get_trust_profile)
    assert '"public_profile"' in src
    assert 'getattr(profile, "public_profile", False)' in src


# ── reviews are published only with consent ─────────────────────────────────

@pytest.fixture()
def reviewer(monkeypatch):
    from app.db.models import UserReview
    monkeypatch.setattr(server, "_require_user", lambda request: _P + "reviewer")
    yield TestClient(server.app)
    with get_session() as s:
        s.exec(delete(UserReview).where(UserReview.user_id == _P + "reviewer"))
        s.commit()


def _reviews():
    from app.db.models import UserReview
    with get_session() as s:
        return s.exec(select(UserReview).where(UserReview.user_id == _P + "reviewer")).all()


def test_a_review_stays_private_unless_the_reviewer_ticks_public(reviewer):
    assert reviewer.post("/api/reviews", json={"rating": 4, "content": "Useful."}).status_code == 200
    assert reviewer.post("/api/reviews", json={"rating": 5, "content": "Great.",
                                                "public": True}).status_code == 200
    rows = sorted(_reviews(), key=lambda r: r.id)
    assert [r.is_public for r in rows] == [False, True]


def test_a_review_needs_a_rating_the_reviewer_chose(reviewer):
    assert reviewer.post("/api/reviews", json={"content": "No stars picked."}).status_code == 400
    assert _reviews() == []
