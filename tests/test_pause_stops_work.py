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
from app.db.models import (
    Application, CompanyRegistry, DiscoveryRun, FunnelEvent, Job, JobSource,
    UserNotification, UserProfile,
)
from app.strategy import adoption, welcome

# The pause is a compute-policy state: enforce it (conftest turns it off).
pytestmark = pytest.mark.compute_policy

_P = "ps-"
UID = _P + "returner"


def _wipe():
    with get_session() as s:
        mine = [r for r in s.exec(select(Job.id).where(
            Job.user_id.like(f"{_P}%") | Job.external_id.like(f"{_P}%"))).all()]
        if mine:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(mine)))
            s.exec(delete(Application).where(Application.job_id.in_(mine)))
            s.exec(delete(Job).where(Job.id.in_(mine)))
        s.exec(delete(UserNotification).where(UserNotification.user_id.like(f"{_P}%")))
        s.exec(delete(DiscoveryRun).where(DiscoveryRun.user_id.like(f"{_P}%")))
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_P}%")))
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


# ── Resume restarts the search ───────────────────────────────────────────────
# Resume clears the pause, then runs the onboarding seed in the background
# (adoption.seed_new_user: the first-hour window, adoption for the CURRENT
# roles, matching, first results). Not "only when it looks like a return":
# review 2026-10-08 reproduced two users left with nothing until the next
# global pass — one who changed roles while paused (the seed was skipped and
# never re-run), one who opened a job before pressing Resume (the click
# overwrote the activity a return was judged by).

def _route_req(path: str):
    r = _req("POST", path)
    r.state = SimpleNamespace()
    return r


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True))
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "get_user_id_from_token", lambda tok: UID)


def _resume(uid):
    """POST /api/search/resume as the route runs it, then its background task."""
    from fastapi import BackgroundTasks
    bt = BackgroundTasks()
    d = server.resume_search(_route_req("/api/search/resume"), bt)
    assert _paused_at(uid) is None, "the pause is cleared before anything runs"
    for t in bt.tasks:
        t.func(*t.args, **t.kwargs)
    return d


@pytest.fixture
def seeded(monkeypatch):
    """Record seeds, and whether the user was still paused when each ran."""
    got = []
    monkeypatch.setattr(adoption, "seed_new_user",
                        lambda uid: got.append((uid, cp.user_paused(uid))) or 0)
    return got


@pytest.mark.parametrize("paused", [True, False])
def test_resume_restarts_the_search(signed_in, seeded, paused):
    uid = _returner(paused=paused)            # paused, or simply idle for 40 days
    d = _resume(uid)
    assert seeded == [(uid, False)]
    assert d["state"] in ("active", "paid")


def test_roles_changed_while_paused_are_searched_on_resume(signed_in, monkeypatch):
    """The skipped seed is not lost: Resume runs it, for the roles saved now."""
    uid = _returner(paused=True)
    adopted = []
    monkeypatch.setattr(adoption, "adopt_and_match", lambda u: adopted.append(u) or 7)
    monkeypatch.setattr(settings, "onboarding_active_discovery", False)
    assert adoption.seed_new_user(uid) == 0 and adopted == [], "paused: saved, not searched"
    _resume(uid)
    assert adopted == [uid]
    assert welcome.is_boosted(uid), "served first, now that they are back"


def test_a_click_before_resume_does_not_cost_the_restart(signed_in, seeded):
    """Opening a job while paused is meaningful (and stamps activity); Resume
    must still restart the search afterwards."""
    uid = _returner(paused=True)
    server._get_user_id(_req("POST", "/application/12/viewed"))
    _resume(uid)
    assert seeded == [(uid, False)]


def test_a_failed_restart_never_fails_resume(signed_in, monkeypatch):
    uid = _returner(paused=True)

    def _boom(u):
        raise RuntimeError("matching unavailable")
    monkeypatch.setattr(adoption, "seed_new_user", _boom)
    assert _resume(uid)["state"] in ("active", "paid")


def test_a_returning_users_pause_click_still_opens_nothing(welcomed, signed_in, seeded):
    """The other half: the Pause route starts nothing at all."""
    uid = _returner(paused=False)
    server.pause_search(_route_req("/api/search/pause"))
    assert welcomed == [] and seeded == []
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


