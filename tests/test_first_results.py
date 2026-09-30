"""The first minute after a resume upload (2026-09-30).

Production (counts only): new users waited 2-4 minutes for a first match, with
nothing on screen in between but a progress bar. Three things change that, and
each has a way to go wrong that these tests pin:

- FIRST RESULTS: the few most promising adopted jobs are scored right after
  adoption. It must buy nothing a cycle would not (same breaker, platform
  budget and plan allowance), run only inside the welcome window, and happen
  BEFORE the slower matching pass.
- SHOW THE WORK: the panel lists REAL jobs with their REAL state. Nothing is
  called a match before it is on the board, the jobs kept off the board are
  counted rather than hidden, and another user's rows never appear.
- NOTHING COVERS IT: the resume questions sit below the panel, and the first
  match no longer reloads the page out from under the user.

Synthetic users and rows only.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import delete

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile
from app.matching import finals_budget as fb
from app.strategy import scoring_lane as sl
from app.strategy import welcome

_U = "fr-user"


@pytest.fixture(autouse=True)
def _boost_on(monkeypatch):
    monkeypatch.setattr(settings, "welcome_boost_minutes", 60)
    monkeypatch.setattr(settings, "welcome_first_scores", 8)
    welcome._BOOSTS.clear()
    welcome._FIRST_DONE.clear()
    yield
    welcome._BOOSTS.clear()
    welcome._FIRST_DONE.clear()


# ── first results: same guards as a cycle, only earlier ─────────────────────

def _stub_scoring(monkeypatch, allow_n=8, queue=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10)):
    import app.matching.reranker as rr
    monkeypatch.setattr(rr, "any_provider_available", lambda *a, **k: True)
    monkeypatch.setattr(rr, "llm_budget_exhausted", lambda *a, **k: False)
    monkeypatch.setattr(sl, "_finals_allowance",
                        lambda uid, cap: fb.Allowance(min(cap, allow_n), 40, "ok"))
    asked = {}

    def _queue(uid, cap, only_unprescored=False):
        asked["cap"] = cap
        return list(queue)[:cap]
    monkeypatch.setattr(sl, "_user_queue", _queue)
    monkeypatch.setattr(sl, "_make_ctx", lambda uid, gate=None, drain_only=False: object())
    monkeypatch.setattr(sl, "_score_job",
                        lambda jid, ctx: ("scored", jid, 80.0 if jid % 2 else 50.0, "anthropic"))
    placed = []
    monkeypatch.setattr(sl, "_shortlist_user",
                        lambda uid, scored, stats: placed.extend(scored))
    return asked, placed


def test_first_results_score_the_top_n_and_place_them(monkeypatch):
    asked, placed = _stub_scoring(monkeypatch)
    st = sl.score_user_now(_U, 8)
    assert asked["cap"] == 8 and st["queued"] == 8 and st["scored"] == 8
    assert [j for j, _ in placed] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_the_plan_allowance_caps_first_results(monkeypatch):
    asked, _ = _stub_scoring(monkeypatch, allow_n=3)
    assert sl.score_user_now(_U, 8)["queued"] == 3
    assert asked["cap"] == 3


@pytest.mark.parametrize("breaker,budget,allow_n", [
    (False, False, 8),     # every provider cooling down
    (True, True, 8),       # platform budget spent
    (True, False, 0),      # the user's day is spent
])
def test_first_results_buy_nothing_a_cycle_would_not(monkeypatch, breaker, budget, allow_n):
    import app.matching.reranker as rr
    asked, placed = _stub_scoring(monkeypatch, allow_n=allow_n)
    monkeypatch.setattr(rr, "any_provider_available", lambda *a, **k: breaker)
    monkeypatch.setattr(rr, "llm_budget_exhausted", lambda *a, **k: budget)
    monkeypatch.setattr(settings, "local_score_fallback", False)
    st = sl.score_user_now(_U, 8)
    assert st.get("skipped") and st["scored"] == 0 and not placed and "cap" not in asked


def test_first_results_run_once_per_welcome_window(monkeypatch):
    calls = []
    monkeypatch.setattr(sl, "score_user_now", lambda uid, n: calls.append((uid, n)) or {})
    welcome._FIRST_DONE.clear()
    assert welcome.first_results(_U).get("skipped")          # no window, no run
    t0 = datetime.utcnow() - timedelta(minutes=5)
    welcome.begin(_U, "test", now=t0)
    welcome.first_results(_U)
    assert calls == [(_U, 8)]
    # A role save inside the window adopts again but does not pay again.
    assert welcome.first_results(_U).get("skipped") and len(calls) == 1
    # A new window (e.g. a return weeks later) runs again.
    welcome._BOOSTS.clear()
    welcome.begin(_U, "later")
    welcome.first_results(_U)
    assert len(calls) == 2
    monkeypatch.setattr(settings, "welcome_first_scores", 0)
    welcome._FIRST_DONE.clear()
    assert welcome.first_results(_U).get("skipped") and len(calls) == 2


def test_a_paused_account_gets_no_first_results(monkeypatch):
    _stub_scoring(monkeypatch)
    from app.api import server
    monkeypatch.setattr(settings, "dormant_user_grace_days", 1)
    monkeypatch.setattr(server, "_user_may_spend", lambda prof: False)
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id == _U))
        s.add(UserProfile(user_id=_U))
        s.commit()
    try:
        assert sl.score_user_now(_U, 8).get("skipped") == "compute policy"
    finally:
        with get_session() as s:
            s.exec(delete(UserProfile).where(UserProfile.user_id == _U))
            s.commit()


def test_first_results_come_before_the_slow_matching_pass(monkeypatch):
    from app.strategy import adoption
    order = []
    monkeypatch.setattr(adoption, "adopt_shared_jobs", lambda uid: order.append("adopt") or 5)
    monkeypatch.setattr(welcome, "first_results", lambda uid: order.append("first") or {})
    import app.matching.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_matching", lambda uid: order.append("match"))
    monkeypatch.setattr(adoption, "_score_first_slice", lambda uid: order.append("slice"))
    adoption.adopt_and_match(_U)
    assert order[:2] == ["adopt", "first"]


# ── show the work: real jobs, real states ────────────────────────────────────

@pytest.fixture
def pool():
    def _wipe():
        with get_session() as s:
            s.exec(delete(Application).where(Application.user_id.like("fr-%")))
            s.exec(delete(Job).where(Job.user_id.like("fr-%")))
            s.exec(delete(UserProfile).where(UserProfile.user_id.like("fr-%")))
            s.commit()
    _wipe()
    now = datetime.utcnow()
    welcome.begin(_U, "test", now=now - timedelta(minutes=5))
    bar = float(settings.shortlist_score_threshold)
    rows = [  # key, score, scored minutes ago, on board
        ("match", bar + 15, 3, True),
        ("close", bar - 10, 2, False),
        ("ghost", 5.0, 2, False),
        ("old", bar - 20, 60 * 24, False),      # checked before the window: not this hour's work
        ("wait1", None, None, False),
        ("wait2", None, None, False),
    ]
    with get_session() as s:
        ids = {}
        for i, (key, score, ago, board) in enumerate(rows):
            j = Job(user_id=_U, source=JobSource.GREENHOUSE, external_id=f"fr-{key}",
                    company=f"Co {key}", title=f"Analyst {key}", url=f"https://x/fr{i}",
                    location="Columbus, OH", description="d",
                    first_seen=now - timedelta(hours=2, minutes=i),
                    discovered_at=now - timedelta(hours=2), rerank_score=score,
                    scored_at=(now - timedelta(minutes=ago)) if ago else None)
            s.add(j)
            s.flush()
            ids[key] = j.id
            if board:
                s.add(Application(user_id=_U, job_id=j.id, status=ApplicationStatus.SHORTLISTED,
                                  created_at=now - timedelta(minutes=3)))
        s.add(Job(user_id="fr-other", source=JobSource.GREENHOUSE, external_id="fr-o",
                  company="Other Co", title="Not yours", url="https://x/o", description="d",
                  first_seen=now, discovered_at=now))
        s.commit()
    yield ids
    _wipe()


def test_the_preview_shows_each_job_in_its_real_state(pool):
    st = welcome.status(_U)
    states = {r["title"].split()[-1]: r["state"] for r in st["preview"]}
    assert states["match"] == "match"
    assert states["wait1"] == "checking" and states["wait2"] == "checking"
    assert states["close"] == "passed"
    assert states["ghost"] == "filtered"
    assert "old" not in states, "a verdict from before the window is not this hour's work"
    assert all("Not yours" != r["title"] for r in st["preview"])
    # Order on screen: matches, then the jobs being checked, then the misses.
    order = [r["state"] for r in st["preview"]]
    assert order.index("match") < order.index("checking") < order.index("passed")
    assert st["passed_count"] == 2 and st["bar"] == settings.shortlist_score_threshold


def test_only_a_job_on_the_board_is_called_a_match(pool):
    """A score over the bar is not a match until slate.place() put it on the board."""
    with get_session() as s:
        j = s.get(Job, pool["wait1"])
        j.rerank_score = settings.shortlist_score_threshold + 20.0
        j.scored_at = datetime.utcnow()
        s.add(j)
        s.commit()
    rows = welcome.status(_U)["preview"]
    assert [r["title"] for r in rows if r["state"] == "match"] == ["Analyst match"]


def test_no_preview_outside_the_window(pool):
    welcome._BOOSTS.clear()
    assert "preview" not in welcome.status(_U)


def test_the_resume_summary_says_only_what_the_profile_holds():
    p = UserProfile(user_id=_U, current_title="Data Analyst", years_experience=3,
                    key_skills="SQL, Python, Tableau, , Excel")
    rs = welcome.resume_summary(p, ["Data Analyst", ""])
    assert rs == {"title": "Data Analyst", "years": 3, "skills": ["SQL", "Python", "Tableau", "Excel"],
                  "skills_count": 4, "roles": ["Data Analyst"]}
    assert welcome.resume_summary(UserProfile(user_id=_U), []) is None
    assert welcome.resume_summary(None, ["x"]) is None


def test_the_route_adds_the_resume_summary_only_while_boosted(pool, monkeypatch):
    from app.api import server
    from app.common import ttl_cache
    with get_session() as s:
        s.add(UserProfile(user_id=_U, current_title="Data Analyst", key_skills="SQL",
                          target_roles="Data Analyst"))
        s.commit()
    monkeypatch.setattr(server, "_get_user_id", lambda request: _U)
    ttl_cache.invalidate(f"welcome:{_U}")
    assert server.welcome_status_api(request=None)["resume"]["skills"] == ["SQL"]
    welcome._BOOSTS.clear()
    ttl_cache.invalidate(f"welcome:{_U}")
    assert "resume" not in server.welcome_status_api(request=None)


def test_a_failed_read_is_reported_and_never_cached(pool, monkeypatch):
    from app.api import server
    from app.common import ttl_cache
    monkeypatch.setattr(server, "_get_user_id", lambda request: _U)
    ttl_cache.invalidate(f"welcome:{_U}")

    def _boom(uid):
        raise RuntimeError("canceling statement due to statement timeout")
    monkeypatch.setattr(welcome, "status", _boom)
    d = server.welcome_status_api(request=None)
    assert d["degraded"] is True and d["boost_active"] is True
    assert ttl_cache.get(f"welcome:{_U}") is None


def test_the_counts_are_bounded_to_the_fresh_window():
    """No index covers scored_at/prescored_at, so every "checked" read carries
    the fresh bound — without it the walk covered the user's whole history."""
    import inspect
    src = inspect.getsource(welcome.status) + inspect.getsource(welcome._preview)
    assert src.count("_fresh(fresh_after)") + src.count(", fresh,") >= 3
    assert "_bound_statements(s)" in inspect.getsource(welcome.status)
    assert "_bound_statements(s)" in inspect.getsource(welcome._preview)


