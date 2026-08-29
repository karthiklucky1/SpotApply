"""``executemany_mode='values_plus_batch'`` against a REAL PostgreSQL.

SQLite cannot answer any of the questions this setting raises: it has no
psycopg2, no execute_batch, and no server log to count round trips in. So this
file is skipped unless ``POSTGRES_TEST_URL`` points at a throwaway database:

    POSTGRES_TEST_URL=postgresql+psycopg2://user@/db?host=/tmp&port=5432 \\
        python -m pytest tests/test_pg_executemany_batching.py

It creates and drops its OWN tables (``emb_*``) and touches nothing else, so it
is safe against any database you are willing to point it at. Do not point it at
production — it writes.

WHAT IT PINS.

The win: an executemany UPDATE of N rows must cost ONE round trip, not N. The
pulse lane's ~50-row registry flush was ~50 cross-region round trips, ~3.7s per
batch, ~22s of every tick.

The cost, which is the part worth a test: execute_batch makes MULTI-row
``cursor.rowcount`` meaningless, and SQLAlchemy responds by setting
``supports_sane_multi_rowcount = False``. That is fine here only because
``supports_sane_rowcount`` (the SINGLE-statement one) stays True — and that is
the one ``finals_budget._register`` reads to decide between UPDATE and INSERT
for a user's LLM spend counter. If a future SQLAlchemy or psycopg2 release ever
degrades single-statement rowcount too, that counter would start double-counting
silently, and this file is what says so out loud.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta

import pytest

PG_URL = os.getenv("POSTGRES_TEST_URL")
if not PG_URL:
    # Skipped at MODULE level, not per test. A per-test skipif would report one
    # SKIPPED line each, and CI caps the suite's skip groups (the guard that
    # stopped "make it green by skipping it" from happening a second time).
    # One environment-gated file should cost one skip, not six.
    pytest.skip("POSTGRES_TEST_URL not set — batching semantics need a real "
                "PostgreSQL, and SQLite cannot answer these questions",
                allow_module_level=True)

sa = pytest.importorskip("sqlalchemy")
from sqlalchemy import (Column, DateTime, Integer, String, create_engine,  # noqa: E402
                        delete, insert, select, text, update)
from sqlalchemy.orm import DeclarativeBase, Session  # noqa: E402

N = 50


class Base(DeclarativeBase):
    pass


class Reg(Base):
    """Mirrors the CompanyRegistry columns the pulse lane actually writes."""
    __tablename__ = "emb_registry"
    id = Column(Integer, primary_key=True)
    slug = Column(String)
    last_seen = Column(DateTime)
    job_count = Column(Integer)
    failure_count = Column(Integer)
    new_jobs_last_poll = Column(Integer)
    last_new_job_at = Column(DateTime)
    next_poll_at = Column(DateTime)
    poll_hash = Column(String)


class Usage(Base):
    __tablename__ = "emb_usage"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String)
    finals_count = Column(Integer)


def _engine(batched: bool):
    kw = {"executemany_mode": "values_plus_batch"} if batched else {}
    return create_engine(PG_URL, future=True, **kw)


def _seed(eng):
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.execute(insert(Reg), [{"id": i, "slug": f"co-{i}", "job_count": 0,
                                 "failure_count": 3} for i in range(1, N + 1)])
        s.commit()


def _poll_rows(now):
    """Exactly the shape pulse_lane._flush_polls builds."""
    return [{"id": i, "last_seen": now, "failure_count": 0,
             "new_jobs_last_poll": i % 3, "job_count": 10 + i,
             "last_new_job_at": now,
             "next_poll_at": now + timedelta(minutes=60),
             "poll_hash": f"h{i}"} for i in range(1, N + 1)]


def _find_log(eng):
    """The server's own log file — the only place round trips are countable.

    ``POSTGRES_TEST_LOG`` wins when set, because a cluster started with
    ``pg_ctl -l`` writes somewhere log_directory knows nothing about.
    """
    explicit = os.getenv("POSTGRES_TEST_LOG")
    if explicit and os.path.isfile(explicit):
        return explicit
    with eng.connect() as c:
        base = c.execute(text("SELECT setting FROM pg_settings "
                              "WHERE name='data_directory'")).scalar()
        rel = c.execute(text("SELECT setting FROM pg_settings "
                             "WHERE name='log_directory'")).scalar()
    cand = []
    for d in filter(None, [rel if (rel or "").startswith("/")
                           else os.path.join(base or "", rel or ""), base]):
        if os.path.isdir(d):
            cand += [os.path.join(d, f) for f in os.listdir(d)
                     if f.endswith(".log")]
    return max(cand, key=os.path.getmtime) if cand else None


def _count_roundtrips(eng, fn, table):
    """Statements the SERVER actually received. Ground truth, not a guess."""
    logfile = _find_log(eng)
    if not logfile:
        pytest.skip("set POSTGRES_TEST_LOG to the server log to count round trips")
    with eng.connect() as c:
        c.execute(text("SET log_statement='all'"))
        c.commit()
    mark = os.path.getsize(logfile)
    fn()
    time.sleep(0.4)
    with open(logfile, errors="replace") as f:
        f.seek(mark)
        chunk = f.read()
    stmts = re.findall(r"statement: (.*)", chunk)
    ups = [s for s in stmts
           if table in s and s.lstrip().upper().startswith("UPDATE")]
    return len(ups), sum(u.count(f"UPDATE {table}") for u in ups)


# ── the win ──────────────────────────────────────────────────────────────────

def test_executemany_update_collapses_to_one_roundtrip():
    now = datetime.utcnow()
    results = {}
    for batched in (False, True):
        eng = _engine(batched)
        _seed(eng)
        rows = _poll_rows(now)

        def _go():
            with Session(eng) as s:
                s.execute(update(Reg), rows)
                s.commit()

        entries, total = _count_roundtrips(eng, _go, "emb_registry")
        results[batched] = (entries, total)
        eng.dispose()

    plain_entries, plain_total = results[False]
    batch_entries, batch_total = results[True]
    assert plain_entries == N, f"expected {N} per-row statements, saw {plain_entries}"
    assert plain_total == N
    assert batch_entries == 1, (
        f"batched mode still made {batch_entries} round trips for {N} rows")
    assert batch_total == N, "the batch did not carry every row"


def test_batched_write_produces_identical_rows():
    """Every column the pulse lane writes must land the same either way."""
    now = datetime.utcnow().replace(microsecond=0)
    snapshots = {}
    for batched in (False, True):
        eng = _engine(batched)
        _seed(eng)
        with Session(eng) as s:
            s.execute(update(Reg), _poll_rows(now))
            s.commit()
        with Session(eng) as s:
            snapshots[batched] = [
                (r.id, r.last_seen, r.job_count, r.failure_count,
                 r.new_jobs_last_poll, r.last_new_job_at, r.next_poll_at,
                 r.poll_hash)
                for r in s.execute(select(Reg).order_by(Reg.id)).scalars()]
        eng.dispose()
    assert snapshots[False] == snapshots[True]
    assert len(snapshots[True]) == N


def test_a_deferral_moves_only_next_poll_at():
    """_defer_boards semantics under batching: a board that was never fetched
    must not acquire any evidence that it was."""
    now = datetime.utcnow()
    eng = _engine(True)
    _seed(eng)
    with Session(eng) as s:
        s.execute(update(Reg), _poll_rows(now))
        s.commit()
    with Session(eng) as s:
        before = {r.id: (r.last_seen, r.job_count, r.failure_count,
                         r.new_jobs_last_poll, r.last_new_job_at, r.poll_hash)
                  for r in s.execute(select(Reg)).scalars()}
    later = now + timedelta(minutes=2)
    with Session(eng) as s:
        s.execute(update(Reg), [{"id": i, "next_poll_at": later}
                                for i in range(1, N + 1)])
        s.commit()
    with Session(eng) as s:
        for r in s.execute(select(Reg)).scalars():
            assert (r.last_seen, r.job_count, r.failure_count,
                    r.new_jobs_last_poll, r.last_new_job_at,
                    r.poll_hash) == before[r.id], (
                f"row {r.id}: a deferral wrote a field only a poll may write")
            assert r.next_poll_at == later
    eng.dispose()


# ── the cost, and why it is safe here ────────────────────────────────────────

def test_single_statement_rowcount_is_unaffected():
    """THE LOAD-BEARING ONE.

    finals_budget._register does `if not res.rowcount: INSERT`. A wrong answer
    double-counts (or drops) a user's daily LLM spend. That call passes a single
    dict, so it is a single execute, and single-statement rowcount must survive
    the batching setting untouched.
    """
    for batched in (False, True):
        eng = _engine(batched)
        _seed(eng)
        assert eng.dialect.supports_sane_rowcount is True, (
            "single-statement rowcount is no longer reliable — "
            "finals_budget._register would start miscounting LLM spend")
        with Session(eng) as s:
            s.add(Usage(user_id="u1", finals_count=0))
            s.commit()
        with Session(eng) as s:
            hit = s.execute(
                text("UPDATE emb_usage SET finals_count = "
                     "COALESCE(finals_count,0)+:f WHERE user_id=:u"),
                {"f": 3, "u": "u1"})
            miss = s.execute(
                text("UPDATE emb_usage SET finals_count = "
                     "COALESCE(finals_count,0)+:f WHERE user_id=:u"),
                {"f": 3, "u": "nobody"})
            assert hit.rowcount == 1, f"batched={batched}: hit rowcount wrong"
            assert miss.rowcount == 0, f"batched={batched}: miss rowcount wrong"
            gone = s.execute(delete(Usage).where(Usage.user_id == "u1"))
            assert gone.rowcount == 1, f"batched={batched}: DELETE rowcount wrong"
            s.commit()
        eng.dispose()


def test_multi_rowcount_is_knowingly_surrendered():
    """The documented trade, asserted so it is a decision and not a surprise.

    If a future release made this True again the setting would simply be doing
    less than we think; if the ORM ever grew a rowcount-based check we rely on,
    this is where the change becomes visible.
    """
    plain, batched = _engine(False), _engine(True)
    # MUST connect first. The dialect sets these in initialize(), so reading
    # them off a fresh engine returns the class default (False) for BOTH modes
    # and would make this assertion pass for the wrong reason.
    for e in (plain, batched):
        with e.connect() as c:
            c.execute(text("SELECT 1"))
    assert plain.dialect.supports_sane_multi_rowcount is True
    assert batched.dialect.supports_sane_multi_rowcount is False
    plain.dispose()
    batched.dispose()


def test_orm_unit_of_work_still_round_trips_correctly():
    """The ORM's own insert/update batching must keep working: primary keys come
    back on insert, and a multi-object flush neither raises StaleDataError nor
    loses a write."""
    for batched in (False, True):
        eng = _engine(batched)
        _seed(eng)
        with Session(eng) as s:
            objs = [Usage(user_id=f"b{i}", finals_count=i) for i in range(N)]
            s.add_all(objs)
            s.commit()
            pks = [o.id for o in objs]
        assert len(set(pks)) == N and all(p is not None for p in pks), (
            f"batched={batched}: autoincrement PKs did not come back")

        with Session(eng) as s:
            rows = s.execute(select(Usage).order_by(Usage.id)).scalars().all()
            for o in rows:
                o.finals_count = (o.finals_count or 0) + 100
            s.commit()          # multi-object UPDATE flush
        with Session(eng) as s:
            vals = sorted(x.finals_count for x in
                          s.execute(select(Usage)).scalars())
        assert vals == sorted(i + 100 for i in range(N)), (
            f"batched={batched}: a unit-of-work update was lost")
        eng.dispose()