# ═════════════════════════════════════════════════════════════════════════════
# The lanes, mid-tick (live test 2026-10-09)
#
# Pause at 14:33:14. The pulse tick then running had read its user list at
# 14:31:55; at 14:33:28 it routed two Workday postings into the paused user's
# pool through its per-user door ("All Jobs" 473 -> 475), and at 14:33:59 the
# matching lane's user-list builder filed the idle/dormant notice ("paused to
# save resources while you're away") into their bell (7 -> 8). Every lane's
# user list is minutes old by the time it writes, so each per-user step asks
# again (compute_policy.paused_user_ids / paused_now).
# ═════════════════════════════════════════════════════════════════════════════

from app.discovery.base import RawJob  # noqa: E402
from app.strategy import fresh_alerts, hot_lane, pulse_lane, scoring_lane  # noqa: E402

SLUG = _P + "board"


def _raw(ext: str, title: str = "Senior Software Engineer") -> RawJob:
    return RawJob(source="greenhouse", external_id=_P + ext, company="Acme",
                  title=title, location="Remote", remote=True,
                  url=f"https://boards.greenhouse.io/acme/jobs/{ext}",
                  description="Build backend services in Python.",
                  posted_at=datetime.utcnow())


def _registry_board():
    with get_session() as s:
        s.add(CompanyRegistry(slug=SLUG, ats=JobSource.GREENHOUSE, is_active=True,
                              job_count=3, source="test"))
        s.commit()


def _my_boards(*_a, **_k):
    with get_session() as s:
        return list(s.exec(select(CompanyRegistry).where(CompanyRegistry.slug == SLUG)).all())


class _ScraperThatSeesAPause:
    """The fetch is where a tick spends its time; the user pauses during it."""

    def __init__(self, uid, pause: bool):
        self.uid, self.pause = uid, pause

    def fetch(self):
        if self.pause:
            server._set_search_pause(self.uid, True)
        return [_raw("1"), _raw("2", title="Backend Engineer")]


@pytest.fixture
def upserts(monkeypatch):
    """Record every door `_upsert` is asked to write through, by owner."""
    got = []

    def _fake(raw, user_id=None, **_k):
        got.append(user_id)
        return len(raw)
    monkeypatch.setattr("app.discovery.pipeline._upsert", _fake)
    return got


def _lane_user(uid):
    return {"user_id": uid, "roles": ["software engineer", "backend engineer"],
            "preferred_country": "United States", "remote_ok": True, "geo_prefs": None}


@pytest.mark.parametrize("pause", [True, False])
def test_a_pause_mid_pulse_tick_stops_the_per_user_route(monkeypatch, upserts, pause):
    uid = _returner()
    _registry_board()
    with get_session() as s:
        last_event = s.exec(select(FunnelEvent.id).order_by(FunnelEvent.id.desc())).first() or 0
    monkeypatch.setattr(pulse_lane, "_due_boards", _my_boards)
    monkeypatch.setattr(pulse_lane, "_next_board_cap", lambda cap, stats, elapsed: cap)
    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, career_url=None: _ScraperThatSeesAPause(uid, pause))
    # The user list is read ONCE, at the start of the tick, before the pause.
    monkeypatch.setattr("app.strategy.hot_lane._active_users", lambda: [_lane_user(uid)])
    fast = []
    monkeypatch.setattr(pulse_lane, "_fast_path_user",
                        lambda u, budget, deadline=None: fast.append(u) or (0, 0, 0))
    try:
        stats = pulse_lane.run_pulse_tick()
    finally:
        with get_session() as s:
            s.exec(delete(FunnelEvent).where(FunnelEvent.id > last_event,
                                             FunnelEvent.stage == "pulse_tick"))
            s.commit()
    assert stats["changed"] == 1
    if pause:
        assert upserts == ["__shared__"], "the shared pool is fed; the paused user's pool is not"
        assert fast == []
    else:
        assert upserts == ["__shared__", uid]
        assert fast == [uid]


