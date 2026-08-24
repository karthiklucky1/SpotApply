"""The pulse loop must be FIXED-RATE, not fixed-delay.

It slept a full interval AFTER the tick body, so the real period was
``tick_duration + interval``. With a saturated ~197s body and a 60s setting that
is a 257s period — 14 ticks/hour where the config says 60, and roughly a third
of the lane's capacity given away to a sleep that was never meant to be
additive. Production's revisit interval carried that loss directly.

These tests drive the real ``_pulse_lane`` coroutine with the clock and the tick
body stubbed, so they assert the SCHEDULING DECISION (how long it chose to
sleep) without sleeping through a real interval.
"""
from __future__ import annotations

import asyncio

import pytest

from app.api import server


class _Harness:
    """Runs the real loop with a fake monotonic clock and a scripted tick body.

    ``durations`` is how long each successive tick "takes" on the fake clock;
    ``sleeps`` records what the loop asked to sleep after each one.
    """

    def __init__(self, monkeypatch, durations, interval=60, stop_after=None):
        self.monkeypatch = monkeypatch
        self.durations = list(durations)
        self.interval = interval
        self.stop_after = stop_after or len(self.durations)
        self.sleeps: list[float] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.ticks = 0
        self._clock = 1000.0

    async def run(self):
        h = self

        def _now():
            return h._clock

        def _tick():
            # Detect overlap: if the loop ever started a second body before the
            # first returned, this would exceed 1.
            h.concurrent += 1
            h.max_concurrent = max(h.max_concurrent, h.concurrent)
            try:
                dur = h.durations[h.ticks] if h.ticks < len(h.durations) else 0.0
                h.ticks += 1
                if isinstance(dur, Exception):
                    h._clock += 1.0
                    raise dur
                h._clock += dur
                return {"boards": 1}
            finally:
                h.concurrent -= 1

        # Capture the REAL sleep before patching — calling the patched one from
        # inside the fake would recurse and record a spurious 0.
        _real_sleep = asyncio.sleep

        async def _sleep(d):
            h.sleeps.append(d)
            h._clock += d
            if len(h.sleeps) >= h.stop_after:
                raise asyncio.CancelledError()
            await _real_sleep(0)

        async def _to_thread(fn, *a, **kw):
            return fn(*a, **kw)

        async def _wait_for(coro, timeout=None):
            return await coro

        self.monkeypatch.setattr(server, "_loop_now", _now)
        self.monkeypatch.setattr(server.settings, "pulse_tick_seconds", self.interval)
        self.monkeypatch.setattr(server.settings, "direct_ats_enabled", True)
        self.monkeypatch.setattr(asyncio, "sleep", _sleep)
        self.monkeypatch.setattr(asyncio, "to_thread", _to_thread)
        self.monkeypatch.setattr(asyncio, "wait_for", _wait_for)
        self.monkeypatch.setattr(
            "app.strategy.pulse_lane.run_pulse_tick", _tick, raising=False)

        with pytest.raises(asyncio.CancelledError):
            await server._pulse_lane()
        # Drop the boot settle sleep so `sleeps` is only the pacing decisions.
        return self.sleeps[1:] if self.sleeps else []


def _run(monkeypatch, durations, interval=60, stop_after=None):
    h = _Harness(monkeypatch, durations, interval, stop_after)
    sleeps = asyncio.run(h.run())
    return sleeps, h


# ── short tick: sleeps the remainder ─────────────────────────────────────────

def test_a_short_tick_sleeps_only_the_remainder(monkeypatch):
    sleeps, _ = _run(monkeypatch, [20.0], interval=60, stop_after=2)
    assert sleeps[0] == pytest.approx(40.0), (
        f"a 20s tick on a 60s interval should sleep ~40s, slept {sleeps[0]}s — "
        f"a fixed-DELAY loop would sleep the full 60")


def test_a_tick_just_under_the_interval_sleeps_the_small_remainder(monkeypatch):
    sleeps, _ = _run(monkeypatch, [55.0], interval=60, stop_after=2)
    assert sleeps[0] == pytest.approx(5.0)


