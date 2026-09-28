"""The first hour: new and returning users are served first, see progress, and
get told when their target roles out-rank their resume (2026-09-28).

Production (counts only): a friend returning to a weeks-old account got 3 jobs
in her first hour. Nothing happened on her return, the lane's "empty board
first" rule skipped her (she had old scored jobs), and her roles were
Senior/Architect titles against ~1 year of experience — 20 of 2,139 finals ever
cleared 60. Synthetic users and rows only.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile
from app.matching import finals_budget as fb
from app.strategy import scoring_lane as sl
from app.strategy import welcome


@pytest.fixture(autouse=True)
def _boost_on(monkeypatch):
    monkeypatch.setattr(settings, "welcome_boost_minutes", 60)
    monkeypatch.setattr(settings, "welcome_boost_cap_multiplier", 2.0)
    welcome._BOOSTS.clear()
    yield
    welcome._BOOSTS.clear()


# ── the boost window ─────────────────────────────────────────────────────────

def test_a_boost_lasts_its_window_and_is_not_restarted_inside_it():
    t0 = datetime(2026, 9, 28, 19, 48)
    assert welcome.begin("w-u1", "test", now=t0)
    assert not welcome.begin("w-u1", "again", now=t0 + timedelta(minutes=30))
    assert welcome.is_boosted("w-u1", now=t0 + timedelta(minutes=59))
    assert not welcome.is_boosted("w-u1", now=t0 + timedelta(minutes=61))
    assert welcome.begin("w-u1", "later", now=t0 + timedelta(minutes=61))


def test_no_boost_when_disabled_or_local(monkeypatch):
    assert not welcome.begin("local", "test")
    assert not welcome.begin(None, "test")
    monkeypatch.setattr(settings, "welcome_boost_minutes", 0)
    assert not welcome.begin("w-u2", "test")
    assert not welcome.is_boosted("w-u2")


def test_a_boosted_user_gets_a_bigger_slice_only_while_boosted():
    assert welcome.per_cycle_cap("w-u3", 40) == 40
    welcome.begin("w-u3", "test")
    assert welcome.per_cycle_cap("w-u3", 40) == 80


@pytest.mark.parametrize("idle,returning", [
    (None, True),                       # never acted
    (timedelta(minutes=30), False),     # still in the same session
    (timedelta(hours=23), False),
    (timedelta(hours=25), True),        # past TRIAL_IDLE_AFTER_HOURS (24)
    (timedelta(days=40), True),         # the friend's case
])
def test_what_counts_as_coming_back(idle, returning):
    now = datetime(2026, 9, 28, 20, 0)
    prev = None if idle is None else now - idle
    assert welcome.returning_after_idle(prev, now) is returning


# ── the scoring lane serves boosted users first ──────────────────────────────

def _stub_cycle(monkeypatch, users):
    calls = []
    monkeypatch.setattr(sl, "_expire_stale_unscored",
                        lambda **kw: {"total": 0, "queue_stale": 0,
                                      "ancient_posting": 0, "stopped": ""})
    monkeypatch.setattr(sl, "_scorable_user_ids", lambda: list(users))
    monkeypatch.setattr(sl, "_plan_budget", lambda u: (250, 35))

    def _allow(u, cap):
        calls.append((u, cap))
        return fb.Allowance(0, 40, "delivered 35/35")
    monkeypatch.setattr(sl, "_finals_allowance", _allow)
    monkeypatch.setattr(settings, "scoring_drain_cap", 0)
    return calls


def test_the_returning_user_goes_first_with_a_double_slice(monkeypatch):
    calls = _stub_cycle(monkeypatch, ["w-a", "w-b", "w-returning"])
    welcome.begin("w-returning", "test")
    stats = sl._run_scoring_cycle(None)
    assert calls[0] == ("w-returning", settings.scoring_per_user_cap * 2)
    assert [u for u, _ in calls[1:]] == ["w-a", "w-b"]
    assert all(cap == settings.scoring_per_user_cap for _, cap in calls[1:])
    assert stats.get("boosted_users") == 1


def test_without_a_boost_the_order_is_unchanged(monkeypatch):
    calls = _stub_cycle(monkeypatch, ["w-a", "w-b"])
    sl._run_scoring_cycle(None)
    assert [u for u, _ in calls] == ["w-a", "w-b"]


# ── triggers ─────────────────────────────────────────────────────────────────

def test_resume_upload_and_role_save_start_the_boost(monkeypatch):
    from app.strategy import adoption
    monkeypatch.setattr(adoption, "adopt_and_match", lambda uid: 0)
    monkeypatch.setattr(settings, "onboarding_active_discovery", False)
    adoption.seed_new_user("w-new")
    assert welcome.is_boosted("w-new")


_RU = "w-returner"


@pytest.fixture
def returner():
    def _wipe():
        with get_session() as s:
            s.exec(delete(Application).where(Application.user_id.like("w-%")))
            s.exec(delete(Job).where(Job.user_id.like("w-%")))
            s.exec(delete(UserProfile).where(UserProfile.user_id.like("w-%")))
            s.commit()
    _wipe()
    with get_session() as s:
        s.add(UserProfile(user_id=_RU, target_roles="Backend Engineer",
                          years_experience=1,
                          last_meaningful_activity_at=datetime.utcnow() - timedelta(days=40)))
        s.commit()
    yield _RU
    _wipe()


def _fake_request(method="POST", path="/api/jobs/1/view"):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def test_a_return_after_weeks_welcomes_the_user_once(returner, monkeypatch):
    from app.api import server
    got = []
    monkeypatch.setattr(welcome, "welcome_back", lambda uid: got.append(uid) or True)
    server._LAST_ACTIVE_STAMP.clear()
    server._touch_last_active(returner, meaningful=True,
                              welcome=server._may_welcome(_fake_request()))
    assert got == [returner]
    # The second action is no longer a return.
    server._LAST_ACTIVE_STAMP.clear()
    server._touch_last_active(returner, meaningful=True, welcome=True)
    assert got == [returner]


def test_deleting_the_account_never_starts_a_refresh(returner, monkeypatch):
    from app.api import server
    got = []
    monkeypatch.setattr(welcome, "welcome_back", lambda uid: got.append(uid) or True)
    assert not server._may_welcome(_fake_request("DELETE", "/api/account"))
    assert not server._may_welcome(_fake_request("POST", "/api/account/export"))
    server._LAST_ACTIVE_STAMP.clear()
    server._touch_last_active(returner, meaningful=True,
                              welcome=server._may_welcome(_fake_request("DELETE", "/api/account")))
    assert got == []


def test_the_refresh_skips_a_deleted_account(monkeypatch):
    from app.strategy import adoption
    called = []
    monkeypatch.setattr(adoption, "adopt_shared_jobs", lambda uid: called.append(uid) or 0)
    monkeypatch.setattr(welcome, "kick_scoring", lambda: called.append("kick"))
    welcome._refresh("w-gone-nobody")
    assert called == []


# ── the role check ───────────────────────────────────────────────────────────

FRIEND_ROLES = ["MuleSoft Integration Developer", "Cloud Solutions Architect",
                "DevOps Engineer", "AWS Solutions Architect", "Senior Backend Engineer"]


def test_senior_roles_against_one_year_get_junior_versions():
    tip = welcome.seniority_tip(SimpleNamespace(years_experience=1), FRIEND_ROLES)
    assert tip["senior_roles"] == ["Cloud Solutions Architect", "AWS Solutions Architect",
                                   "Senior Backend Engineer"]
    assert tip["suggest"] == ["Cloud Engineer", "AWS Engineer", "Backend Engineer"]


@pytest.mark.parametrize("years", [0, 3, 8])
def test_no_tip_outside_one_to_two_years(years):
    """0 is also what an unparsed profile stores — never tell an experienced
    person they are junior."""
    assert welcome.seniority_tip(SimpleNamespace(years_experience=years), FRIEND_ROLES) is None


def test_no_tip_when_the_junior_roles_are_already_there_or_none_exist():
    p = SimpleNamespace(years_experience=2)
    assert welcome.seniority_tip(p, ["Senior Backend Engineer", "Backend Engineer"]) is None
    assert welcome.seniority_tip(p, ["Engineering Manager"]) is None
    assert welcome.seniority_tip(p, ["Backend Engineer", "Data Analyst"]) is None


# ── the progress panel ───────────────────────────────────────────────────────

def test_status_counts_this_users_first_hour(returner):
    now = datetime.utcnow()
    welcome.begin(returner, "test", now=now - timedelta(minutes=10))
    with get_session() as s:
        ids = []
        for i, (scored, pre) in enumerate([(True, True), (False, True), (False, False), (False, False)]):
            j = Job(user_id=returner, source=JobSource.GREENHOUSE, external_id=f"w-{i}",
                    company="Co", title="Backend Engineer", url=f"https://x/w{i}",
                    description="d", first_seen=now - timedelta(hours=1),
                    discovered_at=now - timedelta(hours=1),
                    rerank_score=80.0 if scored else None,
                    scored_at=now - timedelta(minutes=5) if scored else None,
                    prescored_at=now - timedelta(minutes=6) if pre else None)
            s.add(j)
            s.flush()
            ids.append(j.id)
        s.add(Application(user_id=returner, job_id=ids[0], status=ApplicationStatus.SHORTLISTED,
                          created_at=now - timedelta(minutes=4)))
        # Another user's rows never count.
        s.add(Job(user_id="w-other", source=JobSource.GREENHOUSE, external_id="w-o",
                  company="Co", title="t", url="u", description="d", first_seen=now,
                  scored_at=now))
        s.commit()
    st = welcome.status(returner)
    assert st["boost_active"] and 49 <= st["minutes_left"] <= 50
    assert st["pool_fresh"] == 4
    assert st["checked"] == 2
    assert st["waiting"] == 3
    assert st["new_matches"] == 1


def test_the_route_refuses_anonymous_callers(monkeypatch):
    from fastapi import HTTPException
    from app.api import server
    monkeypatch.setattr(server, "_get_user_id", lambda request: None)
    with pytest.raises(HTTPException) as e:
        server.welcome_status_api(request=None)
    assert e.value.status_code == 401


def test_the_route_returns_progress_and_tip(returner, monkeypatch):
    from app.api import server
    from app.common import ttl_cache
    monkeypatch.setattr(server, "_get_user_id", lambda request: returner)
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == returner)).first()
        p.target_roles = "Senior Backend Engineer, Cloud Solutions Architect"
        s.add(p)
        s.commit()
    ttl_cache.invalidate(f"welcome:{returner}")
    welcome.begin(returner, "test")
    d = server.welcome_status_api(request=None)
    assert d["boost_active"] is True
    assert d["role_tip"]["suggest"] == ["Backend Engineer", "Cloud Engineer"]
