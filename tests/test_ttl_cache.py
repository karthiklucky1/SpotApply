"""app/common/ttl_cache.py — the per-user aggregate cache behind the board's polls.

The 2026-09-16 HTTP sample put /api/freshness-stats at 52 s, /api/jobs at 36 s
and the once-a-minute /api/pipeline/live at p50 4 s, all recomputing COUNT /
median aggregates over a 65k-row per-user pool on every request. These tests
pin the cache's contract, not its speed: a miss computes once, a hit computes
nothing, a TTL expires, a vetoed value (a timed-out None) is never pinned, and
the store stays bounded.
"""
from __future__ import annotations

import threading

import pytest

from app.common import ttl_cache as tc


@pytest.fixture(autouse=True)
def _clean():
    tc.invalidate()
    yield
    tc.invalidate()


def test_a_miss_computes_once_and_a_hit_computes_nothing():
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"pool": 42}

    assert tc.get_or_compute("k1", 60, compute) == {"pool": 42}
    assert tc.get_or_compute("k1", 60, compute) == {"pool": 42}
    assert calls["n"] == 1


def test_an_expired_entry_is_recomputed(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tc.time, "monotonic", lambda: now[0])
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    assert tc.get_or_compute("k2", 30, compute) == 1
    now[0] += 29
    assert tc.get_or_compute("k2", 30, compute) == 1
    now[0] += 2
    assert tc.get_or_compute("k2", 30, compute) == 2


def test_a_vetoed_value_is_returned_but_never_stored():
    """A timed-out count comes back as None. Pinning None for five minutes
    would hide the recovery, so callers veto it with cache_if."""
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return None if calls["n"] == 1 else 7

    assert tc.get_or_compute("k3", 300, compute, cache_if=lambda v: v is not None) is None
    assert tc.get_or_compute("k3", 300, compute, cache_if=lambda v: v is not None) == 7
    assert tc.get_or_compute("k3", 300, compute, cache_if=lambda v: v is not None) == 7
    assert calls["n"] == 2


def test_none_is_a_legitimate_cached_value_when_not_vetoed():
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return None

    assert tc.get_or_compute("k4", 60, compute) is None
    assert tc.get_or_compute("k4", 60, compute) is None
    assert calls["n"] == 1, "None was stored, so the second call must not recompute"


def test_the_ttl_may_depend_on_the_value(monkeypatch):
    """Healthy payloads live five minutes; a degraded one only one."""
    now = [5000.0]
    monkeypatch.setattr(tc.time, "monotonic", lambda: now[0])
    ttl = lambda out: 60 if out.get("degraded") else 300  # noqa: E731
    tc.get_or_compute("k5", ttl, lambda: {"degraded": True})
    now[0] += 61
    assert tc.peek("k5") is tc._MISS
    tc.get_or_compute("k6", ttl, lambda: {"degraded": False})
    now[0] += 61
    assert tc.peek("k6") == {"degraded": False}


def test_a_non_positive_ttl_stores_nothing():
    tc.put("k7", "v", 0)
    assert tc.peek("k7") is tc._MISS
    tc.put("k7", "v", -5)
    assert tc.peek("k7") is tc._MISS


def test_invalidate_by_prefix_leaves_other_tenants_alone():
    tc.put("pool_count:a", 1, 60)
    tc.put("pool_count:b", 2, 60)
    tc.put("jobs_total:a", 3, 60)
    assert tc.invalidate("pool_count:") == 2
    assert tc.get("jobs_total:a") == 3
    assert tc.get("pool_count:a") is None


def test_the_store_is_bounded(monkeypatch):
    monkeypatch.setattr(tc, "MAX_ENTRIES", 5)
    for i in range(20):
        tc.put(f"burst:{i}", i, 60)
    assert tc.size() <= 5


def test_expired_entries_are_evicted_before_live_ones(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(tc.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(tc, "MAX_ENTRIES", 3)
    tc.put("old", 1, 10)      # expires at 110
    now[0] = 111
    tc.put("live1", 2, 60)
    tc.put("live2", 3, 60)
    tc.put("live3", 4, 60)    # store full: the expired 'old' must go first
    assert tc.peek("old") is tc._MISS
    assert tc.get("live1") == 2 and tc.get("live2") == 3 and tc.get("live3") == 4


def test_concurrent_readers_never_corrupt_the_store():
    errors = []

    def worker(i):
        try:
            for j in range(200):
                tc.put(f"t{i}:{j % 7}", j, 60)
                tc.get(f"t{(i + 1) % 8}:{j % 7}")
                if j % 50 == 0:
                    tc.invalidate(f"t{i}:")
        except Exception as e:                              # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
