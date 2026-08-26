#!/usr/bin/env python3
"""Measure what one board's upsert costs, per-job path vs batched path.

WHY STATEMENTS, NOT MILLISECONDS. This runs against local SQLite, where a
statement is microseconds. Production runs against Supabase over the network,
where a statement is a round trip. So the transferable number is the STATEMENT
and COMMIT count; wall-clock is derived from it at a range of round-trip times.

The production measurement this exists to explain: ``upsert_shared`` was 76.7%
of the pulse consumer at ~3,519ms per changed board, which left 37.1% of
completed fetches unprocessed (deferred_unconsumed) and put consumer demand at
~1.65x capacity.

    python scripts/bench_upsert.py                 # default sweep
    python scripts/bench_upsert.py --rtt 40        # pin the assumed RTT
    python scripts/bench_upsert.py --sizes 10,40,200
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import pathlib
import statistics
import sys
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
_DB = pathlib.Path(tempfile.gettempdir()) / f"bench_upsert_{os.getpid()}.db"
os.environ.setdefault("SQLITE_PATH", str(_DB))
os.environ.setdefault("FAISS_INDEX_PATH", str(_DB.with_suffix(".faiss")))
for _k in ("DATABASE_URL", "SUPABASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.setdefault(_k, "")

for _name, _attrs in (("sentence_transformers",
                       {"SentenceTransformer": object, "CrossEncoder": object}),
                      ("faiss", {}), ("rank_bm25", {"BM25Okapi": object})):
    if importlib.util.find_spec(_name) is None:
        _m = types.ModuleType(_name)
        _m.__path__ = []
        for _k2, _v in _attrs.items():
            setattr(_m, _k2, _v)
        sys.modules[_name] = _m
_u = types.ModuleType("sentence_transformers.util")
_u.cos_sim = lambda *a, **k: None
sys.modules.setdefault("sentence_transformers.util", _u)

from datetime import datetime, timedelta          # noqa: E402
from sqlalchemy import event, update              # noqa: E402
from app.db.init_db import engine, get_session, init_db   # noqa: E402
from app.db.models import Job, JobSource          # noqa: E402
import app.discovery.pipeline as P                # noqa: E402
from app.discovery.base import RawJob             # noqa: E402

POOL = "bench-pool"
DESC = "Backend engineer. Python, SQL, distributed systems. 5+ years. " * 12
STATS = {"stmts": 0, "commits": 0}


@event.listens_for(engine, "before_cursor_execute")
def _count(conn, cursor, statement, parameters, context, executemany):
    STATS["stmts"] += 1


@event.listens_for(engine, "commit")
def _commit(conn):
    STATS["commits"] += 1


def make_raw(i, desc=DESC):
    return RawJob(source="greenhouse", external_id=f"b-{i}", company=f"Co{i % 9}",
                  title=f"Senior Software Engineer {i}", location="Remote - US",
                  remote=True, url=f"https://boards.greenhouse.io/co/jobs/b-{i}",
                  description=desc,
                  posted_at=datetime.utcnow() - timedelta(days=2))


def reset_pool(n, age_hours):
    with get_session() as s:
        s.exec(Job.__table__.delete().where(Job.__table__.c.user_id == POOL))
        s.commit()
    seen = datetime.utcnow() - timedelta(hours=age_hours)
    with get_session() as s:
        for i in range(n):
            r = make_raw(i)
            s.add(Job(source=JobSource.GREENHOUSE, external_id=r.external_id,
                      company=r.company, title=r.title, location=r.location,
                      remote=True, url=r.url, description=r.description,
                      posted_at=r.posted_at, first_seen=seen, last_seen=seen,
                      content_hash=hashlib.sha256(r.description.encode()).hexdigest(),
                      cross_source_slug=P._cross_source_slug(
                          r.company, r.title, r.location),
                      user_id=POOL))
        s.commit()
    with get_session() as s:
        s.execute(update(Job).where(Job.user_id == POOL).values(last_seen=seen))
        s.commit()


def legacy_upsert(raws, **kw):
    """The per-job path, reached as production reaches it: a failed prefetch."""
    real = P.get_session
    state = {"n": 0}

    class _Boom:
        def __enter__(self):
            raise RuntimeError("prefetch disabled for benchmark")

        def __exit__(self, *a):
            return False

    def fake():
        if state["n"] == 0:
            state["n"] += 1
            return _Boom()
        return real()

    P.get_session = fake
    try:
        return P._upsert(raws, **kw)
    finally:
        P.get_session = real


def measure(fn, raws):
    STATS["stmts"] = STATS["commits"] = 0
    fn(raws, user_id=POOL, user_keywords=["software engineer"])
    return STATS["stmts"], STATS["commits"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="10,25,40,60,100,250")
    ap.add_argument("--revisit-hours", type=float, default=9.6,
                    help="production's measured productive revisit interval")
    ap.add_argument("--rtt", type=float, default=None,
                    help="assumed Supabase per-statement round trip, ms")
    args = ap.parse_args()

    init_db()
    # The legacy path logs its (deliberately induced) prefetch failure once per
    # board; that is the mechanism, not news.
    import logging
    logging.getLogger("app.discovery.pipeline").setLevel(logging.ERROR)
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    window_h = P._LAST_SEEN_REFRESH_SECONDS / 3600

    print(f"last_seen refresh window : {window_h:.0f}h")
    print(f"productive revisit       : {args.revisit_hours}h "
          f"({'OUTSIDE' if args.revisit_hours > window_h else 'inside'} the window "
          "— outside means every re-seen posting needs a write)")
    print()
    print(f"{'board':>6} | {'old stmts':>9} {'old commits':>11} | "
          f"{'new stmts':>9} {'new commits':>11} | {'stmt x':>7}")
    print("-" * 72)

    rows = []
    for n in sizes:
        raws = [make_raw(i) for i in range(n)]
        reset_pool(n, args.revisit_hours)
        old_s, old_c = measure(legacy_upsert, raws)
        reset_pool(n, args.revisit_hours)
        new_s, new_c = measure(P._upsert, raws)
        rows.append((n, old_s, old_c, new_s, new_c))
        print(f"{n:>6} | {old_s:>9} {old_c:>11} | {new_s:>9} {new_c:>11} | "
              f"{old_s / max(new_s, 1):>6.1f}x")

    # Derive the RTT that reproduces the production measurement, so the latency
    # projection is anchored to a real number rather than a guessed one.
    prod_ms, prod_board = 3519.0, 40
    ref = next((r for r in rows if r[0] == prod_board), rows[len(rows) // 2])
    implied_rtt = prod_ms / max(ref[1], 1)
    rtt = args.rtt if args.rtt is not None else implied_rtt
    print()
    print(f"Production measured {prod_ms:.0f}ms/changed board. A {ref[0]}-posting "
          f"board costs {ref[1]} statements on the old path,")
    print(f"which implies a per-statement round trip of ~{implied_rtt:.1f}ms "
          "— consistent with a pooled Supabase connection.")
    print()
    print(f"Projected wall-clock at {rtt:.1f}ms/statement:")
    print(f"{'board':>6} | {'old':>9} | {'new':>9} | {'saved':>9}")
    print("-" * 44)
    olds, news = [], []
    for n, o_s, _oc, n_s, _nc in rows:
        o_ms, n_ms = o_s * rtt, n_s * rtt
        olds.append(o_ms)
        news.append(n_ms)
        print(f"{n:>6} | {o_ms:>7.0f}ms | {n_ms:>7.0f}ms | {o_ms - n_ms:>7.0f}ms")

    def pct(xs, p):
        xs = sorted(xs)
        if not xs:
            return 0.0
        k = (len(xs) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

    print()
    print("Across the sampled board sizes (uniform weighting — the real board-size")
    print("distribution is not in this repo, so treat these as a shape, not a forecast):")
    for label, xs in (("old", olds), ("new", news)):
        print(f"  {label:>3}  p50 {pct(xs, .5):>7.0f}ms   p90 {pct(xs, .9):>7.0f}ms   "
              f"p95 {pct(xs, .95):>7.0f}ms   mean {statistics.mean(xs):>7.0f}ms")

    mean_old, mean_new = statistics.mean(olds), statistics.mean(news)
    print()
    print(f"Mean speedup: {mean_old / max(mean_new, 0.001):.1f}x")
    print()
    print("Consumer capacity implication (production numbers):")
    total = 118822.0            # measured total consumer seconds in a 72,000s window
    share = 0.767               # upsert_shared's measured share
    shared_s = total * share
    new_shared = shared_s * (mean_new / mean_old)
    new_total = total - shared_s + new_shared
    print(f"  upsert_shared was {shared_s:>9,.0f}s of {total:,.0f}s "
          f"({share * 100:.1f}%) against 72,000s of wall clock = "
          f"{total / 72000:.2f}x capacity")
    print(f"  at the measured speedup it becomes {new_shared:>9,.0f}s, "
          f"total {new_total:,.0f}s = {new_total / 72000:.2f}x capacity")
    if new_total < 72000:
        print("  -> consumer demand drops below capacity; deferred_unconsumed "
              "should go to ~0 and the next ceiling is elsewhere.")
    else:
        print("  -> consumer is STILL over capacity; batching alone is not enough.")

    try:
        os.remove(_DB)
    except OSError:
        pass


if __name__ == "__main__":
    main()