@pytest.mark.parametrize("pause", [True, False])
def test_a_pause_mid_hot_lane_cycle_stops_routing_and_matching(monkeypatch, upserts, pause):
    uid = _returner()
    monkeypatch.setattr(hot_lane, "_active_users", lambda: [_lane_user(uid)])
    monkeypatch.setattr(hot_lane, "select_hot_boards",
                        lambda limit: [SimpleNamespace(slug=SLUG, ats="greenhouse", career_url=None)])
    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, career_url=None: _ScraperThatSeesAPause(uid, pause))
    monkeypatch.setattr(hot_lane, "_mark_polled", lambda *a, **k: None)
    monkeypatch.setattr(hot_lane, "_finish_cycle", lambda stats: stats)
    # Even a user with fresh unscored jobs waiting is not matched while paused.
    monkeypatch.setattr(hot_lane, "_users_with_pending_fresh", lambda users: {uid})
    matched = []
    monkeypatch.setattr("app.matching.pipeline.run_matching", lambda u: matched.append(u) or [])
    monkeypatch.setattr(fresh_alerts, "dispatch_fresh_alerts", lambda u, ids: 0)
    hot_lane._run_hot_lane_cycle()
    if pause:
        assert upserts == ["__shared__"] and matched == []
    else:
        assert upserts == ["__shared__", uid] and matched == [uid]


@pytest.fixture
def lane_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(adoption, "adopt_incremental",
                        lambda u, **k: calls.append(("adopt", u)) or 0)
    monkeypatch.setattr(adoption, "adopt_shared_jobs",
                        lambda u, *a, **k: calls.append(("adopt", u)) or 0)
    monkeypatch.setattr(server, "run_matching", lambda u: calls.append(("match", u)) or [])
    monkeypatch.setattr(fresh_alerts, "dispatch_fresh_alerts",
                        lambda u, ids: calls.append(("alert", u)) or 0)
    monkeypatch.setattr("app.strategy.shortlist_hygiene.prune_stale_shortlist", lambda: 0)
    return calls


@pytest.mark.parametrize("pause", [True, False])
def test_the_matching_lane_rechecks_each_user(lane_calls, pause):
    """`uids` was built before the lane waited for the lock and served the
    users ahead of this one."""
    uid = _returner()
    if pause:
        server._set_search_pause(uid, True)
    server._run_matching_lane([uid])
    if pause:
        assert lane_calls == []
    else:
        assert [c[0] for c in lane_calls] == ["adopt", "match", "alert"]


@pytest.mark.parametrize("pause", [True, False])
def test_the_global_pass_adopts_nothing_for_a_user_who_paused_during_the_scrape(lane_calls, pause):
    uid = _returner()
    if pause:
        server._set_search_pause(uid, True)
    server._adopt_match_alert([uid])
    if pause:
        assert lane_calls == []
    else:
        assert [c[0] for c in lane_calls] == ["adopt", "match", "alert"]


# ── adoption: the backstop under every caller ────────────────────────────────

def test_adoption_copies_nothing_for_a_paused_user(monkeypatch):
    uid = _returner(paused=True)
    ran = []
    monkeypatch.setattr(adoption, "_adopt", lambda *a, **k: ran.append(a) or (5, False))
    assert adoption.adopt_shared_jobs(uid) == 0
    assert adoption.adopt_incremental(uid, interval_seconds=300) == 0
    assert ran == []
    assert uid not in adoption._ADOPT_WATERMARK, "the watermark does not move while paused"

    server._set_search_pause(uid, False)
    assert adoption.adopt_shared_jobs(uid) == 5
    assert adoption.adopt_incremental(uid, interval_seconds=300) == 5


def test_a_pause_during_the_instant_feed_lock_wait_stops_its_matching(monkeypatch):
    uid = _returner()
    matched = []

    def _adopt_then_user_pauses(u):
        server._set_search_pause(uid, True)
        return 3
    monkeypatch.setattr(adoption, "adopt_shared_jobs", _adopt_then_user_pauses)
    monkeypatch.setattr(welcome, "first_results", lambda u: {})
    monkeypatch.setattr("app.matching.pipeline.run_matching", lambda u: matched.append(u) or [])
    monkeypatch.setattr(adoption, "_score_first_slice", lambda u: matched.append(("kick", u)))
    assert adoption.adopt_and_match(uid) == 3
    assert matched == []


def test_a_pause_during_the_seed_stops_the_thin_feed_scrape(monkeypatch):
    uid = _returner()
    scraped = []

    def _feed_then_user_pauses(u):
        server._set_search_pause(uid, True)
        return 0
    monkeypatch.setattr(adoption, "adopt_and_match", _feed_then_user_pauses)
    monkeypatch.setattr(adoption, "_usable_count", lambda u: 0)
    monkeypatch.setattr(settings, "onboarding_active_discovery", True)
    monkeypatch.setattr(server, "_get_target_roles", lambda u: ["Software Engineer"])
    monkeypatch.setattr(server, "_user_has_resume", lambda u: True)
    monkeypatch.setattr(server, "_discover_then_match", lambda u, **_k: scraped.append(u))
    adoption.seed_new_user(uid)
    assert scraped == []


