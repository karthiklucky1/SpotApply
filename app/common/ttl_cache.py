"""Process-local TTL cache for per-user aggregates the dashboard polls.

Why this exists. The board polls ``/api/pipeline/live`` every minute and opens
with ``/api/freshness-stats`` and ``/api/jobs``, and each of those recomputed
COUNT / median aggregates over the caller's WHOLE open pool on every request.
The founder's pool is 64,941 open per-user job rows. Post-deploy HTTP sample
(Railway, 2026-09-16 19:01-19:28, n=115): ``/api/freshness-stats`` 52 s,
``/api/jobs`` 36 s, ``/dashboard`` 15 s, ``/api/pipeline/live`` p50 4 s /
p95 9 s / max 10 s, 16x "dashboard read exceeded its budget — panel degraded
(QueryCanceled)", and Supabase's Disk IO budget nearly exhausted. The numbers
behind those tiles move on a LANE cadence (the pulse lane every 5 min, the
scoring lane every few minutes), not on a poll cadence, so recomputing them per
poll bought the user nothing and cost the database everything. A tile that is
up to five minutes old is the product; a tile that never loads is not.

Rules:

* Process-local and best-effort. Every entry can vanish (restart, eviction) and
  every caller must be able to recompute. Nothing here is a source of truth.
* Never hold the lock across ``fn()`` — the compute is a database round trip.
* Bounded: ``MAX_ENTRIES`` caps the store; on overflow, EXPIRED entries go
  first, then the ones closest to expiry.
* Callers decide what is worth keeping via ``cache_if`` — a degraded result
  (``None`` from a timed-out count) must not be pinned for five minutes.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional, Union

MAX_ENTRIES = 2048

_lock = threading.Lock()
# key -> (expires_at_monotonic, value)
_store: dict[str, tuple[float, Any]] = {}

# A sentinel distinct from None, because None is a legitimate cached value
# ("count unavailable" is exactly what some callers refuse to cache).
_MISS = object()


def get(key: str) -> Any:
    """The cached value for ``key``, or ``None`` when absent/expired.

    Use ``peek`` when ``None`` itself may have been stored.
    """
    hit = peek(key)
    return None if hit is _MISS else hit


def peek(key: str) -> Any:
    """Like ``get`` but returns the ``_MISS`` sentinel on a miss."""
    now = time.monotonic()
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return _MISS
        expires_at, value = entry
        if expires_at <= now:
            _store.pop(key, None)
            return _MISS
        return value


def put(key: str, value: Any, ttl_seconds: float) -> None:
    """Store ``value`` for ``ttl_seconds``. A non-positive TTL stores nothing."""
    ttl = float(ttl_seconds or 0)
    if ttl <= 0:
        return
    now = time.monotonic()
    with _lock:
        if key not in _store and len(_store) >= MAX_ENTRIES:
            _evict_locked(now)
        _store[key] = (now + ttl, value)


def get_or_compute(
    key: str,
    ttl_seconds: Union[float, Callable[[Any], float]],
    fn: Callable[[], Any],
    cache_if: Optional[Callable[[Any], bool]] = None,
) -> Any:
    """Return the cached value for ``key`` or compute, store and return it.

    ``ttl_seconds`` may be a number or a callable of the computed value, so a
    caller can keep a healthy response for five minutes and a degraded one for
    one. ``cache_if`` (value -> bool) vetoes storing altogether. ``fn`` runs
    OUTSIDE the lock; two concurrent misses both compute — acceptable, since a
    poll is one browser tab and the compute is bounded by its own timeout.
    """
    hit = peek(key)
    if hit is not _MISS:
        return hit
    value = fn()
    if cache_if is not None and not cache_if(value):
        return value
    ttl = ttl_seconds(value) if callable(ttl_seconds) else ttl_seconds
    put(key, value, ttl)
    return value


def invalidate(prefix: str = "") -> int:
    """Drop every entry whose key starts with ``prefix`` (all of them when
    empty). Returns how many were removed."""
    with _lock:
        if not prefix:
            n = len(_store)
            _store.clear()
            return n
        doomed = [k for k in _store if k.startswith(prefix)]
        for k in doomed:
            _store.pop(k, None)
        return len(doomed)


def size() -> int:
    with _lock:
        return len(_store)


def _evict_locked(now: float) -> None:
    """Make room for one more entry. Caller holds the lock.

    Expired entries go first; if the store is still full, the entries closest
    to expiry go until the bound holds. Bounded, so a burst of distinct filter
    signatures on /api/jobs can never grow the process without limit.
    """
    expired = [k for k, (exp, _v) in _store.items() if exp <= now]
    for k in expired:
        _store.pop(k, None)
    if len(_store) < MAX_ENTRIES:
        return
    by_expiry = sorted(_store.items(), key=lambda kv: kv[1][0])
    overflow = len(_store) - MAX_ENTRIES + 1
    for k, _entry in by_expiry[:overflow]:
        _store.pop(k, None)
