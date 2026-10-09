"""The owner's live test of the dashboard (2026-10-09, Claude in Chrome on the
owner's real account), one test group per finding. Synthetic rows only.

1. "Did you submit?" was scheduled 15 s after Auto-Fill whether or not anyone
   was looking, removed itself after 25 s and was marked asked when SCHEDULED:
   the user came back from the employer's form to nothing.
2. The page posted SPOTAPPLY_INIT_EXTENSION at 0/0.5/1.5/3/6 s and then every
   15 s forever: 152 "Received INIT_EXTENSION" lines in ~15 minutes.
3. Sidebar open at 960-1230 px: "Boards" and "Freshest / Best match" were cut
   off behind a scroll strip.
4. A paused search showed "Checking boards…" and no notice for 15-18 s.
5. Tailoring Studio's "Looks good" was clickable mid-generation.
6. Insights said "No jobs yet" for 473 jobs: /api/stats's closed counts hit the
   statement timeout and skipped everything after them (tests in
   test_dashboard_load.py; the window equivalence is here).
7. "? Profile" and Pool "…" until three API calls returned.
8. Pool read 473 / 3.8k (two writers, two definitions); Ghost Jobs 73,171 then
   3; "You're set: 13 matches on your board" beside a Shortlisted tab of 20.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text()
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _function(name: str) -> str:
    m = re.search(r"(async\s+)?function\s+" + name + r"\s*\(", HTML)
    assert m, name
    depth, i = 0, HTML.index("{", m.end())
    for j in range(i, len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[j], 0)
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


def _decl(name: str) -> str:
    """A one-line top-level `const`/`let` declaration."""
    m = re.search(r"^\s*(?:const|let)\s+" + re.escape(name) + r"\s*=.*?;", HTML, re.M)
    assert m, name
    return m.group(0).strip()


def _node(script: str) -> dict:
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


_FAKE_STORAGE = """
const store = new Map();
global.localStorage = {
  get length() { return store.size; },
  key(i) { return Array.from(store.keys())[i] ?? null; },
  getItem(k) { return store.has(k) ? store.get(k) : null; },
  setItem(k, v) { store.set(k, String(v)); },
  removeItem(k) { store.delete(k); },
};
"""


# ── 1. "Did you submit?" waits for the user ─────────────────────────────────

@needs_node
def test_the_submit_prompt_is_asked_on_return_and_kept_until_answered():
    funcs = "\n".join(_function(n) for n in (
        "_pendingApplies", "trackPendingApply", "_markPendingLeft",
        "maybeAskSubmit", "_clearPendingApply"))
    decls = "\n".join(_decl(n) for n in (
        "_PENDING_KEY", "_PENDING_TTL_MS", "_PENDING_MAX", "_PENDING_MIN_AWAY_MS", "_askTimer"))
    out = _node(_FAKE_STORAGE + """
let now = 1000000;
Date.now = () => now;
const timers = [];
global.setTimeout = (fn, ms) => { timers.push({fn, at: now + ms}); return timers.length; };
global.clearTimeout = () => {};
const runTimers = () => timers.splice(0).filter(t => t.at <= now).forEach(t => t.fn());
const shown = [];
let boxOpen = false;
global.document = { hidden: false,
  getElementById: (id) => (id === 'hp-submit-confirm' && boxOpen) ? {} : null };
