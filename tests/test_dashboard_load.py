"""Sign-in must not wait on a user's whole history (2026-09-28).

Production (counts only): /dashboard took 17-32 s for two sign-ins. Every load
also fetched /api/stats — ~30 uncached, unbounded COUNTs over the user's whole
pool, 87,624 rows for one account (68,450 of them open) in a 7.9 GB table — and
those sat 8-31 s each waiting on disk while the dashboard's own reads timed out
behind them; a phone reloaded six times, starting the whole set each time. The
per-user close ran at 45 days while every surface works on the last 5.

Synthetic rows only.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete

from app.api import server
from app.common import ttl_cache
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource

_U = "dl-user"


@pytest.fixture(autouse=True)
def _clean_cache():
    ttl_cache.invalidate("stats:")
    server._STATS_LAST_GOOD.clear()
    yield
    ttl_cache.invalidate("stats:")
    server._STATS_LAST_GOOD.clear()


def _as(monkeypatch, uid):
    monkeypatch.setattr(server, "_get_user_id", lambda request: uid)
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True))


# ── /api/stats: one computation per user, cached ─────────────────────────────

def test_stats_are_cached_per_user(monkeypatch):
    _as(monkeypatch, _U)
    calls = []
    monkeypatch.setattr(server, "_compute_stats",
                        lambda uid: calls.append(uid) or {"total_jobs": 3, "degraded": False})
    assert server.api_stats(request=None)["total_jobs"] == 3
    assert server.api_stats(request=None)["total_jobs"] == 3
    assert calls == [_U]
    # Another user is another key.
    _as(monkeypatch, "dl-other")
    server.api_stats(request=None)
    assert calls == [_U, "dl-other"]


def test_six_reloads_at_once_compute_once(monkeypatch):
    """The phone that reloaded six times started six full sets of counts."""
    _as(monkeypatch, _U)
    calls = []

    def _slow(uid):
        calls.append(uid)
        time.sleep(0.3)
        return {"total_jobs": 1, "degraded": False}
    monkeypatch.setattr(server, "_compute_stats", _slow)
    threads = [threading.Thread(target=server.api_stats, kwargs={"request": None}) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [_U]


def test_a_degraded_answer_is_never_cached_and_falls_back_to_the_last_good(monkeypatch):
    _as(monkeypatch, _U)
    answers = [{"total_jobs": 40, "degraded": False},
               {"total_jobs": None, "degraded": True}]
    monkeypatch.setattr(server, "_compute_stats", lambda uid: answers.pop(0))
    assert server.api_stats(request=None)["total_jobs"] == 40
    ttl_cache.invalidate("stats:")                       # the 5 minutes passed
    d = server.api_stats(request=None)
    assert d["total_jobs"] == 40 and d["stale"] is True and d["degraded"] is True
    assert ttl_cache.get(f"stats:{_U}") is None, "a degraded answer must not be cached"


def test_an_unanswered_count_is_none_never_zero(monkeypatch):
    """An empty pool and an unanswered question are different things: 0 would
    flip the onboarding banner to 'you have nothing yet'."""
    _as(monkeypatch, _U)

    class _Timeout:
        def __init__(self, session, seconds):
            self.degraded = False

        def get(self, default, fn):
            self.degraded = True
            return default
    monkeypatch.setattr(server, "_BoundedReads", _Timeout)
    d = server.api_stats(request=None)
    assert d["degraded"] is True
    assert d["total_jobs"] is None and d["funnel"]["shortlisted"] is None
    assert d["scores"]["band_85_100"] is None


def test_an_action_refreshes_the_counts(monkeypatch):
    ttl_cache.put(f"stats:{_U}", {"total_jobs": 1}, 300)
    from types import SimpleNamespace
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True))
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "get_user_id_from_token", lambda tok: _U)
    monkeypatch.setattr(server, "get_user_id_from_token", lambda tok: _U, raising=False)
    monkeypatch.setattr(server, "_touch_last_active", lambda *a, **k: None)
    req = SimpleNamespace(method="POST", url=SimpleNamespace(path="/api/jobs/1/view"),
                          headers={"Authorization": "Bearer t"}, cookies={})
    assert server._get_user_id(req) == _U
    assert ttl_cache.get(f"stats:{_U}") is None
    # A poll does not.
    ttl_cache.put(f"stats:{_U}", {"total_jobs": 1}, 300)
    req.method, req.url.path = "GET", "/api/pipeline/live"
    server._get_user_id(req)
    assert ttl_cache.get(f"stats:{_U}") is not None


# ── the counts describe the CURRENT pool, not all history ────────────────────

@pytest.fixture
def pool():
    def _wipe():
        with get_session() as s:
            s.exec(delete(Application).where(Application.user_id == _U))
            s.exec(delete(Job).where(Job.user_id == _U))
            s.commit()
    _wipe()
    now = datetime.utcnow()
    with get_session() as s:
        for i, (score, closed) in enumerate([(90, False), (70, False), (50, False),
                                             (90, True), (90, True), (None, False)]):
            s.add(Job(user_id=_U, source=JobSource.GREENHOUSE, external_id=f"dl-{i}",
                      company=f"Co{i % 2}", title="Backend Engineer", url=f"https://x/dl{i}",
                      description="d", rerank_score=score, scored_at=now if score else None,
                      is_closed=closed, first_seen=now - timedelta(days=1),
                      discovered_at=now - timedelta(days=1)))
        s.commit()
    yield
    _wipe()


def test_the_score_bands_count_open_jobs_only(pool, monkeypatch):
    _as(monkeypatch, _U)
    d = server.api_stats(request=None)
    assert d["degraded"] is False
    assert d["total_jobs"] == 4
    # Both closed rows are a day old: inside the Ghost tab's window. There is
    # no all-history closed count (it was the statement that starved the rest).
    assert d["closed_jobs_recent"] == 2 and "closed_jobs" not in d
    assert d["scores"]["band_85_100"] == 1, "two closed 90s are history, not the pool"
    assert d["scores"]["band_60_84"] == 1 and d["scores"]["band_40_59"] == 1
    assert d["scores"]["unranked"] == 1 == d["funnel"]["pending_scoring"]
    assert sum(d["sources"].values()) == 4


def test_a_slow_closed_count_never_costs_the_insights_numbers(pool, monkeypatch):
    """2026-10-09, the owner's account (73k closed rows): the closed counts ran
    second and hit the statement timeout, which skipped every count after them
    — Insights said "No jobs yet" and drew an empty chart for 473 jobs. The
    closed (history) count now runs LAST: when it times out, only it is
    missing."""
    _as(monkeypatch, _U)
    real = server._BoundedReads

    class _ClosedTimesOut(real):
        def get(self, default, fn):
            for cell in (fn.__closure__ or ()):
                stmt = cell.cell_contents
                if hasattr(stmt, "compile") and "job.is_closed = true" in str(stmt):
                    self.degraded = True              # the closed count timed out
                    return default
            return super().get(default, fn)
    monkeypatch.setattr(server, "_BoundedReads", _ClosedTimesOut)
    d = server.api_stats(request=None)
    assert d["closed_jobs_recent"] is None and d["degraded"] is True
    assert d["total_jobs"] == 4
    assert d["scores"]["band_85_100"] == 1 and d["scores"]["band_40_59"] == 1
    assert sum(d["sources"].values()) == 4, "the source breakdown must still arrive"


# ── retention: untouched copies close after a week ───────────────────────────

def test_the_window_is_a_week_and_outlives_every_surface():
    assert settings.user_job_close_age_days == 7
    for window in (settings.shortlist_max_age_days, settings.explorer_max_age_days,
                   settings.scoring_max_job_age_days):
        assert settings.user_job_close_age_days > window


_R = "dl-retention"


def test_a_week_old_untouched_copy_closes_and_everything_else_stays():
    from app.discovery.pipeline import SHARED_POOL_USER
    from app.strategy.job_retention import close_stale_user_jobs
    now = datetime.utcnow()
    with get_session() as s:
        s.exec(delete(Application).where(Application.user_id == _R))
        s.exec(delete(Job).where(Job.external_id.like("dlr-%")))
        rows = {}
        for key, owner, age in (("old", _R, 8), ("old_applied", _R, 8), ("fresh", _R, 3),
                                ("shared_old", SHARED_POOL_USER, 8)):
            j = Job(user_id=owner, source=JobSource.GREENHOUSE, external_id=f"dlr-{key}",
                    company="Co", title="t", url=f"https://x/{key}", description="d",
                    first_seen=now - timedelta(days=age), discovered_at=now - timedelta(days=age))
            s.add(j)
            s.flush()
            rows[key] = j.id
        s.add(Application(user_id=_R, job_id=rows["old_applied"], status=ApplicationStatus.SUBMITTED))
        s.commit()
    close_stale_user_jobs(settings.user_job_close_age_days, pause_seconds=0)
    with get_session() as s:
        state = {k: s.get(Job, v).is_closed for k, v in rows.items()}
        s.exec(delete(Application).where(Application.user_id == _R))
        s.exec(delete(Job).where(Job.external_id.like("dlr-%")))
        s.commit()
    assert state == {"old": True, "old_applied": False, "fresh": False, "shared_old": False}
