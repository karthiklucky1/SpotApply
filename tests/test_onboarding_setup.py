"""After a resume upload: review profile → target roles → the search starts.

The owner's flow (2026-09-30): a new resume opens a setup dialog that shows
what was read from it and what is still missing (phone, city, work
authorization, its end date), then the target roles, and only then does the
search start — from checked data instead of a guess. Also: demographic answers
the user saved in SpotApply are theirs to have filled on forms.
Synthetic users and rows only.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlmodel import delete, select

from app.api import server
from app.db.init_db import get_session
from app.db.models import UserProfile

HTML = (Path(__file__).resolve().parents[1] / "app/templates/dashboard.html").read_text()


def _function(name: str) -> str:
    m = re.search(r"(async\s+)?function\s+" + name + r"\s*\(", HTML)
    assert m, name
    depth, i = 0, HTML.index("{", m.end())
    for j in range(i, len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[j], 0)
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


# ── the page ─────────────────────────────────────────────────────────────────

def test_a_new_resume_opens_the_review_step():
    assert "openProfileReview(" in _function("_afterResumeChange")
    assert "openProfileReview(" in _function("obUploadResume")


def test_the_review_shows_what_is_missing_and_saves_before_the_roles():
    body = _function("openProfileReview")
    for need in ("phone", "city, state", "work authorization", "authorization end date"):
        assert need in body, need
    nxt = body[body.index("data-pr-next"):]
    assert nxt.index("/api/profile") < nxt.index("openRolesModal({ onboarding: true })")


def test_saving_the_roles_starts_the_search_once():
    save = _function("saveRoles")
    assert "welcomeStarting('finding')" in save
    assert save.index("_rolesOnboarding = false") < save.index("closeRolesModal()")
    # Closing the roles step without saving still starts it with what we have.
    assert "_startSearchNow()" in _function("closeRolesModal")
    assert "_startSearchNow()" in _function("openProfileReview")    # "Later"


def test_the_setup_uploads_defer_the_search_and_the_landing_import_does_not():
    for name in ("uploadResume", "obUploadResume"):
        assert "extract-profile?defer_search=1" in _function(name), name
    assert "extract-profile?defer_search=1" not in _function("checkAndApplyTempData")


# ── the server ───────────────────────────────────────────────────────────────

_U = "ob-setup-user"


@pytest.fixture
def profile():
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id == _U))
        s.add(UserProfile(user_id=_U, first_name="Ob", email="ob@example.invalid"))
        s.commit()
    yield
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id == _U))
        s.commit()


def test_extract_profile_can_defer_the_search():
    import inspect
    src = inspect.getsource(server.extract_profile_from_resume)
    assert "defer_search: bool = False" in src
    assert src.index("if not defer_search:") < src.index("add_task(seed_new_user")


def test_saving_demographic_answers_confirms_them(profile, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "_get_user_id", lambda request: _U)
    r = TestClient(server.app).put("/api/profile", json={"phone": "555"})
    assert r.status_code == 200, r.text
    with get_session() as s:
        assert s.exec(select(UserProfile.eeo_confirmed).where(UserProfile.user_id == _U)).first() is False
    r = TestClient(server.app).put("/api/profile", json={"veteran_status": "I am not a protected veteran"})
    assert r.status_code == 200, r.text
    with get_session() as s:
        assert s.exec(select(UserProfile.eeo_confirmed).where(UserProfile.user_id == _U)).first() is True


def test_a_confirmed_legacy_value_is_the_users_answer():
    v = "I am not a protected veteran"
    assert server._eeo_answer(v) == "Decline to self-identify"
    assert server._eeo_answer(v, True) == v
    assert server._eeo_answer("", True) == "Decline to self-identify"
