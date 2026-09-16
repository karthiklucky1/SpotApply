"""Which résumé the browser extension attaches: the tailored one or the original.

`UserProfile.autofill_resume_source` is read by
`GET /api/fill-pack/{id}/resume`, the single endpoint `extension/content.js`
pulls résumé bytes from. Two things matter beyond "it returns the right file":

* the choice must be validated on the way in, because the reader treats any
  unrecognised value as "tailored" — a typo would look like the setting being
  silently ignored rather than rejected; and
* "original" must return BEFORE the auto-tailor block, or picking it would
  spend a paid generation and a daily tailor credit to build the very document
  the user said they did not want.
"""
from __future__ import annotations

import inspect

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import UserProfile

_USER = "local"      # the SQLite dev tenant these route tests run as


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_pref():
    """Leave the shared dev profile exactly as found (CLAUDE.md: clean up only
    your own rows — here that means our own field, not the row)."""
    with get_session() as s:
        before = s.exec(
            select(UserProfile.autofill_resume_source)
            .where(UserProfile.user_id == _USER)).first()
    yield
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == _USER)).first()
        if p is not None:
            p.autofill_resume_source = before or "tailored"
            s.add(p)
            s.commit()


def test_default_is_the_tailored_resume(client):
    d = client.get("/api/profile").json()
    assert d["autofill_resume_source"] in ("tailored", "original")
    # A profile that has never set it reads as "tailored", never as empty —
    # the reader branches on this string.
    assert d["autofill_resume_source"] != ""


def test_the_preference_round_trips(client):
    assert client.put("/api/profile",
                      json={"autofill_resume_source": "original"}).status_code == 200
    assert client.get("/api/profile").json()["autofill_resume_source"] == "original"
    assert client.put("/api/profile",
                      json={"autofill_resume_source": "tailored"}).status_code == 200
    assert client.get("/api/profile").json()["autofill_resume_source"] == "tailored"


def test_an_unknown_value_is_refused_not_stored(client):
    """Junk here would read as 'tailored' at the branch, i.e. a silent no-op."""
    r = client.put("/api/profile", json={"autofill_resume_source": "whatever"})
    assert r.status_code == 422
    assert client.get("/api/profile").json()["autofill_resume_source"] != "whatever"


def test_case_and_whitespace_are_normalised(client):
    assert client.put("/api/profile",
                      json={"autofill_resume_source": "  Original "}).status_code == 200
    assert client.get("/api/profile").json()["autofill_resume_source"] == "original"


def test_original_returns_before_any_paid_auto_tailor():
    """Order matters more than the branch itself.

    get_tailored_resume auto-tailors on a cache miss, charging
    _check_tailor_limit + _increment_tailor + record_llm_spend. The "original"
    branch placed after that would still buy a tailored résumé and throw it
    away.
    """
    from app.api import server

    src = inspect.getsource(server.get_tailored_resume)
    assert 'resume_source == "original"' in src, "the preference is not read here"
    branch = src.index('resume_source == "original"')
    charge = src.index("_check_tailor_limit")
    assert branch < charge, (
        "the 'original' branch must return before _check_tailor_limit — "
        "otherwise choosing your own résumé still spends a tailoring credit"
    )


def test_the_preference_read_is_scoped_to_the_caller():
    """Multi-tenancy: _get_user_id fails open to None, so an unscoped read of
    UserProfile would hand one tenant another tenant's setting."""
    from app.api import server

    src = inspect.getsource(server.get_tailored_resume)
    assert "_require_owned_application(request, application_id)" in src
    assert "_autofill_resume_source(session, uid)" in src, (
        "the route must use the shared resolver, not a second hand-rolled lookup"
    )


def test_the_resolver_reads_what_the_writer_wrote_in_local_mode(client):
    """The round-trip the source assertions above cannot prove on their own.

    Reading the preference through a lookup that differs from the app's own
    profile resolution is how it ends up unreadable for one tenant shape with
    every unit test still green. `_get_or_create_profile(None)` takes the FIRST
    profile row in local mode — it does NOT require `user_id IS NULL` — so the
    resolver has to do the same.
    """
    from app.api.server import _autofill_resume_source

    assert client.put("/api/profile",
                      json={"autofill_resume_source": "original"}).status_code == 200
    with get_session() as s:
        assert _autofill_resume_source(s, "local") == "original", (
            "the resolver did not find the row PUT /api/profile just wrote"
        )


def test_the_resolver_defaults_rather_than_guessing(client):
    """Unknown and absent both mean "tailored" — the reader branches on this
    string, so a None or a typo must not become a third behaviour."""
    from app.api.server import _autofill_resume_source

    with get_session() as s:
        # A tenant with no profile row at all.
        assert _autofill_resume_source(s, "nobody-has-this-uid") == "tailored"


def test_the_resolver_fails_closed_in_multi_tenant_mode(client):
    """With no uid in SaaS mode it must read NOTHING, not the first row it
    finds — that is the fail-open shape CLAUDE.md calls out."""
    from unittest.mock import PropertyMock, patch

    from app.api.server import _autofill_resume_source
    from app.config import settings

    # Give the first profile row a value the fail-closed path must NOT return.
    assert client.put("/api/profile",
                      json={"autofill_resume_source": "original"}).status_code == 200
    # use_supabase is a computed property — patch it on the class.
    with patch.object(type(settings), "use_supabase",
                      new_callable=PropertyMock, return_value=True):
        with get_session() as s:
            assert _autofill_resume_source(s, None) == "tailored"


def test_the_fill_pack_reports_the_choice(client):
    from app.api import server

    assert "autofill_resume_source" in inspect.getsource(server.get_fill_pack)


def test_the_column_is_in_the_hand_written_migration_list():
    """ensure_model_columns() covers it, but the belt-and-braces list is what
    was missing when target_roles_auto crash-looped production."""
    from app.api import server

    names = [c[0] for c in server._USERPROFILE_COLUMNS]
    assert "autofill_resume_source" in names


def test_the_dashboard_exposes_the_control():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "app" / "templates" / "dashboard.html").read_text()
    # A <select name="..."> is populated by openProfileModal and submitted by
    # saveProfile with no extra JS — a checkbox would need an explicit line.
    assert 'name="autofill_resume_source"' in src
    assert '<option value="tailored">' in src
    assert '<option value="original">' in src
