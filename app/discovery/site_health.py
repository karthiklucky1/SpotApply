"""When most boards of one job system fail together, the SYSTEM is failing.

A board is retired after ``BOARD_DEACTIVATE_AFTER_FAILURES`` failed fetches in
a row. That rule reads every failure as a fact about ONE employer — and it is
the wrong reading whenever the failures share a cause upstream of the board:
the site blocked our address, the site is down, the API moved.

2026-11-14: Rippling's API began refusing production (403 on every board; the
same request succeeded from another network). Each refusal was a strike, the
most active boards are polled most often so they struck out first, and 960
boards holding 15,293 open jobs were retired as "unreachable x5" in a day.
Nothing was wrong with any of them.

This module is the missing piece of evidence: a small in-memory record of how
each job system has been answering lately. The failure recorders ask it before
they count a strike — if nearly everything from that system is failing, the
failure is not counted against the board (it is backed off and re-polled, the
same treatment a 429 gets). It can only ever SPARE a board: a board that is
really gone still fails when its neighbours answer, and is retired then.

Deliberately process-local and tiny: no table, no round-trip, nothing to
migrate. A restart forgets the window, and the first few failures after it are
counted as before — five in a row are needed to retire, so the window is full
long before any board gets there.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from app.config import settings
from app.discovery.base import BLOCKED_MARKER, UNREADABLE_MARKER

# One entry per fetch: (monotonic seconds, answered?). Bounded per system so a
# busy one cannot grow without limit; 600 covers several ticks of the largest.
_MAX_EVENTS = 600
_events: Dict[str, Deque[Tuple[float, bool]]] = {}
_lock = threading.Lock()


def _key(ats) -> str:
    return str(getattr(ats, "value", ats) or "").strip().lower()


def _window_seconds() -> float:
    return float(getattr(settings, "site_health_window_minutes", 15) or 15) * 60.0


def answered(error: Optional[str]) -> bool:
    """Did the SITE answer about this board, whatever the answer was?

    No error is an answer, and so is a 404 or a 410: the site is up and said
    "no such board" — that is board-level evidence, and counting it as a site
    failure would let a batch of junk slugs shield each other from retirement.
    """
    if not error:
        return True
    low = error.lower()
    if BLOCKED_MARKER in low or UNREADABLE_MARKER in low:
        return False            # refused, or answered in a shape we cannot read
    return "404" in low or "410" in low


def record(ats, ok: bool, now: Optional[float] = None) -> None:
    """Note one fetch of one board of this system. ``ok`` = the site answered."""
    key = _key(ats)
    if not key:
        return
    t = time.monotonic() if now is None else now
    with _lock:
        q = _events.get(key)
        if q is None:
            q = _events[key] = deque(maxlen=_MAX_EVENTS)
        q.append((t, bool(ok)))


def _counts(key: str, now: float) -> Tuple[int, int]:
    """(failures, total) inside the window."""
    cutoff = now - _window_seconds()
    with _lock:
        q = _events.get(key)
        if not q:
            return 0, 0
        while q and q[0][0] < cutoff:
            q.popleft()
        total = len(q)
        failures = sum(1 for _, ok in q if not ok)
    return failures, total


def mass_failing(ats, now: Optional[float] = None) -> bool:
    """True when this system is failing for (nearly) every board right now.

    Needs both a real sample (``SITE_HEALTH_MIN_FAILURES``) and a dominant
    share (``SITE_HEALTH_FAILURE_SHARE``): a handful of dead boards among
    hundreds of healthy ones never trips it.
    """
    if not bool(getattr(settings, "site_health_enabled", True)):
        return False
    key = _key(ats)
    if not key:
        return False
    failures, total = _counts(key, time.monotonic() if now is None else now)
    min_failures = int(getattr(settings, "site_health_min_failures", 20) or 20)
    share = float(getattr(settings, "site_health_failure_share", 0.8) or 0.8)
    return failures >= min_failures and total > 0 and (failures / total) >= share


def snapshot(now: Optional[float] = None) -> dict:
    """Per-system counts for logs and the admin health page. Names only."""
    t = time.monotonic() if now is None else now
    with _lock:
        keys = list(_events)
    out = {}
    for key in keys:
        failures, total = _counts(key, t)
        if total:
            out[key] = {"fetches": total, "failed": failures,
                        "mass_failing": mass_failing(key, t)}
    return out


def reset() -> None:
    """Tests only."""
    with _lock:
        _events.clear()
