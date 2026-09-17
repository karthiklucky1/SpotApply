"""The board's per-user aggregates are bounded, cached and honest about it.

Post-deploy HTTP sample, Railway, 2026-09-16 19:01-19:28 (n=115):
/api/freshness-stats 52 s, /api/jobs 36 s, /dashboard 15 s, the once-a-minute
/api/pipeline/live p50 4 s / p95 9 s / max 10 s, and 16x "dashboard read
exceeded its budget — panel degraded (QueryCanceled)". Every one recomputed
COUNT/median aggregates over a 65k-row per-user pool on each request.

Pinned here:
  * /api/freshness-stats keeps its response shape, runs its statements through
    the dashboard budget, and serves the second call from the TTL cache;
  * /api/pipeline/live caches the pool tile per user and never caches a
    timed-out None;
  * /api/jobs returns the page even when its COUNT exceeded the budget —
    total/pages are null and `degraded` is true, never a silent 0;
  * the two funnel events the audit found missing are written, once.

Rows this file writes carry the `aggtest-` prefix and are removed by it.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete, select

from app.api import server
from app.common import ttl_cache
from app.db.init_db import get_session
from app.db.models import FunnelEvent, Job, JobSource, UserProfile

_P = "aggtest-"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_get_user_id", lambda request: "local")
    ttl_cache.invalidate()
    yield TestClient(server.app)
    ttl_cache.invalidate()


@pytest.fixture(autouse=True)
def _clean_rows():
    def _wipe():
        with get_session() as s:
            s.exec(delete(FunnelEvent).where(FunnelEvent.reason.like(f"{_P}%")))
            s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
            for j in s.exec(select(Job).where(Job.external_id.like(f"{_P}%"))).all():
                s.delete(j)
            s.commit()
    _wipe()
    yield
    _wipe()


_FRESHNESS_KEYS = {
    "scored_feed_jobs", "median_feed_age_hours", "median_detection_latency_hours",
    "detected_within_24h_pct", "fresh_alerts_7d", "median_post_to_alert_min",
    "hot_lane_last_run", "hot_lane_runs_24h", "hot_lane_jobs_24h",
    "shared_pool_new_24h", "last_discovery_run", "pulse", "degraded",
}


# ── freshness-stats ──────────────────────────────────────────────────────────

def test_freshness_stats_keeps_its_shape_and_is_served_from_cache_once_computed(client, monkeypatch):
    with get_session() as s:
        s.add(Job(source=JobSource.REMOTEOK, external_id=f"{_P}fresh-1", company="AggCo",
                  title="Backend Engineer", url="http://a", description="x" * 50,
                  rerank_score=88.0, posted_at=datetime.utcnow(),
                  first_seen=datetime.utcnow(), discovered_at=datetime.utcnow()))
        s.commit()
    real = server._compute_freshness_stats
    calls = {"n": 0}

    def counted(user_id_arg):
        calls["n"] += 1
        return real(user_id_arg)

    monkeypatch.setattr(server, "_compute_freshness_stats", counted)
    r1 = client.get("/api/freshness-stats")
    assert r1.status_code == 200
    body = r1.json()
    assert _FRESHNESS_KEYS <= set(body), _FRESHNESS_KEYS - set(body)
    assert body["degraded"] is False
    assert isinstance(body["scored_feed_jobs"], int) and body["scored_feed_jobs"] >= 1
    r2 = client.get("/api/freshness-stats")
    assert r2.status_code == 200
    assert calls["n"] == 1, "the second poll must be served from the TTL cache"


def test_a_degraded_freshness_payload_is_not_pinned_for_five_minutes(client, monkeypatch):
    class _Degraded:
        def __init__(self, session, seconds):
            self.degraded = True

        def get(self, default, fn):
            return default

    monkeypatch.setattr(server, "_BoundedReads", _Degraded)
    body = client.get("/api/freshness-stats").json()
    assert body["degraded"] is True
    assert body["scored_feed_jobs"] is None, "a timed-out count is None, never 0"
    hit = ttl_cache.peek("freshness_stats:local")
    assert hit is not ttl_cache._MISS
    # Degraded entries carry the SHORT ttl: expiring within a minute, not five.
    with ttl_cache._lock:
        expires_at, _ = ttl_cache._store["freshness_stats:local"]
    import time as _t
    assert expires_at - _t.monotonic() <= server._FRESHNESS_DEGRADED_TTL_SECONDS + 1


# ── pipeline/live ────────────────────────────────────────────────────────────

def test_the_pool_tile_is_cached_per_user_and_a_timeout_is_not(client, monkeypatch):
    first = client.get("/api/pipeline/live").json()
    assert first["counts"]["pool"] is not None
    assert ttl_cache.peek("pool_count:local") is not ttl_cache._MISS

    ttl_cache.invalidate()

    class _Degraded(server._BoundedReads):
        def get(self, default, fn):
            self.degraded = True
            return default

    monkeypatch.setattr(server, "_BoundedReads", _Degraded)
    body = client.get("/api/pipeline/live").json()
    assert body["counts"]["pool"] is None and body["degraded"] is True
    assert ttl_cache.peek("pool_count:local") is ttl_cache._MISS, \
        "a timed-out pool count must not be pinned"


# ── /api/jobs ────────────────────────────────────────────────────────────────

def test_jobs_returns_the_page_with_null_counts_when_the_count_times_out(client, monkeypatch):
    with get_session() as s:
        s.add(Job(source=JobSource.REMOTEOK, external_id=f"{_P}jobs-1", company="AggCo",
                  title="Data Engineer", url="http://b", description="y" * 50,
                  rerank_score=75.0, first_seen=datetime.utcnow(),
                  discovered_at=datetime.utcnow()))
        s.commit()

    class _Degraded(server._BoundedReads):
        def get(self, default, fn):
            self.degraded = True
            return default

    monkeypatch.setattr(server, "_BoundedReads", _Degraded)
    r = client.get("/api/jobs?max_age_days=0&limit=50")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] is None and body["pages"] is None and body["degraded"] is True
    assert any(j.get("title") == "Data Engineer" for j in body["jobs"]), \
        "the page itself must still arrive when only the COUNT timed out"
    assert ttl_cache.size() == 0 or all(
        not k.startswith("jobs_total:") for k in list(ttl_cache._store)), \
        "a None total must never be cached"


def test_jobs_counts_are_cached_for_the_same_filter_signature(client):
    r1 = client.get("/api/jobs?max_age_days=0&limit=50").json()
    assert r1["degraded"] is False and isinstance(r1["total"], int)
    keys = [k for k in list(ttl_cache._store) if k.startswith("jobs_total:")]
    assert len(keys) == 1
    r2 = client.get("/api/jobs?max_age_days=0&limit=50").json()
    assert r2["total"] == r1["total"]
    assert len([k for k in list(ttl_cache._store) if k.startswith("jobs_total:")]) == 1


def test_the_filter_signature_folds_search_case_but_not_company():
    a = server._jobs_count_key("u", False, "Backend", None, None, None, None, None, None, None, None, 7)
    b = server._jobs_count_key("u", False, "backend", None, None, None, None, None, None, None, None, 7)
    assert a == b
    c = server._jobs_count_key("u", False, None, "Acme", None, None, None, None, None, None, None, 7)
    d = server._jobs_count_key("u", False, None, "acme", None, None, None, None, None, None, None, 7)
    assert c != d
    assert server._jobs_count_key("other", False, "backend", None, None, None, None, None, None, None, None, 7) != a


# ── funnel events ────────────────────────────────────────────────────────────

def _events(stage: str, reason: str) -> list:
    with get_session() as s:
        return s.exec(select(FunnelEvent).where(
            FunnelEvent.stage == stage, FunnelEvent.reason == reason)).all()


def test_profile_completed_is_written_once_when_roles_and_skills_exist():
    uid = f"{_P}u1"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="ML Engineer", key_skills="python, pytorch"))
        s.commit()
    assert server._record_profile_completed_once(uid) is True
    assert server._record_profile_completed_once(uid) is False, "once per user, ever"
    evs = _events("profile_completed", uid)
    assert len(evs) == 1
    assert '"has_skills": true' in (evs[0].metadata_json or "")


def test_profile_completed_waits_for_target_roles():
    uid = f"{_P}u2"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="", key_skills="python"))
        s.commit()
    assert server._record_profile_completed_once(uid) is False
    assert _events("profile_completed", uid) == []


def test_profile_completed_accepts_a_resume_in_place_of_skills(monkeypatch):
    uid = f"{_P}u3"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="Data Engineer", key_skills=""))
        s.commit()
    monkeypatch.setattr(server, "_user_has_resume", lambda u: u == uid)
    assert server._record_profile_completed_once(uid) is True
    evs = _events("profile_completed", uid)
    assert len(evs) == 1 and '"has_resume": true' in (evs[0].metadata_json or "")


def test_document_downloaded_records_kind_and_application():
    uid = f"{_P}dl"
    server._record_document_downloaded("resume", 424242, uid)
    evs = _events("document_downloaded", uid)
    assert len(evs) == 1
    assert '"kind": "resume"' in evs[0].metadata_json
    assert '"application_id": 424242' in evs[0].metadata_json
