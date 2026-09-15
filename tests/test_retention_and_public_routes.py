"""Nightly retention and the public freshness route.

Both were failing every day in production and neither failure was visible as
anything worse than a WARNING:

  * the shared-pool close was ONE unbounded UPDATE and hit Supabase's statement
    timeout every night from 2026-09-05 to 09-11, so shared rows never closed
    and therefore never purged;
  * all four retention passes ran on the asyncio event loop, so multi-minute
    blocking DB work shared a thread with every HTTP request;
  * /api/public/freshness runs a full-table aggregate with no usable index, and
    its cache was written only on SUCCESS — so after the 09-09 01:33 timeout
    every landing-page hit re-ran the query that had just been cancelled.

Rows are prefixed ``rt_``.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Job, JobSource
from app.discovery.pipeline import SHARED_POOL_USER
from app.strategy.job_retention import close_stale_shared_jobs

_P = "rt_"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
            s.commit()
    _wipe()
    yield
    _wipe()


def _shared(ext: str, days_old: int, closed: bool = False) -> Job:
    now = datetime.utcnow()
    return Job(user_id=SHARED_POOL_USER, source=JobSource.GREENHOUSE,
               external_id=_P + ext, company="Co", title="ML Engineer",
               location="Remote", remote=True, url=f"https://x/{ext}",
               description="d", first_seen=now - timedelta(days=days_old),
               discovered_at=now - timedelta(days=days_old), is_closed=closed)


# ── The shared-pool close ────────────────────────────────────────────────────

def test_shared_pool_close_runs_in_bounded_batches():
    """Batching is the whole fix: the unbatched version could not finish."""
    with get_session() as s:
        for i in range(7):
            s.add(_shared(f"old{i}", days_old=60))
        s.commit()

    # A batch size smaller than the backlog forces several statements.
    closed = close_stale_shared_jobs(days=45, batch=2, max_batches=10)
    assert closed == 7

    with get_session() as s:
        rows = s.exec(select(Job).where(Job.external_id.like(f"{_P}%"))).all()
    assert all(j.is_closed for j in rows)
    assert all("shared-pool retention" in (j.closed_reason or "") for j in rows)


def test_shared_pool_close_leaves_fresh_rows_alone():
    with get_session() as s:
        s.add(_shared("fresh", days_old=3))
        s.add(_shared("stale", days_old=90))
        s.commit()
    close_stale_shared_jobs(days=45, batch=100)
    with get_session() as s:
        fresh = s.exec(select(Job).where(Job.external_id == _P + "fresh")).first()
        stale = s.exec(select(Job).where(Job.external_id == _P + "stale")).first()
    assert not fresh.is_closed
    assert stale.is_closed


def test_shared_pool_close_stops_when_the_batch_budget_runs_out():
    """A bounded pass must never loop forever on a huge backlog."""
    with get_session() as s:
        for i in range(6):
            s.add(_shared(f"b{i}", days_old=60))
        s.commit()
    closed = close_stale_shared_jobs(days=45, batch=2, max_batches=2)
    assert closed == 4, "max_batches did not bound the pass"


def test_a_disabled_window_closes_nothing():
    with get_session() as s:
        s.add(_shared("keep", days_old=999))
        s.commit()
    assert close_stale_shared_jobs(days=0) == 0


# ── Off the event loop ───────────────────────────────────────────────────────

def test_retention_does_not_run_on_the_event_loop():
    """Every retention pass must be handed to a thread.

    Asserted on the source because the failure mode is a latency one: the code
    is correct, it just runs in the wrong thread, and no unit test of the
    functions themselves can see that.
    """
    from app.api import server
    src = inspect.getsource(server._registry_maintenance_once)
    assert "asyncio.to_thread" in src
    for fn in ("close_stale_shared_jobs", "close_stale_user_jobs",
               "strip_dead_descriptions", "purge_old_closed_jobs"):
        assert fn in src, f"{fn} is no longer part of the maintenance cycle"
    # The old inline UPDATE is gone: it is the thing that timed out nightly.
    assert "shared-pool retention (45d)" not in src, (
        "the unbatched shared-pool UPDATE is back in the maintenance job")


# ── The public route ─────────────────────────────────────────────────────────

def test_public_freshness_caches_its_own_failure(monkeypatch):
    from app.api import server

    calls = {"n": 0}

    def _boom(now, now_ts):
        calls["n"] += 1
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(server, "_public_freshness_uncached", _boom)
    server.__dict__.pop("_PUBLIC_FRESHNESS_CACHE", None)
    server.__dict__.pop("_PUBLIC_FRESHNESS_LAST", None)

    first = server.public_freshness()
    second = server.public_freshness()

    assert calls["n"] == 1, (
        "a failed aggregate was not cached, so every landing-page hit re-runs "
        "the query the database just cancelled")
    assert first == second
    assert "active_boards" in first


def test_public_freshness_serves_the_last_good_numbers_after_a_failure(monkeypatch):
    from app.api import server

    good = {"active_boards": 123, "jobs_tracked_7d": 456,
            "median_detection_latency_hours": 1.0, "detected_within_24h_pct": 90,
            "fresh_alerts_7d": 3, "median_post_to_alert_min": 12.0}
    monkeypatch.setattr(server, "_public_freshness_uncached", lambda now, ts: good)
    server.__dict__.pop("_PUBLIC_FRESHNESS_CACHE", None)
    server.__dict__.pop("_PUBLIC_FRESHNESS_LAST", None)
    assert server.public_freshness() == good

    def _boom(now, now_ts):
        raise RuntimeError("timeout")

    monkeypatch.setattr(server, "_public_freshness_uncached", _boom)
    server.__dict__.pop("_PUBLIC_FRESHNESS_CACHE", None)
    assert server.public_freshness() == good, "a failure should not blank the page"
