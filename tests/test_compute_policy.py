"""Background-compute lifecycle (audit 2026-09-25, priority 3).

Controlled clock for every boundary, the two enforcement points (queue time and
immediately before a paid provider call), atomic reservations under concurrent
workers, and the pause/resume routes. Synthetic users only; rows prefixed
``cpol_`` and removed by that prefix.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.common import compute_policy as cp
from app.common import daily_counter
from app.db.init_db import get_session
from app.db.models import UserProfile

pytestmark = pytest.mark.compute_policy
_P = "cpol_"
NOW = datetime(2026, 9, 26, 12, 0, 0)


class _Prof:
    def __init__(self, last=None, paused=None, reason=""):
        self.user_id = _P + "x"
        self.last_meaningful_activity_at = last
        self.search_paused_at = paused
        self.pause_reason = reason


@pytest.fixture(autouse=True)
def _clean():
    yield
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
        s.commit()


# ── the state machine, on a controlled clock ─────────────────────────────────

@pytest.mark.parametrize("hours_ago,state", [
    (0.1, cp.ACTIVE), (23.9, cp.ACTIVE),
    (24.1, cp.IDLE), (71.9, cp.IDLE),
    (72.1, cp.DORMANT), (24 * 30, cp.DORMANT),
])
def test_state_boundaries(hours_ago, state):
    p = _Prof(last=NOW - timedelta(hours=hours_ago))
    assert cp.search_state(p, now=NOW).state == state


def test_never_engaged_is_not_grandfathered_as_active():
    """The old last_active_at was stamped by polling; NULL here proves nothing."""
    assert cp.search_state(_Prof(last=None), now=NOW).state == cp.DORMANT


def test_what_each_state_allows():
    active = cp.search_state(_Prof(last=NOW), now=NOW)
    idle = cp.search_state(_Prof(last=NOW - timedelta(hours=30)), now=NOW)
    dormant = cp.search_state(_Prof(last=NOW - timedelta(days=5)), now=NOW)
    assert active.allows(cp.PAID_AI) and active.allows(cp.QUEUE)
    assert idle.allows(cp.QUEUE) and not idle.allows(cp.PAID_AI) and not idle.allows(cp.RESEARCH)
    assert not dormant.allows(cp.QUEUE) and not dormant.allows(cp.PAID_AI)


def test_a_user_pause_wins_even_over_recent_activity_and_payment():
    p = _Prof(last=NOW, paused=NOW, reason="got a job")
    st = cp.search_state(p, now=NOW, paid=lambda _p: True)
    assert st.state == cp.PAUSED and "got a job" in st.reason
    assert not st.allows(cp.QUEUE)


def test_a_genuine_paid_search_keeps_running_while_away():
    p = _Prof(last=NOW - timedelta(days=10))
    assert cp.search_state(p, now=NOW, paid=lambda _p: True).state == cp.PAID


def test_a_paid_lookup_failure_keeps_a_possibly_paid_search_running():
    def boom(_p):
        raise RuntimeError("db down")
    assert cp.search_state(_Prof(last=None), now=NOW, paid=boom).state == cp.PAID


def test_no_resume_is_setup():
    assert cp.search_state(_Prof(last=NOW), now=NOW, has_resume=False).state == cp.SETUP


# ── enforcement immediately before a paid provider call ─────────────────────

def _save(uid, **kw):
    with get_session() as s:
        s.add(UserProfile(user_id=uid, **kw))
        s.commit()


def test_paid_ai_allowed_reads_the_profile_and_caches(monkeypatch):
    import app.api.server as srv
    monkeypatch.setattr(srv, "_user_paid_search_is_live", lambda p: False)
    _save(_P + "gone", last_meaningful_activity_at=datetime.utcnow() - timedelta(days=4))
    _save(_P + "here", last_meaningful_activity_at=datetime.utcnow())
    assert cp.paid_ai_allowed(_P + "here") is True
    assert cp.paid_ai_allowed(_P + "gone") is False
    # A meaningful action (forget) is seen immediately, not after the cache TTL.
    with get_session() as s:
        row = s.exec(select(UserProfile).where(UserProfile.user_id == _P + "gone")).first()
        row.last_meaningful_activity_at = datetime.utcnow()
        s.add(row)
        s.commit()
    assert cp.paid_ai_allowed(_P + "gone") is False, "cached"
    cp.forget(_P + "gone")
    assert cp.paid_ai_allowed(_P + "gone") is True


def test_the_reranker_refuses_before_any_provider_call(monkeypatch):
    """A job queued while the user was active is not paid for after they left."""
    from app.matching import reranker as rr
    from app.db.models import Job, JobSource
    monkeypatch.setattr(cp, "paid_ai_allowed", lambda uid, now=None: False)

    calls = []

    class R(rr.Reranker):
        def __init__(self):
            self._user_id = _P + "gone"
            self._profile = None
            self._feedback = ""
            self._anthropic_client = object()
            self._openai_client = None
            self._active_backend = "anthropic"

        def _score_backends(self, provider=None):
            return [("anthropic", lambda *a: calls.append(a) or None)]

        def _prescore_backends(self):
            return [("openai", lambda *a: calls.append(a) or None)]

    job = Job(id=1, title="Software Engineer", company="Co", location="Remote", remote=True,
              description="Build Python services for our platform team. " * 3,
              source=JobSource.GREENHOUSE, external_id="x", url="u")
    monkeypatch.setattr(rr, "provider_available", lambda n: True)
    with pytest.raises(RuntimeError, match="search paused"):
        R().score_with_meta("resume", job)
    assert R().prescore("resume", job) is None
    assert calls == [], "no provider was called"


# ── atomic reservations under concurrent workers ─────────────────────────────

def test_concurrent_workers_cannot_exceed_the_per_user_ceiling(monkeypatch):
    from app.config import settings
    uid = _P + "race"
    daily_counter.reset(f"paid_calls:user:{uid}")
    daily_counter.reset("paid_calls:platform")
    monkeypatch.setattr(settings, "user_daily_paid_call_cap", 5, raising=False)
    monkeypatch.setattr(settings, "platform_daily_paid_call_cap", 0, raising=False)
    got = []
    lock = threading.Lock()

    def worker():
        ok = cp.reserve_paid_call(uid)
        with lock:
            got.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(True) == 5, got
    assert daily_counter.count(f"paid_calls:user:{uid}") == 5
    daily_counter.reset(f"paid_calls:user:{uid}")
    daily_counter.reset("paid_calls:platform")


def test_the_platform_ceiling_binds_every_user(monkeypatch):
    from app.config import settings
    daily_counter.reset("paid_calls:platform")
    monkeypatch.setattr(settings, "platform_daily_paid_call_cap", 2, raising=False)
    monkeypatch.setattr(settings, "user_daily_paid_call_cap", 0, raising=False)
    results = [cp.reserve_paid_call(f"{_P}u{i}") for i in range(4)]
    assert results == [True, True, False, False]
    daily_counter.reset("paid_calls:platform")
    for i in range(4):
        daily_counter.reset(f"paid_calls:user:{_P}u{i}")


# ── routes ───────────────────────────────────────────────────────────────────

def test_pause_and_resume_routes():
    """Local single-user mode: the caller is 'local' (NULL-owner profile)."""
    from fastapi.testclient import TestClient
    from app.api.server import app
    with get_session() as s:
        had = s.exec(select(UserProfile).where(UserProfile.user_id.is_(None))).first()
        if had is None:
            s.add(UserProfile(user_id=None))
            s.commit()
    c = TestClient(app)
    r = c.post("/api/search/pause", json={"reason": "on vacation"}).json()
    assert r["state"] == "paused" and r["automatic_scoring"] is False
    assert c.get("/api/search/state").json()["state"] == "paused"
    r = c.post("/api/search/resume").json()
    assert r["state"] in ("active", "paid") and r["automatic_scoring"] is True
    assert r["idle_after_hours"] == 24 and r["pause_after_hours"] == 72


def test_pause_accepts_an_empty_request():
    """The body is optional: a client that POSTs nothing (mobile, curl, an
    older dashboard) must pause, not get a 422 while the notice says running."""
    from fastapi.testclient import TestClient
    from app.api.server import app
    c = TestClient(app)
    r = c.post("/api/search/pause")
    assert r.status_code == 200 and r.json()["state"] == "paused"
    assert c.post("/api/search/resume").json()["state"] in ("active", "paid")
