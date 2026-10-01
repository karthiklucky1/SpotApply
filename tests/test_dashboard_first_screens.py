"""What a new user sees in their first minutes (owner's review, 2026-10-01).

Four things the owner flagged on a real sign-up, each pinned here:

- the resume review sat in a box over a half-visible dashboard ("clumsy") —
  it is now a full page with its own header, and the roles step of the setup
  has an opaque backdrop;
- "2 new matches ready — tap to load" made the user reload for jobs we already
  had — new matches now go into the board in place, on their own;
- the Pool tile, "Jobs found" and the All Jobs badge showed 500 / 381 / 360 —
  all three now count with ONE definition (freshness.found_jobs_expr);
- the first-hour priority ran its full hour — it now ends once
  WELCOME_COMPLETE_MATCHES (10) matches are on the board, and the panel says
  "You're set" instead of vanishing.

Synthetic users and rows only.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.strategy import welcome

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


# ── 1. the setup is a page, not a box over the board ────────────────────────

def test_the_resume_review_is_a_full_page():
    body = _function("openProfileReview")
    assert "background:var(--canvas" in body, "opaque page background"
    assert "rgba(15,23,42,.55)" not in body, "no see-through backdrop over the dashboard"
    assert "_setupHeader(1)" in body and "_setupPageOpen(true)" in body
    # every way out restores the page underneath
    assert body.count("_closeReview()") >= 2 and "_setupPageOpen(false)" in body


def test_the_setup_roles_step_has_an_opaque_backdrop_and_scroll_is_restored():
    body = _function("openRolesModal")
    assert "_rolesOnboarding ? 'var(--canvas" in body
    assert "_setupPageOpen(_rolesOnboarding)" in body
    assert "_setupPageOpen(false)" in _function("closeRolesModal")


# ── 2. new matches load themselves ──────────────────────────────────────────

def test_new_matches_are_swapped_in_without_a_reload():
    body = _function("refreshShortlistInPlace")
    assert "fetch('/dashboard'" in body and "DOMParser" in body
    assert "shortlisted-container" in body and "jr-new" in body
    assert "window.location.reload" not in body
    # the sort and the score filter the user chose are re-applied
    assert "_initShortlistSort()" in body and "filterShortlistByScore(" in body


def test_the_banner_is_only_for_a_busy_user_and_loads_in_place_too():
    tick = _function("livePipelineTick")
    assert tick.index("!_userIsBusy()") < tick.index("_showNewMatchesBanner(")
    assert "b.onclick = loadNewMatchesNow" in HTML
    busy = _function("_userIsBusy")
    for marker in ("activeElement", "profile-review", "jobModalOverlay", '-modal'):
        assert marker in busy, marker


def test_the_shortlisted_tab_count_follows_the_live_count():
    assert 'id="pt-count-shortlist"' in HTML
    assert "getElementById('pt-count-shortlist')" in _function("livePipelineTick")


# ── 3. one number for "jobs found" ──────────────────────────────────────────

def test_pool_tile_jobs_found_and_all_jobs_share_one_definition():
    from app.api import server
    from app.common import freshness
    assert "found_jobs_expr" in inspect.getsource(server._pipeline_live_uncached)
    assert "found_jobs_expr" in inspect.getsource(welcome.status)
    # and it IS the All Jobs window: same two bounds, render posting date
    src = inspect.getsource(freshness.found_jobs_expr)
    assert "explorer_max_age_days" in src and "shortlist_max_posted_age_days" in src
    assert "for_render=True" in src
    assert server._POOL_COUNT_TTL_SECONDS == server._JOBS_COUNT_TTL_SECONDS


def test_found_jobs_counts_agree_on_real_rows(monkeypatch):
    """Rows inside and outside the window: the pool query and the panel query
    give the same answer."""
    from sqlmodel import delete, func, select
    from app.common.freshness import found_jobs_expr
    from app.db.init_db import get_session
    from app.db.models import Job, JobSource
    monkeypatch.setattr(settings, "explorer_max_age_days", 5)
    monkeypatch.setattr(settings, "shortlist_max_posted_age_days", 30)
    uid, now = "fjx-user", datetime.utcnow()
    with get_session() as s:
        s.exec(delete(Job).where(Job.external_id.like("fjx-%")))
        for i, (days, posted, closed) in enumerate([(1, 2, False), (3, 40, False),
                                                     (9, 10, False), (1, 1, True)]):
            s.add(Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=f"fjx-{i}",
                      company="Co", title="Engineer", url=f"https://x.test/fjx{i}",
                      description="d", first_seen=now - timedelta(days=days),
                      discovered_at=now - timedelta(days=days),
                      posted_at=now - timedelta(days=posted), is_closed=closed))
        s.commit()
    try:
        with get_session() as s:
            n = s.exec(select(func.count(Job.id)).where(
                Job.user_id == uid, Job.is_closed == False,  # noqa: E712
                found_jobs_expr())).one()
        assert (n if isinstance(n, int) else n[0]) == 1, "only the fresh, open, recently posted job"
    finally:
        with get_session() as s:
            s.exec(delete(Job).where(Job.external_id.like("fjx-%")))
            s.commit()


# ── 4. priority ends at 10 matches ──────────────────────────────────────────

@pytest.fixture
def _window(monkeypatch):
    monkeypatch.setattr(settings, "welcome_boost_minutes", 60)
    monkeypatch.setattr(settings, "welcome_complete_matches", 10)
    monkeypatch.setattr(welcome, "_CHECK_EVERY_S", 0.0)
    for d in (welcome._BOOSTS, welcome._GRADUATED, welcome._LAST_CHECK):
        d.clear()
    yield
    for d in (welcome._BOOSTS, welcome._GRADUATED, welcome._LAST_CHECK):
        d.clear()


def test_priority_ends_once_ten_matches_are_on_the_board(_window, monkeypatch):
    board = {"n": 4}
    monkeypatch.setattr(welcome, "_matches_since", lambda uid, since: board["n"])
    welcome.begin("fs-user", "test")
    assert welcome.is_boosted("fs-user")
    assert welcome.scoring_window_days("fs-user") == settings.welcome_catchup_days
    board["n"] = 10
    assert not welcome.is_boosted("fs-user"), "served like everyone else from here"
    assert welcome.scoring_window_days("fs-user") == settings.scoring_max_job_age_days
    assert welcome.completed_at("fs-user") is not None
    # a role save inside the same hour does not restart the priority
    assert welcome.begin("fs-user", "roles saved") is False
    assert not welcome.is_boosted("fs-user")


def test_the_count_is_throttled(_window, monkeypatch):
    calls = []
    monkeypatch.setattr(welcome, "_CHECK_EVERY_S", 30.0)
    monkeypatch.setattr(welcome, "_matches_since", lambda uid, since: calls.append(1) or 0)
    welcome.begin("fs-user2", "test")
    for _ in range(20):
        welcome.is_boosted("fs-user2")
    assert len(calls) == 1, "one count per 30 s per user, not one per call"


def test_zero_disables_the_early_end(_window, monkeypatch):
    monkeypatch.setattr(settings, "welcome_complete_matches", 0)
    monkeypatch.setattr(welcome, "_matches_since", lambda uid, since: 999)
    welcome.begin("fs-user3", "test")
    assert welcome.is_boosted("fs-user3")


def test_the_panel_says_you_are_set_instead_of_vanishing():
    assert '"completed": done is not None' in inspect.getsource(welcome.status)
    body = _function("_renderWelcomePanel")
    assert "d.completed" in body and "You're set" in body and "closeWelcomeDone()" in body


def test_copying_jobs_in_clears_the_cached_counts():
    """Pool 0 beside Jobs found 15: the cached count predated the copies."""
    from app.common import ttl_cache
    from app.strategy import adoption
    src = inspect.getsource(adoption._adopt)
    assert "pool_count:{u}" in src and 'jobs_open:["{u}"' in src
    # the prefixes really match the keys the server writes
    from app.api import server
    key = server._jobs_count_key("u-1", False, None, None, None, None, None, None,
                                 None, None, None, 5)
    ttl_cache.put("jobs_open:" + key, 7, 60)
    assert ttl_cache.invalidate('jobs_open:["u-1"') == 1


# ── 5. the setup asks where they want to work ───────────────────────────────
# A blank country falls back to the platform default (US), so a user in India
# would have been searched for US jobs. The setup now asks, pre-filled only
# from what the location states.

def test_the_setup_asks_for_the_country_and_requires_it():
    body = _function("openProfileReview")
    assert "Where you want to work" in body
    assert "sel('preferred_country'" in body and "chk('remote_ok'" in body
    assert "missing.push('country')" in body
    nxt = body[body.index("data-pr-next]').onclick"):]
    assert nxt.index("!cSel.value") < nxt.index("fetch('/api/profile'"), "no save without a country"
    assert "Remote jobs based in other countries are left out" in body


def test_the_country_is_read_only_from_what_the_location_states(tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    a = HTML.index("const _COUNTRIES")
    b = HTML.index("// The setup's own page header")
    cases = {"Columbus, OH": "United States", "Toronto, ON, Canada": "Canada",
             "Bangalore, India": "India", "London, UK": "United Kingdom",
             "Remote, USA": "United States", "Berlin, Germany": "Germany",
             "Dublin": "", "Hyderabad, Telangana": "", "": ""}
    js = HTML[a:b] + "\nconsole.log(JSON.stringify(%s.map(_countryFromLocation)));" % (
        __import__("json").dumps(list(cases)))
    f = tmp_path / "c.js"
    f.write_text(js)
    out = subprocess.run([node, str(f)], capture_output=True, text=True, check=True).stdout
    assert __import__("json").loads(out) == list(cases.values())


def test_the_profile_route_accepts_both_fields():
    from app.api.server import ProfileUpdate
    assert {"preferred_country", "remote_ok"} <= set(ProfileUpdate.model_fields)
