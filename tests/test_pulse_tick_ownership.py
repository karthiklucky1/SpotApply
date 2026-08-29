"""Two pulse consumers must never run at once — by construction, not by luck.

THE BUG. ``run_pulse_tick`` held a "self-healing" lock: if the current holder
looked overdue, the next caller **proceeded without the lock**. The premise was
that an overdue holder is dead. It isn't — it is slow, which is the very
condition that produces the overrun, so the bypass fired exactly when a second
consumer was most damaging. Above it, the scheduler awaited
``asyncio.wait_for(asyncio.to_thread(run_pulse_tick), 300)``; a timeout there
abandons the AWAIT and leaves the thread running, because ``to_thread`` cannot
interrupt Python. The two together are a lock-steal generator: worker thread
still consuming, scheduler starts a second tick, lock looks stale, second
consumer proceeds. It fired once on the P2 deployment, and historically produced
overlapping consumers and registry deadlocks — two threads writing the same
CompanyRegistry rows in whatever order their board lists happened to take.

The five properties these tests pin, in the order the failure unfolds:

  1. a busy lock is never stolen, however old the holder looks;
  2. a scheduler timeout cannot create a second consumer;
  3. the original worker finishing later cannot corrupt ownership;
  4. work the consumer did not reach is DEFERRED, not dropped;
  5. no board is ever both completed and deferred (and none is neither).
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.api import server
from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource
from app.strategy import pulse_lane

_PREFIX = "own-"

# Captured BEFORE any test patches asyncio.sleep. The scheduler harness patches
# it to count the loop's pacing decisions, so a test that used the patched one
# to yield would count its own yields and then be cancelled by them.
_REAL_SLEEP = asyncio.sleep


def _board(slug: str, **kw) -> int:
    with get_session() as session:
        row = CompanyRegistry(
            slug=_PREFIX + slug, ats=JobSource.GREENHOUSE,
            company_name=_PREFIX + slug, is_active=True,
            job_count=kw.pop("job_count", 5), failure_count=0,
            poll_hash="sig-old", **kw)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _mine():
    with get_session() as session:
        return session.exec(select(CompanyRegistry).where(
            CompanyRegistry.slug.like(f"{_PREFIX}%"))).all()


def _cleanup():
    with get_session() as session:
        session.exec(delete(CompanyRegistry).where(
            CompanyRegistry.slug.like(f"{_PREFIX}%")))
        session.commit()


@pytest.fixture(autouse=True)
def _clean_lock():
    """Never leave the module lock held — a leaked lock would silently turn
    every later test in the process into a 'skipped' tick."""
    init_db()
    _cleanup()
    yield
    _cleanup()
    if pulse_lane._TICK_LOCK.locked():
        try:
            pulse_lane._TICK_LOCK.release()
        except RuntimeError:
            pass
    pulse_lane._TICK_STARTED[0] = 0.0


# ── 1. The lock is never stolen ──────────────────────────────────────────────

def test_a_busy_lock_is_never_stolen_however_old_the_holder(monkeypatch):
    """THE REGRESSION. The old code compared the holder's age to a grace window
    and proceeded anyway once it elapsed. There must be no such branch: an
    ancient holder is still a holder."""
    ran = []
    monkeypatch.setattr(pulse_lane, "_run_pulse_tick_locked",
                        lambda deadline: ran.append(1) or {"boards": 1})

    assert pulse_lane._TICK_LOCK.acquire(blocking=False)
    try:
        # Holder started an hour ago — far past any grace window the old code
        # would have honoured.
        pulse_lane._TICK_STARTED[0] = time.monotonic() - 3600.0
        stats = pulse_lane.run_pulse_tick()
    finally:
        pulse_lane._TICK_LOCK.release()

    assert ran == [], "a second consumer ran while the lock was held"
    assert stats.get("skipped") == "previous tick still running"
    assert stats["boards"] == 0
    assert stats["holder_age_s"] >= 3600.0, "holder age not reported for triage"


def test_the_skip_does_not_leave_the_lock_broken(monkeypatch):
    """A skipped call must not release a lock it never acquired."""
    monkeypatch.setattr(pulse_lane, "_run_pulse_tick_locked",
                        lambda deadline: {"boards": 1})
    assert pulse_lane._TICK_LOCK.acquire(blocking=False)
    try:
        pulse_lane.run_pulse_tick()
        assert pulse_lane._TICK_LOCK.locked(), "the skip released someone else's lock"
    finally:
        pulse_lane._TICK_LOCK.release()
    # ...and the lane still works afterwards.
    assert pulse_lane.run_pulse_tick() == {"boards": 1}


def test_concurrent_callers_never_both_enter_the_consumer(monkeypatch):
    """Hammer it: many threads, one consumer."""
    inside = []
    peak = [0]
    guard = threading.Lock()

    def _body(deadline):
        with guard:
            inside.append(1)
            peak[0] = max(peak[0], len(inside))
        time.sleep(0.02)
        with guard:
            inside.pop()
        return {"boards": 1}

    monkeypatch.setattr(pulse_lane, "_run_pulse_tick_locked", _body)
    threads = [threading.Thread(target=pulse_lane.run_pulse_tick) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1, f"{peak[0]} consumers ran concurrently"


def test_the_lock_is_released_even_when_the_consumer_raises(monkeypatch):
    def _boom(deadline):
        raise RuntimeError("tick blew up")

    monkeypatch.setattr(pulse_lane, "_run_pulse_tick_locked", _boom)
    with pytest.raises(RuntimeError):
        pulse_lane.run_pulse_tick()
    assert not pulse_lane._TICK_LOCK.locked(), "lock leaked on the error path"
    assert pulse_lane._TICK_STARTED[0] == 0.0, "holder clock not cleared"


# ── 2/3. The scheduler cannot outrun its own worker ──────────────────────────

class _SchedulerHarness:
    """Drives the real ``server._pulse_lane`` with a tick body we control.

    ``asyncio.wait`` is patched to report a timeout on the first cycle only —
    exactly what production sees when a tick blows the 300s budget — while the
    worker thread keeps running. Everything else is the real loop.
    """

    def __init__(self, monkeypatch, cycles=4):
        self.monkeypatch = monkeypatch
        self.cycles = cycles
        self.release = threading.Event()
        self.entered = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self._guard = threading.Lock()
        self.sleeps = []

    def tick(self):
        with self._guard:
            self.entered += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            # First tick blocks until the test releases it; later ones return.
            if self.entered == 1:
                self.release.wait(timeout=5)
            return {"boards": 1}
        finally:
            with self._guard:
                self.concurrent -= 1

    async def run(self):
        h = self
        real_sleep = asyncio.sleep
        real_wait = asyncio.wait
        state = {"forced_timeout": False}

        async def _sleep(d):
            h.sleeps.append(d)
            if len(h.sleeps) > h.cycles:
                raise asyncio.CancelledError()
            await real_sleep(0)

        async def _wait(fs, timeout=None, **kw):
            if not state["forced_timeout"]:
                state["forced_timeout"] = True
                # The production failure: the budget elapses, the await is
                # abandoned, the thread runs on. asyncio.wait returns
                # (done, pending) — it does NOT cancel.
                await real_sleep(0)
                return set(), set(fs)
            return await real_wait(fs, timeout=timeout, **kw)

        self.monkeypatch.setattr(server.settings, "direct_ats_enabled", True)
        self.monkeypatch.setattr(server.settings, "pulse_tick_seconds", 60)
        self.monkeypatch.setattr(asyncio, "sleep", _sleep)
        self.monkeypatch.setattr(asyncio, "wait", _wait)
        self.monkeypatch.setattr("app.strategy.pulse_lane.run_pulse_tick",
                                 self.tick, raising=False)
        with pytest.raises(asyncio.CancelledError):
            await server._pulse_lane()


def test_a_scheduler_timeout_cannot_start_a_second_consumer(monkeypatch):
    """THE OTHER HALF OF THE REGRESSION.

    The first tick's await times out while its thread is still inside the tick
    body. The loop must NOT call run_pulse_tick again until that Future is done.
    """
    h = _SchedulerHarness(monkeypatch, cycles=4)

    async def _go():
        task = asyncio.ensure_future(h.run())
        # Let the loop spin through several cycles while tick #1 is stuck.
        for _ in range(60):
            await _REAL_SLEEP(0)
        assert h.entered == 1, (
            f"the scheduler started {h.entered} tick bodies while the first "
            f"worker thread was still running")
        h.release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_go())
    assert h.max_concurrent == 1, (
        f"{h.max_concurrent} tick bodies ran concurrently")


def test_a_late_worker_does_not_corrupt_ownership(monkeypatch):
    """Once the abandoned Future finishes, the lane must resume — reaping the
    result rather than leaking the Future or double-counting the cycle."""
    # Generous cycle budget: this test is about what happens AFTER the late
    # worker returns, so the harness must not cancel itself before then.
    h = _SchedulerHarness(monkeypatch, cycles=400)

    async def _go():
        task = asyncio.ensure_future(h.run())
        for _ in range(20):
            await _REAL_SLEEP(0)
        assert h.entered == 1, "a second consumer started while tick #1 was stuck"
        h.release.set()                      # the late worker returns
        for _ in range(400):
            if h.entered >= 2:
                break
            await _REAL_SLEEP(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_go())
    assert h.entered >= 2, (
        "the lane never resumed after the late worker finished — the Future "
        "was leaked and the loop is now stuck forever")
    assert h.max_concurrent == 1


# ── 4/5. Unconsumed work is deferred, never dropped, never double-counted ────

def _stub_fetch_world(monkeypatch, n_boards):
    """A tick whose fetches all succeed instantly, over this file's boards."""
    ids = [_board(f"b{i}") for i in range(n_boards)]
    monkeypatch.setattr(pulse_lane, "_due_boards", lambda now, limit: _mine()[:limit])
    monkeypatch.setattr(pulse_lane, "_active_users", lambda: [], raising=False)

    class _Scraper:
        def fetch(self):
            return []

    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, url=None: _Scraper())
    return ids