# The scrape itself waits on the blocking discovery lock (the global pass, hot
# lane and matching lane all hold it, for minutes) and runs two waves. The check
# above runs before that wait; these pin the ones after it.

@pytest.fixture
def scrape(monkeypatch):
    """A thin-feed seed whose scrape and matching are recorded, not run."""
    calls = []
    monkeypatch.setattr(adoption, "adopt_and_match", lambda u: 0)
    monkeypatch.setattr(adoption, "_usable_count", lambda u: 0)
    monkeypatch.setattr(settings, "onboarding_active_discovery", True)
    monkeypatch.setattr(server, "_user_has_resume", lambda u: True)
    monkeypatch.setattr(server, "run_discovery",
                        lambda u, run_id=None, keywords=None, phase=None: calls.append(("discover", phase)))
    monkeypatch.setattr(server, "run_matching", lambda u: calls.append(("match", u)) or [])
    monkeypatch.setattr(fresh_alerts, "dispatch_fresh_alerts", lambda u, ids: 0)
    return calls


def _runs(uid):
    with get_session() as s:
        return [(r.status, r.error) for r in s.exec(
            select(DiscoveryRun).where(DiscoveryRun.user_id == uid)).all()]


def test_a_pause_during_the_scrapes_lock_wait_stops_it(monkeypatch, scrape):
    """Reviewer repro: Pause lands while the seed's scrape waits for the lock."""
    from contextlib import contextmanager
    uid = _returner()

    @contextmanager
    def _lock_held_by_a_global_pass(blocking=True, label=""):
        server._set_search_pause(uid, True)      # the user pauses during the wait
        yield True
    monkeypatch.setattr("app.common.discovery_lock.discovery_guard", _lock_held_by_a_global_pass)
    adoption.seed_new_user(uid)
    assert scrape == [], "scraped into a paused user's pool after the lock wait"
    assert _runs(uid) == [] and _notifications(uid) == []


@pytest.mark.parametrize("automatic", [True, False])
def test_a_pause_between_the_waves_stops_the_deep_scrape(monkeypatch, scrape, automatic):
    """Wave 1 ran; the user paused while it matched. The seed's run stops there,
    closed with no "Job Discovery Completed" in the bell. A manual Discover
    click is the user asking, so it is unchanged."""
    uid = _returner()

    def _match_then_user_pauses(u):
        scrape.append(("match", u))
        if len(scrape) == 2:
            server._set_search_pause(uid, True)
        return []
    monkeypatch.setattr(server, "run_matching", _match_then_user_pauses)
    server._discover_then_match(uid, automatic=automatic)
    if automatic:
        assert scrape == [("discover", "fast"), ("match", uid)]
        assert _runs(uid) == [("cancelled", "Search paused")]
        assert _notifications(uid) == []
    else:
        assert [c[1] for c in scrape if c[0] == "discover"] == ["fast", "boards"]
        assert [r[0] for r in _runs(uid)] == ["done"]
        assert _notifications(uid) == ["discovery_completed"]


def test_an_unpaused_seed_still_scrapes_both_waves(scrape):
    uid = _returner()
    adoption.seed_new_user(uid)
    assert [c[1] for c in scrape if c[0] == "discover"] == ["fast", "boards"]
    assert [r[0] for r in _runs(uid)] == ["done"]


# ── notifications ────────────────────────────────────────────────────────────

def _notifications(uid):
    with get_session() as s:
        return list(s.exec(select(UserNotification.type).where(
            UserNotification.user_id == uid)).all())


def _strong_fresh_job(uid) -> int:
    now = datetime.utcnow()
    with get_session() as s:
        j = Job(user_id=uid, source=JobSource.LEVER, external_id=_P + "alert-1",
                company="Acme", title="Backend Engineer", url="https://jobs.lever.co/acme/1",
                description="Python", rerank_score=95.0, blended_score=95.0,
                first_seen=now, discovered_at=now, posted_at=now)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


@pytest.mark.parametrize("paused", [True, False])
def test_no_fresh_match_alert_reaches_a_paused_user(paused):
    """dispatch_fresh_alerts is the door every lane sends "New match" through."""
    uid = _returner(paused=paused)
    jid = _strong_fresh_job(uid)
    sent = fresh_alerts.dispatch_fresh_alerts(uid, [jid])
    if paused:
        assert sent == 0 and _notifications(uid) == []
    else:
        assert sent == 1 and _notifications(uid) == ["fresh_job"]