function showSubmitConfirm(appId) { shown.push(appId); boxOpen = true; }
""" + decls + "\n" + funcs + """
const out = {};
trackPendingApply(11, 'Acme');                 // Auto-Fill & Apply / a posting link
maybeAskSubmit();                              // never left the tab: nothing to ask
out.neverLeft = shown.length;
document.hidden = true; _markPendingLeft();    // the posting's tab is in front
now += 20000; maybeAskSubmit();                // a hidden tab never shows it
out.whileHidden = shown.length;
document.hidden = false; maybeAskSubmit();     // back on the dashboard
out.onReturn = shown.slice();
maybeAskSubmit(); out.noDuplicate = shown.length;
out.stillPending = _pendingApplies().map(p => p.appId);   // shown != answered
boxOpen = false;
trackPendingApply(22, 'Beta');
_clearPendingApply(11);                        // answered: only THAT one is done
out.afterAnswer = _pendingApplies().map(p => p.appId);
document.hidden = true; _markPendingLeft(); now += 2000; document.hidden = false;
maybeAskSubmit(); out.backAfter2s = shown.length;         // a glance, not yet
now += 4000; runTimers(); out.afterAway = shown.slice();
boxOpen = false;
now += 4 * 24 * 3600 * 1000;
out.expired = _pendingApplies().length;
console.log(JSON.stringify(out));
""")
    assert out["neverLeft"] == 0
    assert out["whileHidden"] == 0
    assert out["onReturn"] == ["11"]
    assert out["noDuplicate"] == 1
    assert out["stillPending"] == ["11"], "showing the prompt must not mark it answered"
    assert out["afterAnswer"] == ["22"]
    assert out["backAfter2s"] == 1 and out["afterAway"] == ["11", "22"]
    assert out["expired"] == 0


def test_nothing_schedules_or_removes_the_prompt_on_a_timer():
    fill = _function("fillWithExtension")
    assert "trackPendingApply(appId" in fill
    assert "maybeAskSubmit(" not in fill and "setTimeout(maybeAskSubmit" not in fill, \
        "Auto-Fill arms the prompt; the return to the tab shows it"
    box = _function("showSubmitConfirm")
    assert "setTimeout" not in box and "25000" not in box
    for btn in ('id="hp-sc-yes"', 'id="hp-sc-no"', 'id="hp-sc-gone"'):
        assert btn in box
    # Every answer clears that application's prompt, and Yes only once saved.
    assert "_clearPendingApply(appId)" in box and "if (!r.ok)" in box
    # Leaving marks it; coming back asks.
    assert re.search(r"if \(document\.hidden\) _markPendingLeft\(\);\s*else maybeAskSubmit\(\);", HTML)
    assert "addEventListener('blur', _markPendingLeft)" in HTML


# ── 2. the extension credentials: once per page load ────────────────────────

@needs_node
def test_init_is_sent_once_per_load_and_again_only_on_a_change():
    funcs = _function("_initPack") + "\n" + _function("_broadcastInitPack")
    out = _node(_FAKE_STORAGE + """
const posted = [];
global.window = { location: { origin: 'https://app.spotapply.ai' },
                  postMessage: (m) => posted.push(m.type) };