def test_boards_past_the_consumer_deadline_are_deferred_not_polled(monkeypatch):
    """The consumer stops on the tick's wall clock. Everything it did not reach
    must come back through the deferral path with last_seen untouched."""
    ids = _stub_fetch_world(monkeypatch, 6)
    before = {r.id: (r.last_seen, r.poll_hash, r.job_count, r.failure_count)
              for r in _mine()}

    # Deadline already gone the moment the consumer loop starts.
    monkeypatch.setattr(settings, "pulse_tick_max_seconds", 0)
    stats = pulse_lane.run_pulse_tick()

    assert stats["selected"] == len(ids)
    assert stats["fetch_ok"] == 0, "a board was polled after the deadline"
    assert stats["deferred"] == len(ids), "unconsumed boards were dropped"
    for r in _mine():
        assert (r.last_seen, r.poll_hash, r.job_count, r.failure_count) == before[r.id], (
            f"board {r.slug} was written as polled although it was never consumed")
        assert r.next_poll_at is not None, "a deferred board got no retry time"


def test_every_selected_board_lands_in_exactly_one_bucket(monkeypatch):
    """The telemetry contract, under the deadline break specifically: a board is
    completed OR deferred — never both, and never neither."""
    for cap in (0, 150):
        _cleanup()
        ids = _stub_fetch_world(monkeypatch, 5)
        monkeypatch.setattr(settings, "pulse_tick_max_seconds", cap)
        stats = pulse_lane.run_pulse_tick()
        buckets = (stats["fetch_ok"] + stats["fetch_failed"]
                   + stats["unsupported"] + stats["deferred"])
        assert buckets == stats["selected"] == len(ids), (
            f"cap={cap}: buckets {buckets} != selected {stats['selected']} — "
            f"a board was double-counted or lost: {stats}")
        split = (stats["deferred_cancelled"] + stats["deferred_running"]
                 + stats["deferred_unconsumed"])
        assert split == stats["deferred"], (
            f"cap={cap}: deferral split {split} != deferred {stats['deferred']}")


