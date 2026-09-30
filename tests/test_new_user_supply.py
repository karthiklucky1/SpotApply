"""A new user's first matches: supply, not scoring speed (2026-09-30).

Production (counts only): an internship-only student signed up and saw no match
for 10+ minutes. 1,980 postings were adopted in 90 seconds — 1,350 of them were
expired by the scoring gate the moment they arrived (21-day adoption vs a 5-day
gate), 100 of 103 prescores went on full-time roles he does not want, and the
onboarding search was skipped because the RAW pool count looked large.

The owner's rule: "give jobs from the past 2 weeks too, but fresh jobs first,
and make sure those are not closed ones". These tests pin each part:

- adoption copies only what the user's scoring window and job type can use;
- while the welcome window is open the queue reaches back 14 days, fresh first;
- a posting older than the normal window reaches the board only with POSITIVE
  evidence it is open, and then stays for the normal window from delivery;
- the first results keep going until the target is met (same guards each round);
- when there is honestly nothing more, the panel says so and offers ways to
  widen the search — never a lower bar.

Synthetic users and rows only.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile
from app.matching.filters.rule_filter import classify_job_type
from app.strategy import welcome

_P = "nus-"
_U = "nus-user"


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    monkeypatch.setattr(settings, "welcome_boost_minutes", 60)
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 5)
    monkeypatch.setattr(settings, "shortlist_max_age_days", 5)
    monkeypatch.setattr(settings, "welcome_catchup_days", 14)
    welcome._BOOSTS.clear()
    welcome._FIRST_DONE.clear()
    yield
    welcome._BOOSTS.clear()
    welcome._FIRST_DONE.clear()
    with get_session() as s:
        ids = [r if not isinstance(r, tuple) else r[0] for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if ids:
            s.exec(delete(Application).where(Application.job_id.in_(ids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
        s.commit()


def _job(ext, *, days=0.0, user=_U, **kw):
    now = datetime.utcnow()
    with get_session() as s:
        j = Job(source=JobSource.GREENHOUSE, external_id=_P + ext, company="Acme",
                title=kw.pop("title", f"Analyst {ext}"), url=f"https://x.test/{_P}{ext}",
                description="d", user_id=user, first_seen=now - timedelta(days=days),
                discovered_at=now - timedelta(days=days), **kw)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


# ── job type: word-bounded, and applied at adoption ─────────────────────────

@pytest.mark.parametrize("title,want", [
    ("Software Engineering Intern", "internship"),
    ("Data Science Internship (Summer 2027)", "internship"),
    ("Co-op, Mechanical Engineering", "internship"),
    ("Werkstudent Data", "internship"),
    ("Internal Tools Engineer", "full_time"),
    ("International Sales Manager", "full_time"),
    ("Internet Platform Developer", "full_time"),
])
def test_internship_titles_are_word_bounded(title, want):
    assert classify_job_type(title) == want


def test_adoption_copies_only_the_job_type_the_user_wants():
    from app.strategy.adoption import _job_type_gate
    only = _job_type_gate(SimpleNamespace(job_type_preference="internship"))
    assert only("Data Analyst Intern") and not only("Data Analyst")
    full = _job_type_gate(SimpleNamespace(job_type_preference="full_time",
                                          include_internships_in_discovery=False))
    assert full("Data Analyst") and full("Internal Audit Analyst") and not full("Data Intern")
    both = _job_type_gate(SimpleNamespace(job_type_preference="both"))
    assert both("Data Analyst") and both("Data Intern")
    assert _job_type_gate(None)("anything")


# ── the window: 14 days only while the welcome window is open ───────────────

def test_the_catch_up_window_is_only_for_the_first_hour():
    assert welcome.scoring_window_days(_U) == 5
    welcome.begin(_U, "test")
    assert welcome.scoring_window_days(_U) == 14
    assert welcome.scoring_window_days("someone-else") == 5


def test_the_queue_reaches_back_14_days_fresh_first():
    from app.strategy import scoring_lane as sl
    fresh_weak = _job("q-fresh", days=1, prescore=30.0)
    old_strong = _job("q-old", days=9, prescore=95.0)
    too_old = _job("q-ancient", days=20, prescore=99.0)
    assert sl._user_queue(_U, 10) == [fresh_weak], "outside the window: normal 5 days"
    welcome.begin(_U, "test")
    got = sl._user_queue(_U, 10)
    assert got == [fresh_weak, old_strong], "fresh first, then catch-up; never past 14 days"
    assert too_old not in got


def test_the_expiry_sweep_spares_a_new_users_catch_up_postings():
    from app.strategy.scoring_lane import _expire_stale_unscored
    welcome.begin(_U, "test")
    kept = _job("x-9d", days=9)
    gone = _job("x-20d", days=20)
    other = _job("x-other-9d", days=9, user=_P + "other")
    _expire_stale_unscored()
    with get_session() as s:
        assert s.get(Job, kept).rerank_score is None
        assert s.get(Job, gone).expired_at is not None
        assert s.get(Job, other).expired_at is not None, "only the boosted user is widened"


# ── "make sure those are not closed ones" ───────────────────────────────────

def _shared(ext, *, closed=False, seen_hours=None):
    from app.discovery.pipeline import SHARED_POOL_USER
    last = None if seen_hours is None else datetime.utcnow() - timedelta(hours=seen_hours)
    return _job(ext, days=9, user=SHARED_POOL_USER, is_closed=closed, last_seen=last)


def test_open_needs_positive_evidence(monkeypatch):
    from app.strategy import delivery_gate as dg
    checks = []

    def _verify(state):
        def _f(src, ext, url):
            checks.append(ext)
            return state, "checked"
        return _f

    _shared("o-recent", seen_hours=3)
    _shared("o-closed", closed=True, seen_hours=1)
    _shared("o-stale", seen_hours=200)
    monkeypatch.setattr(dg, "verify_for_delivery", _verify("unverifiable"))
    assert dg.confirmed_open(JobSource.GREENHOUSE, _P + "o-recent", "u") is True
    assert dg.confirmed_open(JobSource.GREENHOUSE, _P + "o-closed", "u") is False
    assert checks == [], "the board's own listing answered both"
    # Not seen lately and the check cannot confirm it: NOT delivered.
    assert dg.confirmed_open(JobSource.GREENHOUSE, _P + "o-stale", "u") is False
    assert dg.confirmed_open(JobSource.GREENHOUSE, _P + "o-missing", "u") is False
    monkeypatch.setattr(dg, "verify_for_delivery", _verify(dg.JobLivenessState.LIVE.value))
    assert dg.confirmed_open(JobSource.GREENHOUSE, _P + "o-stale", "u") is True


def test_a_catch_up_delivery_needs_open_evidence_and_is_marked(monkeypatch):
    from app.strategy import delivery_gate as dg
    from app.strategy import scoring_lane as sl
    from app.strategy import slate
    welcome.begin(_U, "test")
    fresh = _job("d-fresh", days=1)
    old_open = _job("d-old-open", days=9)
    old_unknown = _job("d-old-unknown", days=9)
    monkeypatch.setattr(dg, "confirmed_open",
                        lambda src, ext, url, **k: ext == _P + "d-old-open")
    monkeypatch.setattr(dg, "verified_dead", lambda *a, **k: False)

    def _place(session, job, score, user_id=None, **kw):
        session.add(Application(user_id=user_id, job_id=job.id,
                                status=ApplicationStatus.SHORTLISTED))
        session.commit()
        return SimpleNamespace(created=True, outcome="placed")
    monkeypatch.setattr(slate, "place", _place)
    stats: dict = {"shortlisted": 0}
    sl._shortlist_user(_U, [(fresh, 90.0), (old_open, 88.0), (old_unknown, 87.0)], stats)
    with get_session() as s:
        apps = {a.job_id: a for a in s.exec(select(Application).where(
            Application.job_id.in_([fresh, old_open, old_unknown]))).all()}
    assert set(apps) == {fresh, old_open}, "an unconfirmed catch-up posting is not delivered"
    assert stats.get("catchup_unconfirmed") == 1
    assert apps[old_open].delivered_catchup and not apps[fresh].delivered_catchup


def test_the_board_keeps_a_catch_up_delivery_but_nothing_else_that_old():
    from app.api.server import _shortlist_fresh_clause
    marked = _job("r-marked", days=9)
    unmarked = _job("r-unmarked", days=9)
    with get_session() as s:
        for jid, flag in ((marked, True), (unmarked, False)):
            s.add(Application(user_id=_U, job_id=jid, status=ApplicationStatus.SHORTLISTED,
                              delivered_catchup=flag))
        s.commit()
        shown = {r if not isinstance(r, tuple) else r[0] for r in s.exec(
            select(Application.job_id).join(Job).where(
                Application.job_id.in_([marked, unmarked]), _shortlist_fresh_clause())).all()}
    assert shown == {marked}


def test_hygiene_keeps_a_catch_up_delivery():
    from app.strategy.shortlist_hygiene import prune_stale_shortlist
    marked = _job("h-marked", days=9)
    unmarked = _job("h-unmarked", days=9)
    with get_session() as s:
        for jid, flag in ((marked, True), (unmarked, False)):
            s.add(Application(user_id=_U, job_id=jid, status=ApplicationStatus.SHORTLISTED,
                              delivered_catchup=flag))
        s.commit()
    prune_stale_shortlist()
    with get_session() as s:
        st = {a.job_id: a.status for a in s.exec(select(Application).where(
            Application.job_id.in_([marked, unmarked]))).all()}
    assert st[marked] == ApplicationStatus.SHORTLISTED
    assert st[unmarked] != ApplicationStatus.SHORTLISTED


# ── first results: keep going to the target, same guards each round ─────────

def test_first_results_run_rounds_until_the_target(monkeypatch):
    from app.strategy import scoring_lane as sl
    monkeypatch.setattr(settings, "welcome_first_scores", 8)
    monkeypatch.setattr(settings, "welcome_target_matches", 5)
    monkeypatch.setattr(settings, "welcome_first_rounds", 4)
    board = {"n": 0}

    def _round(uid, n):
        board["n"] += 2
        return {"queued": n, "scored": n, "shortlisted": 2}
    monkeypatch.setattr(sl, "score_user_now", _round)
    monkeypatch.setattr(welcome, "_matches_since", lambda uid, since: board["n"])
    welcome.begin(_U, "test")
    out = welcome.first_results(_U)
    assert out["rounds"] == 3 and board["n"] == 6          # 2, 4, 6 ≥ 5 → stop


@pytest.mark.parametrize("reply", [{"skipped": "budget", "queued": 0, "scored": 0},
                                   {"queued": 0, "scored": 0}])
def test_first_results_stop_when_nothing_is_allowed_or_left(monkeypatch, reply):
    from app.strategy import scoring_lane as sl
    monkeypatch.setattr(settings, "welcome_first_scores", 8)
    monkeypatch.setattr(settings, "welcome_first_rounds", 4)
    calls = []
    monkeypatch.setattr(sl, "score_user_now", lambda uid, n: calls.append(n) or dict(reply))
    monkeypatch.setattr(welcome, "_matches_since", lambda uid, since: 0)
    welcome.begin(_U, "test")
    welcome.first_results(_U)
    assert len(calls) == 1


# ── the onboarding search reads USABLE postings, not the raw count ──────────

def test_usable_count_ignores_expired_rejected_and_held_postings():
    from app.strategy.adoption import _usable_count
    _job("u-ok", days=1)
    _job("u-good", days=2, rerank_score=80.0)
    _job("u-rejected", days=1, rerank_score=20.0)
    _job("u-expired", days=1, rerank_score=8.0)
    _job("u-held", days=1, eligibility="unknown")
    _job("u-closed", days=1, is_closed=True)
    _job("u-old", days=9)
    assert _usable_count(_U) == 2
    welcome.begin(_U, "test")
    assert _usable_count(_U) == 3, "the catch-up window counts the 9-day posting"


# ── honest when supply is thin ──────────────────────────────────────────────

def _prof(**kw):
    base = dict(job_type_preference="internship", open_to_relocation=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_thin_supply_speaks_only_when_nothing_is_left_to_check(monkeypatch):
    monkeypatch.setattr(settings, "welcome_target_matches", 5)
    old = datetime.utcnow() - timedelta(minutes=10)
    st = {"boost_active": True, "waiting": 0, "new_matches": 2, "checked": 140, "_started": old}
    out = welcome.thin_supply(_prof(), st)
    assert out["found"] == 2 and out["target"] == 5
    assert [t["key"] for t in out["tips"]] == ["both", "relocate", "roles"]
    assert welcome.thin_supply(_prof(), {**st, "waiting": 12}) is None, "still checking"
    assert welcome.thin_supply(_prof(), {**st, "new_matches": 5}) is None
    assert welcome.thin_supply(_prof(), {**st, "_started": datetime.utcnow()}) is None
    assert welcome.thin_supply(_prof(), {**st, "boost_active": False}) is None
    tips = welcome.thin_supply(_prof(job_type_preference="both", open_to_relocation=True), st)
    assert [t["key"] for t in tips["tips"]] == ["roles"]


def test_the_panel_offers_the_tips_and_never_a_lower_bar():
    html = (Path(__file__).resolve().parent.parent / "app/templates/dashboard.html").read_text()
    body = html[html.index("function widenSearch"):]
    body = body[:body.index("function _welcomeMoreBtn")]
    assert "openRolesModal" in body and "open_to_relocation" in body
    assert "job_type_preference" in body and "_startSearchNow" in body
    assert not re.search(r"threshold|min_match|score", body), "widening never lowers the bar"
    assert "thin_supply" in html


def test_the_status_route_hides_the_internal_start_time():
    from app.api import server
    import inspect
    src = inspect.getsource(server.welcome_status_api)
    assert 'out.pop("_started", None)' in src


# ── the measurement: minutes to the first and fifth match ───────────────────

def test_admin_first_hour_reports_minutes_without_identity(monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import server
    uid = _P + "admin-view-user-000"
    started = datetime.utcnow() - timedelta(hours=1)
    with get_session() as s:
        s.add(UserProfile(user_id=uid, feed_started_at=started, email="x@example.test"))
        s.commit()
    jids = [_job(f"a-{i}", user=uid) for i in range(5)]
    with get_session() as s:
        for i, jid in enumerate(jids):
            s.add(Application(user_id=uid, job_id=jid, status=ApplicationStatus.SHORTLISTED,
                              created_at=started + timedelta(minutes=2 + i)))
        s.commit()
    body = TestClient(server.app).get("/api/admin/first-hour").json()
    row = next(r for r in body["rows"] if r["user"] == uid[:8])
    assert row["minutes_to_first"] == 2.0 and row["minutes_to_fifth"] == 6.0
    assert "example.test" not in str(body) and uid not in str(body)


def test_admin_first_hour_is_admin_only(monkeypatch):
    from fastapi import HTTPException
    from app.api import server
    monkeypatch.setattr(settings, "supabase_url", "https://x.supabase.test")
    monkeypatch.setattr(settings, "database_url", "postgresql://x.test/db")
    monkeypatch.setattr(server, "_get_user_email", lambda req: "someone@example.test")
    assert settings.use_supabase
    with pytest.raises(HTTPException) as e:
        server.admin_first_hour(SimpleNamespace(), 14)
    assert e.value.status_code == 403
