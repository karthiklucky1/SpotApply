"""Paused means paused: no welcome refresh, no boost, no "Checking jobs".

Reported 2026-10-08 with two screenshots: the board said "You paused your
search", and directly above it the first-hour panel said "Checking jobs against
your resume · Priority scoring · 58 min left", 394 still to check. The lanes'
user lists already skipped PAUSED (compute_policy). The welcome machinery asked
nobody — and clicking "Pause my search" is itself a POST, which the request
layer stamps BEFORE the route body runs, so for someone idle past the window
the very click that asked us to stop opened a boost that adopted and scored.

Synthetic users only; rows prefixed ``ps-`` and removed by that prefix.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.api import server
from app.common import compute_policy as cp
from app.common import ttl_cache
from app.config import settings
from app.db.init_db import get_session
from app.db.models import UserProfile
from app.strategy import adoption, welcome

# The pause is a compute-policy state: enforce it (conftest turns it off).
pytestmark = pytest.mark.compute_policy

_P = "ps-"
UID = _P + "returner"


def _wipe():
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
        s.commit()


def _reset_welcome():
    for d in (welcome._BOOSTS, welcome._GRADUATED, welcome._LAST_CHECK, welcome._FIRST_DONE):
        d.clear()


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(settings, "welcome_boost_minutes", 60)
    monkeypatch.setattr(settings, "welcome_boost_cap_multiplier", 2.0)
    _reset_welcome()
    server._LAST_ACTIVE_STAMP.clear()
    _wipe()
    yield
    _reset_welcome()
    server._LAST_ACTIVE_STAMP.clear()
    ttl_cache.invalidate(f"welcome:{UID}")
    _wipe()


def _returner(paused: bool = False) -> str:
    """Has roles, last acted 40 days ago: any click is a welcome-back."""
    with get_session() as s:
        s.add(UserProfile(
            user_id=UID, target_roles="Software Engineer, Backend Engineer",
            years_experience=3,
            last_meaningful_activity_at=datetime.utcnow() - timedelta(days=40),
            search_paused_at=datetime.utcnow() - timedelta(hours=2) if paused else None))
        s.commit()
    return UID


def _paused_at(uid: str):
    with get_session() as s:
        return s.exec(select(UserProfile.search_paused_at).where(
            UserProfile.user_id == uid)).first()


def _req(method: str, path: str):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path),
                           headers={"Authorization": "Bearer t"}, cookies={})


@pytest.fixture
def welcomed(monkeypatch):
    """Record welcome-backs instead of starting a real refresh thread."""
    got = []
    monkeypatch.setattr(welcome, "welcome_back", lambda uid: got.append(uid) or True)
    return got


# ── the click that asked us to stop ──────────────────────────────────────────

def test_clicking_pause_never_opens_a_welcome(welcomed, monkeypatch):
    """End to end through the request layer, in the route's own order: the
    stamp in `_get_user_id` runs first, then `_set_search_pause`."""
    uid = _returner()
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True))
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "get_user_id_from_token", lambda tok: uid)

    assert server._get_user_id(_req("POST", "/api/search/pause")) == uid
    server._set_search_pause(uid, True)

    assert welcomed == [], "the Pause click itself started a welcome refresh"
    assert _paused_at(uid) is not None


@pytest.mark.parametrize("path", ["/api/search/pause", "/api/search/resume"])
def test_the_search_controls_never_start_a_welcome(path):
    assert not server._may_welcome(_req("POST", path))
    # An ordinary click still may (the returning-user welcome is unchanged).
    assert server._may_welcome(_req("POST", "/application/12/viewed"))


def test_a_paused_user_is_not_welcomed_back_by_other_clicks(welcomed):
    """Opening a job or saving a note is not "Resume search"."""
    uid = _returner(paused=True)
    server._touch_last_active(uid, meaningful=True,
                              welcome=server._may_welcome(_req("POST", "/application/12/viewed")))
    assert welcomed == []
    assert _paused_at(uid) is not None, "only Resume clears an explicit pause"


def test_the_same_click_welcomes_an_unpaused_returner(welcomed):
    """The control: the pause check is what stops it, not something else."""
    uid = _returner(paused=False)
    server._touch_last_active(uid, meaningful=True,
                              welcome=server._may_welcome(_req("POST", "/application/12/viewed")))
    assert welcomed == [uid]


# ── Resume is how an idle user comes back ────────────────────────────────────
# Page loads stopped counting as activity (2026-10-08, _PASSIVE_WRITE_PATHS),
# so "Resume search" is now the usual way back. A RETURN gets the first-hour
# window — opened by the route AFTER it clears the pause (the stamp runs first,
# while the profile still reads paused, and begin() refuses a paused user).

def _route_req(path: str):
    r = _req("POST", path)
    r.state = SimpleNamespace()
    return r


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True))
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "get_user_id_from_token", lambda tok: UID)


def test_resume_after_a_long_pause_welcomes_them(welcomed, signed_in):
    uid = _returner(paused=True)          # last acted 40 days ago, then paused
    server.resume_search(_route_req("/api/search/resume"))
    assert _paused_at(uid) is None
    assert welcomed == [uid], "the welcome opens once the pause is cleared"


def test_resume_by_an_idle_user_welcomes_them(welcomed, signed_in):
    uid = _returner(paused=False)         # never paused, just away 40 days
    d = server.resume_search(_route_req("/api/search/resume"))
    assert welcomed == [uid]
    assert d["state"] in ("active", "paid")


def test_resume_minutes_after_pausing_is_not_a_return(welcomed, signed_in):
    uid = _returner(paused=True)
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        p.last_meaningful_activity_at = datetime.utcnow() - timedelta(minutes=20)
        s.add(p)
        s.commit()
    server.resume_search(_route_req("/api/search/resume"))
    assert _paused_at(uid) is None
    assert welcomed == []


def test_a_returning_users_pause_click_still_opens_nothing(welcomed, signed_in):
    """The other half: the same return signal on the PAUSE route is ignored."""
    uid = _returner(paused=False)
    server.pause_search(_route_req("/api/search/pause"))
    assert welcomed == []
    assert _paused_at(uid) is not None


# ── a window that was already open ───────────────────────────────────────────

def test_pausing_ends_an_open_boost():
    uid = _returner()
    assert welcome.begin(uid, "test")
    base = settings.scoring_per_user_cap
    assert welcome.is_boosted(uid) and welcome.per_cycle_cap(uid, base) == base * 2

    server._set_search_pause(uid, True)

    assert not welcome.is_boosted(uid)
    assert welcome.per_cycle_cap(uid, base) == base
    assert welcome.scoring_window_days(uid) == settings.scoring_max_job_age_days


def test_a_paused_user_gets_no_new_boost():
    uid = _returner(paused=True)
    assert not welcome.begin(uid, "resume or roles saved")
    assert not welcome.is_boosted(uid)


def test_the_refresh_does_nothing_for_a_paused_user(monkeypatch):
    uid = _returner(paused=True)
    called = []
    monkeypatch.setattr(adoption, "adopt_shared_jobs", lambda u: called.append("adopt") or 0)
    monkeypatch.setattr(welcome, "kick_scoring", lambda: called.append("kick"))
    welcome._refresh(uid)
    assert called == []


def test_a_pause_landing_mid_refresh_stops_the_scoring_kick(monkeypatch):
    """The thread can start a moment before the Pause lands."""
    uid = _returner()
    welcome.begin(uid, "returning user")
    called = []

    def _adopt_then_user_pauses(u):
        called.append("adopt")
        server._set_search_pause(uid, True)
        return 3
    monkeypatch.setattr(adoption, "adopt_shared_jobs", _adopt_then_user_pauses)
    monkeypatch.setattr(welcome, "kick_scoring", lambda: called.append("kick"))
    welcome._refresh(uid)
    assert called == ["adopt"]
    assert not welcome.is_boosted(uid)


def test_saving_roles_or_a_resume_while_paused_seeds_nothing(monkeypatch):
    uid = _returner(paused=True)
    called = []
    monkeypatch.setattr(adoption, "adopt_and_match", lambda u: called.append("adopt") or 5)
    monkeypatch.setattr(settings, "onboarding_active_discovery", False)
    assert adoption.seed_new_user(uid) == 0
    assert called == []
    assert not welcome.is_boosted(uid)


# ── what the panel says ──────────────────────────────────────────────────────

def test_the_panel_says_paused_even_if_a_window_survives(monkeypatch):
    """Another replica (the window is process-local) may still hold a boost:
    the panel reads the pause, not the window."""
    uid = _returner()
    monkeypatch.setattr(server, "_get_user_id", lambda request: uid)
    welcome.begin(uid, "test")
    with get_session() as s:     # paused elsewhere: this process's window is untouched
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        p.search_paused_at = datetime.utcnow()
        s.add(p)
        s.commit()
    ttl_cache.invalidate(f"welcome:{uid}")
    d = server.welcome_status_api(request=None)
    assert d["paused"] is True and d["boost_active"] is False
    assert "preview" not in d and "minutes_left" not in d


def test_resume_drops_the_paused_answer():
    uid = _returner()
    server._set_search_pause(uid, True)
    ttl_cache.get_or_compute(f"welcome:{uid}", lambda d: 60, lambda: {"paused": True})
    server._set_search_pause(uid, False)
    assert ttl_cache.get_or_compute(f"welcome:{uid}", lambda d: 60,
                                    lambda: {"fresh": True}) == {"fresh": True}


# ── one reading, and the kill switch ─────────────────────────────────────────

def test_the_pause_reading_follows_the_kill_switch(monkeypatch):
    """With COMPUTE_POLICY_ENFORCED=0 the lanes serve a paused user; the
    welcome machinery must not be the one path that still refuses them."""
    uid = _returner(paused=True)
    assert cp.user_paused(uid)
    assert cp.is_paused(SimpleNamespace(search_paused_at=datetime.utcnow()))
    monkeypatch.setattr(settings, "compute_policy_enforced", False)
    assert not cp.user_paused(uid)
    assert not cp.is_paused(SimpleNamespace(search_paused_at=datetime.utcnow()))


def test_no_profile_is_not_paused():
    assert not cp.user_paused(_P + "nobody")
    assert not cp.user_paused(None)
    assert not cp.is_paused(None)


# ── the dashboard ────────────────────────────────────────────────────────────

HTML = (Path(__file__).resolve().parent.parent / "app/templates/dashboard.html").read_text()


def _function(name: str) -> str:
    m = re.search(r"(async\s+)?function\s+" + name + r"\s*\(", HTML)
    assert m, name
    depth, i = 0, HTML.index("{", m.end())
    for j in range(i, len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[j], 0)
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


def test_the_panel_never_draws_checking_for_a_paused_search():
    body = _function("_renderWelcomePanel")
    assert "d.paused" in body
    assert "!d.paused && (d.boost_active || starting)" in body


def test_pause_and_resume_refresh_the_panel_at_once():
    assert "loadWelcomePanel()" in _function("_afterSearchToggle")
    assert "_afterSearchToggle()" in _function("_renderSearchState")
    toggle = HTML[HTML.index("window.toggleSearchFromSettings"):]
    assert "_afterSearchToggle()" in toggle[:toggle.index("};")]