def test_pausing_files_no_idle_notice_in_the_bell():
    """The notice is the idle/dormant one: "while you're away", "or open any
    job". Both are false for a pause the user made, which only Resume clears."""
    uid = _returner(paused=True)
    with get_session() as s:
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
    assert not server._user_is_active(prof)
    assert server._notify_if_newly_dormant(prof) is False
    assert _notifications(uid) == []


def test_an_idle_user_is_still_told_once():
    """The control: the notice still reaches someone who simply went away."""
    uid = _returner(paused=False)
    with get_session() as s:
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
    assert server._notify_if_newly_dormant(prof) is True
    assert _notifications(uid) == ["feed_paused"]


@pytest.mark.parametrize("paused", [True, False])
def test_the_scoring_lane_places_nothing_for_a_user_who_paused_mid_cycle(monkeypatch, paused):
    """Scores already bought stay on the jobs (Resume's matching pass re-offers
    them); nothing new reaches the board or the bell while paused."""
    uid = _returner(paused=paused)
    reached = []

    def _delivered_today(u, cached=True):
        reached.append(u)
        raise RuntimeError("stop: the placement path was entered")
    monkeypatch.setattr("app.matching.finals_budget.delivered_today", _delivered_today)
    stats = {"shortlisted": 0, "alerts": 0}
    if paused:
        scoring_lane._shortlist_user(uid, [(1, 90.0)], stats)
        assert reached == [] and stats["paused_skipped"] == 1
    else:
        with pytest.raises(RuntimeError):
            scoring_lane._shortlist_user(uid, [(1, 90.0)], stats)
        assert reached == [uid]


def test_the_high_match_notice_asks_about_a_pause():
    """run_matching files "Perfect Job Match!" as it places; a pass that began
    before the Pause must not put one in the bell after it."""
    import inspect
    from app.matching import pipeline as mp
    src = inspect.getsource(mp.run_matching)
    i = src.index('type="high_match"')
    guard = src[src.rindex("if score >= 75", 0, i):i]
    assert "paused_now(user_id)" in guard


# ── board placement: ONE gate, in the one writer of a SHORTLISTED row ─────────
# Only the scoring lane asked again before placing. run_matching's Phase 3 (the
# matching lane, the global pass, the hot lane) and the pulse fast path placed
# every score their pass had already bought, after the Pause.

# SQLite reuses a deleted job's id, and other files leave placement events
# behind for theirs: count only events written after OUR job was created.
_EVENTS_BEFORE: dict = {}


def _scored_job(uid, ext: str, score=None) -> int:
    now = datetime.utcnow()
    with get_session() as s:
        mark = s.exec(select(FunnelEvent.id).order_by(FunnelEvent.id.desc())).first() or 0
        j = Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=_P + ext,
                company=f"Co-{ext}", title=f"Backend Engineer {ext}", location="Remote",
                remote=True, url=f"https://boards.greenhouse.io/co/jobs/{ext}",
                description="Build backend services in Python.", rerank_score=score,
                blended_score=score, first_seen=now, discovered_at=now, posted_at=now)
        s.add(j)
        s.commit()
        s.refresh(j)
        _EVENTS_BEFORE[j.id] = mark
        return j.id


def _placements(jid):
    with get_session() as s:
        return list(s.exec(select(FunnelEvent.reason).where(
            FunnelEvent.job_id == jid, FunnelEvent.stage == "placement",
            FunnelEvent.id > _EVENTS_BEFORE.get(jid, 0)).order_by(FunnelEvent.id)).all())


def _apps(uid):
    with get_session() as s:
        return list(s.exec(select(Application.job_id).where(Application.user_id == uid)).all())


@pytest.mark.parametrize("paused", [True, False])
def test_the_slate_places_nothing_for_a_paused_user(paused):
    from app.strategy import slate
    uid = _returner(paused=paused)
    jid = _scored_job(uid, "slate-1", 90.0)
    with get_session() as s:
        res = slate.place(s, s.get(Job, jid), 90.0, user_id=uid)
        s.commit()
    if paused:
        assert (res.created, res.outcome) == (False, slate.OUTCOME_PAUSED)
        assert _apps(uid) == [] and _placements(jid) == ["paused"], \
            "refused, and the one placement event says why"
    else:
        assert res.created and _apps(uid) == [jid]