# ── the page: nothing covers the panel, nothing reloads it away ──────────────

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


def test_every_upload_path_shows_the_panel_while_the_resume_is_read():
    for name in ("uploadResume", "obUploadResume"):
        body = _function(name)
        assert body.index("welcomeStarting('reading')") < body.index("/api/resume/extract-profile"), name
    assert "welcomeStarting('finding')" in _function("_afterResumeChange")


def test_the_resume_questions_do_not_cover_the_panel():
    body = _function("_afterResumeChange")
    assert "position:fixed" not in body and "backdrop-filter" not in body
    assert "insertAdjacentElement('afterend'" in body


def test_the_first_match_does_not_reload_the_page_mid_flow():
    i = HTML.index("if (emptyState && rendered === 0 && server > 0")
    guard = HTML[i - 600:i + 200]
    assert "resume-review-card" in guard and "_welcomeStarting" in guard and "!welcomeBusy" in guard


def test_fast_polling_is_limited_to_the_first_ten_minutes_of_a_visible_tab():
    body = _function("loadWelcomePanel")
    assert "document.hidden" in body and "minutes_left || 0) >= 50" in body
    assert "new_matches || 0) === 0" not in body, "no-match users must not poll every 3 s for an hour"


def test_job_text_is_escaped_everywhere_the_panel_writes_it():
    for fn in ("_wRow", "_welcomeFirstMatch"):
        body = _function(fn)
        for field in ("r.title", "m.title", "m.company"):
            if field in body:
                assert f"_wEsc({field})" in body, (fn, field)
    row = _function("_wRow")
    assert "map(_wEsc)" in row          # company · location
