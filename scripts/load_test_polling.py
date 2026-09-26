#!/usr/bin/env python3
"""Concurrent polling load test for the dashboard's hot endpoints.

    python -m scripts.load_test_polling --base http://127.0.0.1:8765 \
        --clients 20 --requests 30 [--token $BEARER]

Point it at STAGING (or a local instance) — never at production: it is load,
not a probe. Prints p50/p95/max per endpoint and the status mix. A lab run
against local SQLite is a LOWER-FIDELITY number than Postgres over a network
and must be labelled as such wherever it is quoted.
"""
from __future__ import annotations

import argparse
import statistics
import threading
import time
from collections import Counter, defaultdict

import httpx

ENDPOINTS = ("/api/pipeline/live", "/api/notifications", "/api/public/freshness",
             "/api/search/state")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--clients", type=int, default=20)
    ap.add_argument("--requests", type=int, default=30)
    ap.add_argument("--token", default="")
    a = ap.parse_args()
    if "spotapply.ai" in a.base:
        raise SystemExit("refusing to load-test production; use staging or local")
    headers = {"Authorization": f"Bearer {a.token}"} if a.token else {}
    lat: dict = defaultdict(list)
    status: Counter = Counter()
    lock = threading.Lock()

    def client(i: int) -> None:
        with httpx.Client(base_url=a.base, headers=headers, timeout=60) as c:
            for n in range(a.requests):
                ep = ENDPOINTS[(i + n) % len(ENDPOINTS)]
                t0 = time.perf_counter()
                try:
                    r = c.get(ep)
                    code = r.status_code
                except Exception:
                    code = 0
                ms = (time.perf_counter() - t0) * 1000
                with lock:
                    lat[ep].append(ms)
                    status[code] += 1

    t0 = time.perf_counter()
    threads = [threading.Thread(target=client, args=(i,)) for i in range(a.clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    print(f"clients={a.clients} requests/client={a.requests} wall={wall:.1f}s status={dict(status)}")
    for ep, xs in sorted(lat.items()):
        xs.sort()
        p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
        print(f"  {ep:<26} n={len(xs):4d} p50={statistics.median(xs):7.1f}ms "
              f"p95={p95:7.1f}ms max={xs[-1]:7.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
