"""Memory guards: the browser concurrency gate + container memory telemetry.

Both exist because this container runs torch + three models' worth of ML + FAISS
+ five background lanes + headless Chromium under ONE platform memory limit, and
crossing that limit is an OOM kill with no traceback. These tests pin the two
behaviours that keep it from happening again:

  * only N Chromiums may be alive at once (default 1), and a waiter that can't
    get a slot is REFUSED rather than launching anyway;
  * memory readings never raise, whatever the host exposes.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from app.common import memuse
from app.common.browser import BrowserBusy, browser_slot
from app.common import browser as browser_mod
from app.config import settings


# ── browser gate ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_gate():
    """Each test starts with fresh per-loop semaphores and the default limit."""
    limit, wait = settings.browser_max_concurrency, settings.browser_slot_wait_seconds
    browser_mod._sems.clear()
    yield
    browser_mod._sems.clear()
    settings.browser_max_concurrency = limit
    settings.browser_slot_wait_seconds = wait


def test_slots_are_serialized_at_limit_one():
    """Two concurrent callers must not hold a browser slot at the same time —
    that overlap is exactly the ~800MB of simultaneous Chromium that OOM-killed
    the container."""
    settings.browser_max_concurrency = 1
    browser_mod._sems.clear()
    events: list[str] = []

    async def work(name: str):
        async with browser_slot(name):
            events.append(f"enter:{name}")
            await asyncio.sleep(0.05)
            events.append(f"exit:{name}")

    async def main():
        await asyncio.wait_for(asyncio.gather(work("a"), work("b")), timeout=10)

    asyncio.run(main())

    # Never two enters in a row: each block is enter/exit before the next starts.
    assert events[0].startswith("enter") and events[1].startswith("exit")
    assert events[2].startswith("enter") and events[3].startswith("exit")


def test_higher_limit_allows_overlap():
    """The cap is a knob, not a hard-coded 1 — raising it does permit overlap
    (for deployments that raised the container's memory limit to match)."""
    settings.browser_max_concurrency = 2
    browser_mod._sems.clear()
    events: list[str] = []

    async def work(name: str):
        async with browser_slot(name):
            events.append(f"enter:{name}")
            await asyncio.sleep(0.05)
            events.append(f"exit:{name}")

    async def main():
        await asyncio.wait_for(asyncio.gather(work("a"), work("b")), timeout=10)

    asyncio.run(main())

    assert events[0].startswith("enter") and events[1].startswith("enter")


def test_waiter_is_refused_rather_than_launching_anyway():
    """When the budget expires the caller gets BrowserBusy. Refusing is the
    whole point: a clear, retryable error beats a second browser and a kill."""
    settings.browser_max_concurrency = 1
    settings.browser_slot_wait_seconds = 0.2
    browser_mod._sems.clear()

    async def main():
        async def hog():
            async with browser_slot("hog"):
                await asyncio.sleep(1.0)

        task = asyncio.create_task(hog())
        await asyncio.sleep(0.05)          # let the hog take the only slot
        with pytest.raises(BrowserBusy):
            async with browser_slot("late"):
                pytest.fail("acquired a slot that should have been unavailable")
        await task
        # …and the slot is usable again once the hog finishes.
        async with browser_slot("after"):
            return True

    assert asyncio.run(asyncio.wait_for(main(), timeout=10)) is True


def test_slot_released_when_body_raises():
    """An exception inside the block must not leak the slot — otherwise one
    failed autofill would wedge every later browser launch."""
    settings.browser_max_concurrency = 1
    settings.browser_slot_wait_seconds = 0.5
    browser_mod._sems.clear()

    async def main():
        with pytest.raises(ValueError):
            async with browser_slot("boom"):
                raise ValueError("kaboom")
        async with browser_slot("next"):
            return True

    assert asyncio.run(asyncio.wait_for(main(), timeout=10)) is True


def test_works_across_event_loops():
    """The lanes run browser work via asyncio.run() inside worker threads, so a
    new event loop each time. A single module-level Semaphore would raise
    'attached to a different loop'; the gate keeps one per loop."""
    settings.browser_max_concurrency = 1
    browser_mod._sems.clear()
    results: list = []

    async def use():
        async with browser_slot("threaded"):
            await asyncio.sleep(0.01)
        return True

    def run():
        try:
            results.append(asyncio.run(use()))
        except Exception as exc:  # noqa: BLE001 — the failure we're pinning
            results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results == [True] * 5, results


def test_loop_semaphore_map_is_bounded():
    """One entry per event loop, and loops are created per lane tick — the map
    must not grow forever."""
    browser_mod._sems.clear()

    async def touch():
        async with browser_slot("x"):
            pass

    for _ in range(browser_mod._MAX_TRACKED_LOOPS + 10):
        asyncio.run(touch())

    assert len(browser_mod._sems) <= browser_mod._MAX_TRACKED_LOOPS + 1


# ── memory telemetry ─────────────────────────────────────────────────────────

def test_snapshot_has_every_key_and_never_raises():
    snap = memuse.snapshot()
    for key in ("rss_mb", "container_mb", "limit_mb", "used_pct", "non_python_mb"):
        assert key in snap
    # Values are numeric-or-None — a host that doesn't expose a cgroup limit
    # must degrade to None, not blow up the health endpoint.
    for value in snap.values():
        assert value is None or isinstance(value, (int, float))


def test_format_snapshot_is_always_a_string():
    assert isinstance(memuse.format_snapshot(), str)
    # Fully-unavailable host: still a usable log line, not an exception.
    empty = {"rss_mb": None, "container_mb": None, "limit_mb": None,
             "used_pct": None, "non_python_mb": None}
    assert memuse.format_snapshot(empty) == "memory stats unavailable"


def test_used_pct_is_container_usage_over_limit(monkeypatch):
    """used_pct must compare CONTAINER usage (which includes Chromium children)
    to the limit — not our own RSS. Measuring RSS is what makes an OOM look
    inexplicable: the Python heap can sit at 700MB while the container is at 95%."""
    monkeypatch.setattr(memuse, "rss_mb", lambda: 700.0)
    monkeypatch.setattr(memuse, "cgroup_usage_mb", lambda: 1900.0)
    monkeypatch.setattr(memuse, "cgroup_limit_mb", lambda: 2000.0)

    snap = memuse.snapshot()
    assert snap["used_pct"] == 95.0
    assert snap["non_python_mb"] == 1200.0   # the browsers
    assert memuse.under_pressure(85.0) is True


def test_no_limit_means_no_false_pressure(monkeypatch):
    """With no cgroup limit visible we cannot know the headroom — never claim
    pressure and never block work on a reading we couldn't take."""
    monkeypatch.setattr(memuse, "cgroup_limit_mb", lambda: None)
    assert memuse.snapshot()["used_pct"] is None
    assert memuse.under_pressure(50.0) is False


def test_unlimited_cgroup_reads_as_no_limit(tmp_path, monkeypatch):
    """cgroup v2 writes the literal 'max' and v1 a near-2^63 sentinel when
    uncapped; both must read as 'no limit', not a petabyte ceiling."""
    v2 = tmp_path / "memory.max"
    v2.write_text("max\n")
    monkeypatch.setattr(memuse, "_V2_MAX", str(v2))
    monkeypatch.setattr(memuse, "_V1_MAX", str(tmp_path / "missing"))
    assert memuse.cgroup_limit_mb() is None

    v2.write_text(str(2 ** 63 - 4096))
    assert memuse.cgroup_limit_mb() is None


def test_log_snapshot_warns_above_threshold(monkeypatch, caplog):
    """The WARNING line 30 seconds before the kill is the whole diagnostic."""
    monkeypatch.setattr(memuse, "rss_mb", lambda: 900.0)
    monkeypatch.setattr(memuse, "cgroup_usage_mb", lambda: 1900.0)
    monkeypatch.setattr(memuse, "cgroup_limit_mb", lambda: 2000.0)

    with caplog.at_level("INFO"):
        memuse.log_snapshot("test", warn_pct=85.0)
    assert any(r.levelname == "WARNING" and "MEMORY HIGH" in r.getMessage()
               for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(memuse, "cgroup_usage_mb", lambda: 900.0)
    with caplog.at_level("INFO"):
        memuse.log_snapshot("test", warn_pct=85.0)
    assert all(r.levelname != "WARNING" for r in caplog.records)
