"""Process-wide cap on concurrent headless Chromium instances.

Every Playwright launch in this app happens inside the SAME container that
already holds torch + sentence-transformers + FAISS + all five background lanes.
A headless Chromium costs roughly 300-500 MB RSS, and it is a child process — it
does not appear in our own RSS but it is charged to the container's memory limit
in full.

Nothing used to bound how many could be alive at once. An autofill request, a
preview request, and the discovery lane rendering a JS-heavy job page could all
launch a browser within the same second; three browsers is ~1.2 GB on top of the
ML stack, which is an OOM kill on any modestly sized container.

This gate serializes launches so the browser bill is bounded and predictable no
matter how many lanes or HTTP requests want one. Default concurrency is 1
(``BROWSER_MAX_CONCURRENCY``) — raise it only after raising the container's
memory limit to match.

Usage::

    from app.common.browser import browser_slot

    async with browser_slot("autofill"):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(...)
            ...

Waiters that cannot get a slot within ``BROWSER_SLOT_WAIT_SECONDS`` raise
:class:`BrowserBusy` rather than queueing forever behind a hung browser. A clear
error the caller can surface beats a request that hangs until the worker dies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from app.config import settings

log = logging.getLogger(__name__)


class BrowserBusy(RuntimeError):
    """Raised when no browser slot became free within the wait budget."""


# One semaphore per event loop. The lanes run their async work through
# asyncio.to_thread → asyncio.run, so several distinct loops exist over a
# process's life; a single module-level Semaphore would be bound to whichever
# loop happened to create it and raise "attached to a different loop" elsewhere.
_sems: dict[int, asyncio.Semaphore] = {}
_MAX_TRACKED_LOOPS = 32


def _limit() -> int:
    return max(1, int(getattr(settings, "browser_max_concurrency", 1) or 1))


def _sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _sems.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_limit())
        _sems[key] = sem
        if len(_sems) > _MAX_TRACKED_LOOPS:
            # Loops die with their threads and their ids are never reused while
            # alive; drop the oldest entries so this map cannot grow unbounded.
            for stale in [k for k in list(_sems) if k != key][: len(_sems) - _MAX_TRACKED_LOOPS]:
                _sems.pop(stale, None)
    return sem


@contextlib.asynccontextmanager
async def browser_slot(label: str = "browser"):
    """Hold a browser slot for the duration of the block.

    Raises :class:`BrowserBusy` if the wait budget expires before a slot frees.
    """
    sem = _sem()
    wait = float(getattr(settings, "browser_slot_wait_seconds", 120.0) or 120.0)
    started = time.monotonic()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=wait)
    except asyncio.TimeoutError:
        log.warning("browser slot [%s]: no slot free after %.0fs (limit=%d) — refusing "
                    "to launch a second browser rather than risk an OOM kill", label, wait, _limit())
        raise BrowserBusy(
            f"No browser slot available after {wait:.0f}s — another page render or "
            f"autofill is still running. Please retry in a moment."
        ) from None

    waited = time.monotonic() - started
    if waited > 1.0:
        log.info("browser slot [%s]: waited %.1fs for a free slot (limit=%d)",
                 label, waited, _limit())
    try:
        from app.common.memuse import log_snapshot
        log_snapshot(f"browser-launch:{label}")
    except Exception:  # telemetry must never break the work
        pass
    try:
        yield
    finally:
        sem.release()
        log.debug("browser slot [%s] released after %.1fs", label, time.monotonic() - started)