def test_resume_re_offers_what_the_pause_held_back():
    """The scores stay on the jobs; Resume's matching pass places them."""
    from app.matching import pipeline as mp
    uid = _returner(paused=True)
    jids = [_scored_job(uid, f"held-{i}", 90.0 - i) for i in range(3)]
    assert mp._reshortlist_scored_jobs(uid, 0) == ([], 0)
    assert [r for j in jids for r in _placements(j)] == ["paused"], \
        "one refusal, then the backstop stops instead of refusing the whole list"
    server._set_search_pause(uid, False)
    ids, n = mp._reshortlist_scored_jobs(uid, 0)
    assert sorted(ids) == sorted(jids) and n == 3


def test_the_pulse_fast_path_places_nothing_after_a_pause(monkeypatch):
    uid = _returner()
    with get_session() as s:       # active: the fast path pays only for an active search
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        p.last_meaningful_activity_at = datetime.utcnow()
        s.add(p)
        s.commit()
    jid = _scored_job(uid, "fast-1")

    class _UserPausesWhileWeScore:
        def __init__(self, profile=None, feedback=""):
            pass

        def has_prescore_backend(self):
            return False

        def score(self, resume, job):
            server._set_search_pause(uid, True)
            return 90.0, "Strong fit", [], {}
    monkeypatch.setattr("app.matching.reranker.Reranker", _UserPausesWhileWeScore)
    monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: "resume")
    monkeypatch.setattr("app.matching.filters.score_ghost", lambda job, session: SimpleNamespace(
        is_ghost=False, ghost_score=0.0, flags_json=None, flags=[]))
    assert pulse_lane._fast_path_user(uid, score_budget=5) == (1, 0, 0)
    with get_session() as s:
        assert s.get(Job, jid).rerank_score == 90.0, "the score already bought is kept"
    assert _apps(uid) == [] and _placements(jid) == ["paused"]


# ── the one reading the lanes use ────────────────────────────────────────────

def test_the_paused_set_is_one_cached_read_that_a_pause_refreshes():
    uid = _returner()
    assert uid not in cp.paused_user_ids()
    server._set_search_pause(uid, True)          # forget() drops the cached set
    assert cp.paused_now(uid)
    server._set_search_pause(uid, False)
    assert not cp.paused_now(uid)


def test_the_paused_set_expires_for_a_pause_made_on_another_replica():
    uid = _returner()
    t0 = 1000.0
    assert uid not in cp.paused_user_ids(now=t0)
    with get_session() as s:                     # paused elsewhere: no forget() here
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        p.search_paused_at = datetime.utcnow()
        s.add(p)
        s.commit()
    assert uid not in cp.paused_user_ids(now=t0 + 1), "cached inside the TTL"
    assert uid in cp.paused_user_ids(now=t0 + cp._PAUSED_TTL_S + 1)


def test_the_paused_set_follows_the_kill_switch(monkeypatch):
    uid = _returner(paused=True)
    assert cp.paused_now(uid)
    monkeypatch.setattr(settings, "compute_policy_enforced", False)
    cp.reset_state()
    assert cp.paused_user_ids() == frozenset() and not cp.paused_now(uid)


def test_a_failed_read_keeps_the_pauses_already_seen(monkeypatch):
    uid = _returner(paused=True)
    assert cp.paused_now(uid)
    cp.forget(uid)

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("app.db.init_db.get_session", _boom)
    try:
        assert cp.paused_now(uid), "a hiccup must not forget a pause we already saw"
    finally:
        monkeypatch.undo()          # before any teardown opens a session


# ── "New postings (24h)" is the platform's number, never the user's pool ─────

def test_the_header_never_shows_the_users_pool_as_new_postings():
    """It reads the SHARED pool (every lane, each posting once). It used to fall
    back to `jobs_discovered_24h` (THIS user's pool) whenever the shared count
    missed the dashboard budget, which is what the tester saw move 127 -> 128."""
    body = _function("loadMeasuredStatus")
    assert "f.shared_pool_new_24h" in body and "f.jobs_discovered_24h" not in body
    card = HTML[HTML.index("if (newEl) newEl.textContent ="):]
    card = card[:card.index(";")]
    assert "f.shared_pool_new_24h" in card and "f.jobs_discovered_24h" not in card