def test_the_consumer_stops_mid_loop_and_defers_the_remainder(monkeypatch):
    """THE PRODUCTION SHAPE, and the one the other cases cannot reach.

    ``as_completed``'s timeout only bounds WAITING. When every fetch is already
    done it hands them back with no wait at all, so the old consumer could run
    arbitrarily far past the tick's wall clock — which is how a 150s tick
    reached the scheduler's 300s budget. Reproduced by making the per-board work
    slow enough that the deadline lands PART WAY through a batch whose futures
    have all completed: some boards must be polled, the rest deferred, and the
    two sets must not overlap.
    """
    _stub_fetch_world(monkeypatch, 8)
    ids_before = {r.id: r.last_seen for r in _mine()}

    real_sig = pulse_lane._board_signature

    def _slow_sig(raw):
        time.sleep(0.4)          # ~0.4s of consumer work per board
        return real_sig(raw)

    monkeypatch.setattr(pulse_lane, "_board_signature", _slow_sig)
    monkeypatch.setattr(settings, "pulse_tick_max_seconds", 1)

    stats = pulse_lane.run_pulse_tick()

    assert stats["consumer_deadline_hit"] == 1, (
        f"the consumer ran the whole batch past its 1s deadline: {stats}")
    assert 0 < stats["fetch_ok"] < 8, (
        f"expected a PARTIAL consume, got fetch_ok={stats['fetch_ok']}")
    assert stats["deferred"] == 8 - stats["fetch_ok"], "boards vanished"

    # The two sets are disjoint: a board that was polled moved last_seen, a
    # board that was deferred did not. Nothing may appear in both.
    polled = [r for r in _mine() if r.last_seen != ids_before[r.id]]
    unpolled = [r for r in _mine() if r.last_seen == ids_before[r.id]]
    assert len(polled) == stats["fetch_ok"]
    assert len(unpolled) == stats["deferred"]
    for r in unpolled:
        assert r.next_poll_at is not None, "a deferred board got no retry time"
        assert r.poll_hash == "sig-old", "a deferred board's poll_hash moved"


def test_a_tick_with_time_to_spare_reports_no_deadline_stop(monkeypatch):
    _stub_fetch_world(monkeypatch, 4)
    monkeypatch.setattr(settings, "pulse_tick_max_seconds", 150)
    assert pulse_lane.run_pulse_tick()["consumer_deadline_hit"] == 0, (
        "a tick that finished its work reported a deadline stop")


def test_deferral_retry_and_jitter_semantics_are_preserved(monkeypatch):
    """The deadline path must reuse the EXISTING deferral semantics: a short
    retry plus the id-derived (stable, not random) offset."""
    _stub_fetch_world(monkeypatch, 4)
    base = max(1, int(settings.pulse_deferred_retry_minutes or 1))
    jitter = max(0, int(settings.pulse_deferred_retry_jitter_seconds or 0))
    monkeypatch.setattr(settings, "pulse_tick_max_seconds", 0)

    t0 = datetime.utcnow()
    pulse_lane.run_pulse_tick()
    for r in _mine():
        offset = (r.id % (jitter + 1)) if jitter else 0
        expected = t0 + timedelta(minutes=base, seconds=offset)
        assert abs((r.next_poll_at - expected).total_seconds()) < 5, (
            f"board {r.slug} retry time drifted from the deferral contract")