""" + _decl("_initPackSent") + "\n" + funcs + """
const out = {};
_broadcastInitPack(); out.loggedOut = posted.length;
localStorage.setItem('sb_token', 't1'); localStorage.setItem('sb_refresh', 'r1');
for (let i = 0; i < 10; i++) _broadcastInitPack();      // every ping reply
out.afterPings = posted.length;
localStorage.setItem('sb_token', 't2');                 // a token refresh
for (let i = 0; i < 10; i++) _broadcastInitPack();
out.afterRefresh = posted.length;
out.types = posted;
console.log(JSON.stringify(out));
""")
    assert out["loggedOut"] == 0
    assert out["afterPings"] == 1
    assert out["afterRefresh"] == 2
    assert set(out["types"]) == {"SPOTAPPLY_INIT_EXTENSION"}


def test_init_has_no_schedule_and_follows_the_extensions_reply():
    assert "setInterval(_broadcastInitPack" not in HTML
    assert "[0, 500, 1500, 3000, 6000]" not in HTML
    i = HTML.index("let _initPackSent")
    tail = HTML[i:i + 2500]
    assert re.search(r"_EXT_PING_OK\$/\.test\(.*\) _broadcastInitPack\(\);", tail)
    # Hidden tabs do not ping (their throttled timers fire in bursts).
    assert "if (!document.hidden) ping();" in HTML


# ── 3. the toolbar wraps instead of cutting controls off ────────────────────

def _desktop_blocks() -> str:
    return "\n".join(m.group(0) for m in re.finditer(
        r"@media \(min-width: 769px\) \{.*?\n\s*\}\n", HTML, re.S))


def test_the_toolbars_wrap_on_desktop():
    css = _desktop_blocks()
    assert re.search(r"#pipe-tabs \{ flex-wrap: wrap; overflow-x: visible;", css)
    assert re.search(r"#shortlist-controls, #submitted-controls, #interviewing-controls \{\s*flex-wrap: wrap;", css)
    assert re.search(r"\.tab-row #tab-nav-bar \{ flex-wrap: wrap; overflow-x: visible;", css)
    # The active-tab indicator follows its tab onto a second row...
    move = _function("_moveTabIndicator")
    assert "btn.offsetTop" in move and "btn.offsetHeight" in move
    # ...and survives a resize or the sidebar opening/closing.
    i = HTML.index("let _tabIndicatorRaf")
    assert "_moveTabIndicator(" in HTML[i:i + 600] and "'resize'" in HTML[i:i + 600]


# ── 5. "Looks good" waits for the draft ─────────────────────────────────────

def test_looks_good_is_disabled_until_the_draft_is_rendered():
    btn = re.search(r'<button[^>]*id="ts-done-btn"[^>]*>Looks good</button>', HTML)
    assert btn and " disabled" in btn.group(0)
    assert "_tsSetDone(false)" in _function("openTailorStudio")
    assert "_tsSetDone(false)" in _function("tsRebuild")
    assert "_tsSetDone(true)" in _function("_tsRenderResult")
    # The only enabling call is the rendered result.
    assert HTML.count("_tsSetDone(true)") == 1


# ── 6. the window count is the same rows, index-friendly ────────────────────

def test_the_index_friendly_window_counts_the_same_rows():
    from sqlmodel import delete, func, select

    from app.common.freshness import found_jobs_expr, is_fresh_expr
    from app.db.init_db import get_session
    from app.db.models import Job, JobSource
    uid, now = "dlf-window", datetime.utcnow()
    d = lambda n: now - timedelta(days=n)  # noqa: E731
    cases = [  # first_seen, discovered_at, posted_at
        (d(1), d(1), d(2)), (d(9), d(1), d(2)), (None, d(1), d(2)), (None, d(9), None),
        (None, None, d(1)), (d(1), None, d(40)), (d(2), d(2), None), (None, d(3), d(50)),
    ]
    with get_session() as s:
        s.exec(delete(Job).where(Job.user_id == uid))
        for i, (fs, disc, posted) in enumerate(cases):
            s.add(Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=f"dlf-{i}",
                      company="Co", title="Engineer", url=f"https://x.test/dlf{i}",
                      description="d", first_seen=fs, discovered_at=disc, posted_at=posted))
        s.commit()
        try:
            def n(expr):
                return s.exec(select(func.count(Job.id)).where(Job.user_id == uid, expr)).one()
            a = n(is_fresh_expr(5, 30, now, for_render=True))
            b = n(is_fresh_expr(5, 30, now, for_render=True, index_friendly=True))
            assert a == b > 0
            sql = str(found_jobs_expr(now).compile())
            assert "coalesce(job.first_seen, job.discovered_at)" not in sql.lower()
        finally:
            s.exec(delete(Job).where(Job.user_id == uid))
            s.commit()


# ── 4 + 7. what the server knows is on the page at first paint ───────────────

def _first_paint_page(monkeypatch, *, paused: bool, pool=None):
    from fastapi.testclient import TestClient

    import app.autofill.answer_pack as ap
    from app.api import server
    from app.common import ttl_cache
    prof = SimpleNamespace(
        user_id=None, first_name="Sam", last_name="Tester",
        search_paused_at=datetime.utcnow() if paused else None, pause_reason="",
        last_meaningful_activity_at=datetime.utcnow(), work_authorization="",
        visa_status="", requires_sponsorship=False)
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: prof)
    ttl_cache.invalidate("pool_count:local")
    if pool is not None:
        ttl_cache.put("pool_count:local", pool, 60)
    try:
        r = TestClient(server.app).get("/dashboard")
    finally:
        ttl_cache.invalidate("pool_count:local")
    assert r.status_code == 200
    return r.text


def test_a_paused_search_is_paused_at_first_paint(monkeypatch):
    page = _first_paint_page(monkeypatch, paused=True)
    assert re.search(r'id="measured-status"[^>]*>Search paused<', page)
    assert "Checking boards" not in page
    banner = re.search(r'<div id="hp-search-state"[^>]*data-ssr="1"[^>]*>(.*?)</div>', page)
    assert banner and "You paused your search" in banner.group(1)
    assert 'data-search-action="resume"' in banner.group(1)
    m = re.search(r"let _searchState = (\{.*?\});", page)
    assert m and json.loads(m.group(1))["state"] == "paused"


def test_a_running_search_never_claims_a_check_in_progress(monkeypatch):
    page = _first_paint_page(monkeypatch, paused=False)
    assert re.search(r'id="measured-status"[^>]*>Board monitoring active<', page)
    assert 'data-ssr="1"' not in page
    assert "let _searchState = null;" in page


def test_the_header_has_the_name_and_the_cached_pool_at_first_paint(monkeypatch):
    page = _first_paint_page(monkeypatch, paused=False, pool=3840)
    assert re.search(r'id="header-profile-label">Sam<', page)
    assert re.search(r'id="header-avatar-ph"[^>]*>S<', page)
    assert 'id="hp-greet-name">, Sam<' in page
    assert re.search(r'id="stat-pool"[^>]*>3.8k<', page)
    assert 'id="explorer-total-count">3,840<' in page
    # Never computed on the page's critical path: a cold cache shows "…".
    cold = _first_paint_page(monkeypatch, paused=False)
    assert re.search(r'id="stat-pool"[^>]*>…<', cold)


def test_the_name_does_not_wait_behind_the_avatar():
    body = _function("loadSidebarAvatar")
    # The avatar fetch is fired, not awaited, before the profile read.
    avatar = body.index("/api/profile/avatar")
    assert "await fetch('/api/profile/avatar'" not in body
    assert body.index("await fetch('/api/profile'") > avatar


def test_fmt_k_matches_the_dashboards():
    from app.api.server import _fmt_k
    assert [_fmt_k(n) for n in (0, 950, 1000, 3840, 12345, 99999, 120400, 1_250_000)] == \
        ["0", "950", "1k", "3.8k", "12.3k", "100k", "120k", "1.3M"]


# ── 8. numbers that claim the same thing count the same way ─────────────────

def test_the_pool_tile_and_all_jobs_badge_have_one_writer():
    # /api/stats total_jobs (the all-time pool) never reaches the tile...
    stats = _function("loadStats")
    assert "stat-pool" not in stats
    # ...nor the all-history closed count the Ghost badge.
    assert "stats.closed_jobs;" not in stats and "stats.closed_jobs " not in stats
    # One writer for the tile and the badge.
    writer = _function("_setFoundJobs")
    assert "stat-pool" in writer and "explorer-total-count" in writer
    assert HTML.count("getElementById('stat-pool')") == 1
    # The explorer only writes it for the All Jobs window (server-declared).
    jobs = _function("loadJobsPage")
    assert "data.found_window" in jobs and "_setFoundJobs(" in jobs
    assert "getElementById('explorer-total-count').textContent" not in jobs


def test_jobs_api_declares_whether_total_open_is_the_found_window():
    import inspect

    from app.api import server
    src = inspect.getsource(server.api_jobs)
    assert '"found_window"' in src and "explorer_max_age_days" in src


def test_an_unknown_count_is_never_no_jobs_yet():
    stats = _function("loadStats")
    i = stats.index("No jobs yet — run Discover.")
    assert "stats.degraded" in stats[i - 800:i], "a timed-out count must not read as 'no jobs'"
    assert "if (_bandsKnown) renderChart(" in stats


def test_the_session_count_is_not_called_the_board():
    panel = _function("_renderWelcomePanel")
    assert "on your board</p>" not in panel
    assert "new match${n === 1 ? '' : 'es'} found this session" in panel
    assert "match${found === 1 ? '' : 'es'} on your board" not in _function("_wBoostHtml")