# ── tick == interval, and tick > interval: no additive sleep ─────────────────

def test_a_tick_equal_to_the_interval_does_not_add_another(monkeypatch):
    sleeps, _ = _run(monkeypatch, [60.0], interval=60, stop_after=2)
    assert sleeps[0] <= 1.0, (
        f"a 60s tick on a 60s interval slept {sleeps[0]}s on top — that is the "
        f"additive-sleep bug")


def test_an_overrunning_tick_starts_the_next_one_promptly(monkeypatch):
    """THE production case: a ~197s body on a 60s interval was costing a further
    60s every time."""
    sleeps, _ = _run(monkeypatch, [197.0], interval=60, stop_after=2)
    assert sleeps[0] <= 1.0, (
        f"a 197s tick still slept {sleeps[0]}s — the period is back to "
        f"tick + interval")


def test_the_effective_period_matches_the_interval_across_mixed_ticks(monkeypatch):
    """End to end: the loop should hold the configured cadence when it can, and
    never pad when it can't."""
    sleeps, h = _run(monkeypatch, [10.0, 30.0, 90.0, 5.0], interval=60,
                     stop_after=5)
    periods = [d + s for d, s in zip([10.0, 30.0, 90.0, 5.0], sleeps)]
    assert periods[0] == pytest.approx(60.0)
    assert periods[1] == pytest.approx(60.0)
    assert periods[2] <= 91.0, "the overrunning tick padded its period"
    assert periods[3] == pytest.approx(60.0)


# ── never a busy loop ────────────────────────────────────────────────────────

def test_a_permanently_overrunning_tick_still_yields(monkeypatch):
    """If every tick overruns, the loop must not spin at zero delay — it has to
    hand control back to the event loop each time."""
    sleeps, _ = _run(monkeypatch, [500.0] * 5, interval=60, stop_after=6)
    assert all(s > 0 for s in sleeps), "the loop asked to sleep 0 — busy spin"
    assert all(s <= 1.5 for s in sleeps), "an overrun should not pad the period"


# ── exceptions and shutdown ──────────────────────────────────────────────────

def test_an_exception_does_not_stop_the_loop_or_break_pacing(monkeypatch):
    sleeps, h = _run(monkeypatch, [RuntimeError("boom"), 20.0], interval=60,
                     stop_after=3)
    assert h.ticks == 2, "the loop stopped after a failing tick"
    # The failing tick advanced the fake clock 1s, so ~59s remains.
    assert sleeps[0] == pytest.approx(59.0, abs=1.0)
    assert sleeps[1] == pytest.approx(40.0)


def test_a_timeout_does_not_stop_the_loop(monkeypatch):
    sleeps, h = _run(monkeypatch, [asyncio.TimeoutError(), 10.0], interval=60,
                     stop_after=3)
    assert h.ticks == 2
    assert sleeps[1] == pytest.approx(50.0)


def test_cancellation_propagates_for_clean_shutdown(monkeypatch):
    """CancelledError must NOT be swallowed by the broad handler, or the lane
    would keep running through shutdown."""
    async def _go():
        h = _Harness(monkeypatch, [asyncio.CancelledError()], 60, stop_after=99)
        await h.run()

    # _Harness.run already asserts CancelledError escapes _pulse_lane.
    asyncio.run(_go())


# ── no overlap ───────────────────────────────────────────────────────────────

def test_ticks_never_overlap(monkeypatch):
    _, h = _run(monkeypatch, [90.0, 120.0, 30.0, 200.0], interval=60,
                stop_after=5)
    assert h.max_concurrent == 1, (
        f"{h.max_concurrent} tick bodies ran concurrently — the loop must await "
        f"one before starting the next")


def test_the_lane_exits_when_direct_ats_is_disabled(monkeypatch):
    """The disabled path must return, not fall through into the loop."""
    monkeypatch.setattr(server.settings, "direct_ats_enabled", False)
    asyncio.run(server._pulse_lane())   # returns immediately, no sleep patched
