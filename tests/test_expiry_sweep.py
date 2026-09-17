"""The stale-unscored expiry sweep runs PER OWNER, on the partial index, and one
owner's failure does not abandon the others.

Production 2026-09-16: ``scoring_cycle`` metadata showed ``expiry_stopped=
"error"`` in 159 of 271 cycles, the log carried four matching ``stale-unscored
expiry failed … QueryCanceled`` warnings, and one owner's OLDEST unscored
per-user copy was 2,024 hours (84 days) old — the age gate was not reaching it.
The sweep's statement was::

    rerank_score IS NULL AND user_id != '__shared__'
    AND coalesce(first_seen, discovered_at) < cutoff

No ``user_id`` prefix, and the indexed column wrapped in a function, so
``ix_job_unscored (user_id, first_seen) WHERE rerank_score IS NULL`` could serve
neither clause and the statement scanned the 1.48M-row job table — past
Supabase's statement timeout, every cycle. And because ONE ``except`` wrapped
the whole loop, the cancelled statement abandoned every owner's rows for that
cycle.

What these tests pin:

  * the index-friendly spelling of the known bound is PROVABLY the coalesce
    (every NULL combination, both directions);
  * both bounds still fire, and only on the rows they should;
  * the statement the code runs is one owner's slice of the partial index;
  * a cancelled statement on one owner is recorded and the sweep CONTINUES;
  * owners are visited round-robin across calls, so a slice spent on one
    owner's backlog is not spent on the same owner every 90 seconds.

The sweep is pointed at a PRIVATE SQLite database whose job table has the
shape production has — ``first_seen`` and ``discovered_at`` nullable (both
reached production via a bare ALTER TABLE with no backfill, so the oldest rows
are NULL there; the suite's shared schema declares them NOT NULL and cannot
seed that case) — with the partial index built from the SAME declaration
``init_db.ensure_performance_indexes`` uses. Pointing ``scoring_lane.get_session``
at it also means the owner list is exactly what each test seeded.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.common.freshness import (
    EXPIRY_SENTINEL_SCORE, known_before_expr, known_on_or_after_expr, known_ref,
)
from app.config import settings
from app.db.init_db import _PERF_INDEXES
from app.db.models import Job, JobSource
from app.discovery.pipeline import SHARED_POOL_USER
from app.strategy import scoring_lane as sl

_KNOWN, _POSTED = 5, 30
_NOW = datetime.utcnow()


def _days(n):
    return _NOW - timedelta(days=n)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """A private database: the job table with nullable timestamps, plus
    ``ix_job_unscored`` from the declaration list. The sweep, its owner
    enumeration and its writes all go through ``scoring_lane.get_session``,
    which is pointed here for the test's duration."""
    eng = create_engine(f"sqlite:///{tmp_path / 'sweep.db'}")
    md = MetaData()
    table = Job.__table__.to_metadata(md)
    table.c.first_seen.nullable = True
    table.c.discovered_at.nullable = True
    table.create(eng)
    with eng.begin() as conn:
        for name, tbl, cols in _PERF_INDEXES:
            if name == "ix_job_unscored":
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {tbl} {cols}"))

    @contextmanager
    def _session():
        with Session(eng) as s:
            yield s

    monkeypatch.setattr(sl, "get_session", _session)
    monkeypatch.setattr(sl, "_EXPIRY_RESUME", [])
    monkeypatch.setattr(settings, "scoring_max_job_age_days", _KNOWN)
    monkeypatch.setattr(settings, "scoring_max_posted_age_days", _POSTED)
    monkeypatch.setattr(settings, "scoring_expiry_max_seconds", 0)
    return eng


def _job(eng, user_id, ext, *, first_seen="unset", discovered="unset",
         posted=None, score=None, closed=False):
    """``first_seen``/``discovered`` are datetimes, None (SQL NULL), or
    "unset" (= now, the model default)."""
    j = Job(source=JobSource.GREENHOUSE, external_id=ext, company="Acme",
            title=f"Role {ext}", url=f"https://x.test/{ext}", user_id=user_id,
            rerank_score=score, posted_at=posted, is_closed=closed)
    if first_seen not in ("unset", None):
        j.first_seen = first_seen
    if discovered not in ("unset", None):
        j.discovered_at = discovered
    with Session(eng) as s:
        s.add(j)
        s.commit()
        s.refresh(j)
        jid = j.id
    # The ORM drops a None attribute from the INSERT so the column default
    # fires — the only way to get the pre-backfill NULL production has is to
    # write it after the fact, as the bare ALTER TABLE did.
    nulls = [c for c, v in (("first_seen", first_seen), ("discovered_at", discovered))
             if v is None]
    if nulls:
        with eng.begin() as conn:
            conn.execute(text(
                f"UPDATE job SET {', '.join(f'{c} = NULL' for c in nulls)} WHERE id = :id"),
                {"id": jid})
    return jid


def _row(eng, jid):
    with Session(eng) as s:
        return s.get(Job, jid)


def _ids(eng, *clauses):
    with Session(eng) as s:
        rows = s.exec(select(Job.id).where(*clauses)).all()
    return {r[0] if isinstance(r, tuple) else r for r in rows}


# ── 1. The index-friendly bound IS the coalesce bound ────────────────────────

def test_known_before_expr_matches_the_coalesce_on_every_null_combination(iso):
    """Every (first_seen, discovered_at) shape a production row can have:
    old / fresh / NULL on each axis. The two spellings must select the same
    rows in BOTH directions — including the row with neither timestamp, which
    matches neither (``NULL < x`` is never true, in either form)."""
    cutoff = _days(_KNOWN)
    shapes = {"old": _days(9), "fresh": _days(1), "null": None}
    seeded = {}
    for fs_name, fs in shapes.items():
        for d_name, d in shapes.items():
            seeded[(fs_name, d_name)] = _job(
                iso, "eqv", f"eqv-{fs_name}-{d_name}", first_seen=fs, discovered=d)
    mine = (Job.user_id == "eqv",)

    coalesce_before = _ids(iso, *mine, known_ref() < cutoff)
    friendly_before = _ids(iso, *mine, known_before_expr(cutoff))
    assert friendly_before == coalesce_before, (
        "known_before_expr selects a different set from coalesce(first_seen, "
        "discovered_at) < cutoff")

    coalesce_after = _ids(iso, *mine, known_ref() >= cutoff)
    friendly_after = _ids(iso, *mine, known_on_or_after_expr(cutoff))
    assert friendly_after == coalesce_after

    # Spot-check the shapes that matter: first_seen decides when present, the
    # discovered_at fallback decides when it is NULL, and both-NULL is in
    # neither set.
    assert seeded[("old", "fresh")] in friendly_before
    assert seeded[("fresh", "old")] not in friendly_before
    assert seeded[("null", "old")] in friendly_before, (
        "a row with first_seen NULL must fall back to discovered_at — these "
        "are exactly the pre-backfill rows the coalesce exists for")
    assert seeded[("null", "fresh")] in friendly_after
    assert seeded[("null", "null")] not in friendly_before | friendly_after
    assert not (friendly_before & friendly_after), "a row cannot be on both sides"


def test_the_friendly_bound_never_wraps_the_indexed_column(iso):
    """The whole point: no function over first_seen, so a btree on it can
    serve the bound as a range. Compiled text is the cheapest place to pin it."""
    sql = str(known_before_expr(_days(5)).compile(
        iso, compile_kwargs={"literal_binds": True})).lower()
    assert "coalesce" not in sql
    assert "job.first_seen <" in sql
    assert "job.first_seen is null" in sql


# ── 2. Both bounds fire, and only on the rows they should ────────────────────

def test_rows_on_both_sides_of_each_bound(iso):
    """One owner, every case on both sides of both bounds, plus the rows the
    sweep must never touch (scored, closed, shared pool)."""
    uid = "sides"
    null_first_seen_old = _job(iso, uid, "nfs-old", first_seen=None, discovered=_days(9))
    held_too_long = _job(iso, uid, "held", first_seen=_days(9), discovered=_days(9),
                         posted=_days(1))
    found_today_ancient = _job(iso, uid, "ancient", posted=_days(40))
    found_today_undated = _job(iso, uid, "undated")
    found_today_old_source = _job(iso, uid, "src-old", posted=_days(12))
    null_first_seen_fresh = _job(iso, uid, "nfs-fresh", first_seen=None,
                                 discovered=_days(1))
    scored_old = _job(iso, uid, "scored", first_seen=_days(40), score=72.0)
    closed_old = _job(iso, uid, "closed", first_seen=_days(40), closed=True)
    shared_old = _job(iso, SHARED_POOL_USER, "shared", first_seen=_days(40))

    out = sl._expire_stale_unscored()

    # The known bound, through the discovered_at fallback: first_seen NULL,
    # discovered 9 days ago.
    r = _row(iso, null_first_seen_old)
    assert r.rerank_score == EXPIRY_SENTINEL_SCORE
    assert r.expired_at is not None and r.scored_at is None
    assert "held" in r.rerank_reasoning
    # The known bound, directly.
    r = _row(iso, held_too_long)
    assert r.rerank_score == EXPIRY_SENTINEL_SCORE and "held" in r.rerank_reasoning
    # The posted bound: found today, source says 40 days.
    r = _row(iso, found_today_ancient)
    assert r.rerank_score == EXPIRY_SENTINEL_SCORE
    assert r.expired_at is not None
    assert "posting date" in r.rerank_reasoning
    # Fresh discoveries survive: no posted_at, an old-but-inside-30d source
    # date, and a NULL first_seen whose discovered_at is recent.
    for jid in (found_today_undated, found_today_old_source, null_first_seen_fresh):
        r = _row(iso, jid)
        assert r.rerank_score is None and r.expired_at is None, r.external_id
    # Never touched.
    assert _row(iso, scored_old).rerank_score == 72.0
    assert _row(iso, closed_old).rerank_score is None
    assert _row(iso, shared_old).rerank_score is None

    assert out["total"] == 3
    assert out["queue_stale"] == 2
    assert out["ancient_posting"] == 1
    assert out["stopped"] == ""
    # The owner keys: this owner, the always-probed NULL owner, nothing failed.
    assert set(out) >= {"total", "queue_stale", "ancient_posting", "stopped",
                        "owners", "owners_swept", "owners_failed"}
    assert out["owners"] == 2 and out["owners_swept"] == 2
    assert out["owners_failed"] == 0


# ── 3. The statement rides the partial index ─────────────────────────────────

def _plan(eng, stmt) -> str:
    sql = str(stmt.compile(eng, compile_kwargs={"literal_binds": True}))
    assert "coalesce" not in sql.lower(), sql
    with eng.connect() as conn:
        rows = conn.execute(text("EXPLAIN QUERY PLAN " + sql)).all()
    return " | ".join(str(r[-1]) for r in rows)


def test_the_per_owner_statement_uses_ix_job_unscored_on_sqlite(iso):
    """EXPLAIN QUERY PLAN for the statement the sweep actually runs — pass A,
    pass B and the NULL-owner probe — names the partial index. The private
    database carries the job table's own indexes plus ix_job_unscored, built
    from the same declaration ensure_performance_indexes runs, and nothing
    else: SQLite's planner has no statistics and always prefers two equalities
    (user_id, is_closed) over an equality plus a range, so on the full
    production index set it would pick ix_job_user_fresh here regardless of
    the bound's spelling. What this pins is that the statement is servable by
    the (user_id, first_seen) partial index at all — the property the old
    whole-table, coalesce-wrapped shape did not have."""
    known_cutoff, posted_cutoff = _days(_KNOWN), _days(_POSTED)
    pass_a = (known_before_expr(known_cutoff),)
    pass_b = (known_on_or_after_expr(known_cutoff),
              Job.posted_at.is_not(None), Job.posted_at < posted_cutoff)

    for label, owner in (("owner", Job.user_id == "u1"),
                         ("null owner", Job.user_id.is_(None))):
        for pname, extra in (("A", pass_a), ("B", pass_b)):
            plan = _plan(iso, sl._owner_expiry_select(owner, extra, 2000))
            assert "ix_job_unscored" in plan, (
                f"{label} / pass {pname}: the sweep's statement does not use the "
                f"partial index: {plan}")


def test_every_statement_is_scoped_to_one_owner_and_never_the_shared_pool(iso, monkeypatch):
    """No statement without an owner prefix, and the shared pool is never an
    owner: its 400k+ unscored rows are the mass that made the old statement a
    table scan, and they are never scored directly anyway."""
    a = _job(iso, "own-a", "a1", first_seen=_days(9))
    b = _job(iso, "own-b", "b1", first_seen=_days(9))
    shared = _job(iso, SHARED_POOL_USER, "s1", first_seen=_days(40))

    owners_seen: list = []
    real = sl._owner_expiry_select

    def _spy(owner_clause, extra, batch):
        sql = str(owner_clause.compile(compile_kwargs={"literal_binds": True}))
        owners_seen.append(sql)
        return real(owner_clause, extra, batch)

    monkeypatch.setattr(sl, "_owner_expiry_select", _spy)
    out = sl._expire_stale_unscored()

    assert owners_seen, "no per-owner statement was issued"
    assert all("user_id" in s for s in owners_seen)
    assert not any(SHARED_POOL_USER in s for s in owners_seen), (
        "a statement was scoped to the shared pool")
    assert any("'own-a'" in s for s in owners_seen)
    assert any("'own-b'" in s for s in owners_seen)
    assert _row(iso, a).rerank_score == EXPIRY_SENTINEL_SCORE
    assert _row(iso, b).rerank_score == EXPIRY_SENTINEL_SCORE
    assert _row(iso, shared).rerank_score is None
    assert out["owners"] == 3            # own-a, own-b, and the NULL probe
    assert out["owners_swept"] == 3


def test_owner_enumeration_excludes_the_shared_pool_and_probes_the_null_owner(iso):
    _job(iso, "enum-a", "e1", first_seen=_days(9))
    _job(iso, SHARED_POOL_USER, "e2", first_seen=_days(9))
    _job(iso, None, "e3", first_seen=_days(9))
    owners = sl._expiry_owners(0)
    assert owners == ["enum-a", None], owners


# ── 4. One owner's cancelled statement does not end the sweep ────────────────

class _QueryCanceled(Exception):
    """Stands in for psycopg2.errors.QueryCanceled — classified by class name,
    exactly as the driver's exception would be."""


def _failing_sessions(eng, failing_owner: str, exc_factory):
    """A get_session whose SELECTs against ``failing_owner`` raise — the
    statement fails, not the code building it."""

    class _Proxy:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, k):
            return getattr(self._s, k)

        def exec(self, stmt, *a, **kw):
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            if f"'{failing_owner}'" in sql and sql.lstrip().upper().startswith("SELECT"):
                raise exc_factory(sql)
            return self._s.exec(stmt, *a, **kw)

    @contextmanager
    def _session():
        with Session(eng) as s:
            yield _Proxy(s)

    return _session


def test_a_cancelled_statement_on_one_owner_still_sweeps_the_next(iso, monkeypatch):
    """The production failure mode, made survivable: owner A's statement is
    cancelled on the statement timeout; owner B's rows must still be stamped
    in the SAME call, the failure must be counted and named, and A's rows must
    be left exactly as they were for the next cycle."""
    a1 = _job(iso, "fail-a", "fa1", first_seen=_days(9))
    a2 = _job(iso, "fail-a", "fa2", first_seen=_days(9))
    b1 = _job(iso, "fail-b", "fb1", first_seen=_days(9))
    b2 = _job(iso, "fail-b", "fb2", posted=_days(40))

    monkeypatch.setattr(sl, "get_session", _failing_sessions(
        iso, "fail-a",
        lambda sql: OperationalError(sql, {}, _QueryCanceled(
            "canceling statement due to statement timeout"))))

    out = sl._expire_stale_unscored()

    assert _row(iso, b1).rerank_score == EXPIRY_SENTINEL_SCORE, (
        "owner B was abandoned because owner A's statement failed — the old "
        "whole-sweep except, back again")
    assert _row(iso, b2).rerank_score == EXPIRY_SENTINEL_SCORE
    assert _row(iso, a1).rerank_score is None and _row(iso, a2).rerank_score is None
    assert out["owners_failed"] == 1
    assert out["stopped"] == "statement_timeout"
    assert out["owners"] == 3 and out["owners_swept"] == 2   # B and the NULL probe
    assert out["total"] == 2


def test_a_generic_failure_is_recorded_as_error_and_the_sweep_continues(iso, monkeypatch):
    _job(iso, "err-a", "ea1", first_seen=_days(9))
    b1 = _job(iso, "err-b", "eb1", first_seen=_days(9))
    monkeypatch.setattr(sl, "get_session", _failing_sessions(
        iso, "err-a", lambda sql: RuntimeError("connection reset by peer")))

    out = sl._expire_stale_unscored()

    assert _row(iso, b1).rerank_score == EXPIRY_SENTINEL_SCORE
    assert out["owners_failed"] == 1
    assert out["stopped"] == "error"


def test_a_time_stop_outranks_a_recorded_failure(iso, monkeypatch):
    """`stopped` answers "why did the sweep end?". When the slice or the cycle
    deadline ended it, that is the answer; a per-owner failure that happened
    along the way is still visible in owners_failed."""
    import time as _time
    _job(iso, "dl-a", "da1", first_seen=_days(9))
    out = sl._expire_stale_unscored(deadline=_time.monotonic() - 1)
    assert out["stopped"] == "cycle_deadline"
    assert out["total"] == 0 and out["owners_swept"] == 0
    assert out["owners"] == 2      # enumerated, then handed the cycle back


def test_statement_timeout_classification():
    """SQLSTATE 57014 by code, QueryCanceled by class, the server's wording by
    message; anything else is a plain error."""
    class _WithCode(Exception):
        pgcode = "57014"

    assert sl._is_statement_timeout(OperationalError("s", {}, _WithCode("x")))
    assert sl._is_statement_timeout(OperationalError("s", {}, _QueryCanceled("x")))
    assert sl._is_statement_timeout(
        RuntimeError("canceling statement due to statement timeout"))
    assert not sl._is_statement_timeout(RuntimeError("connection reset"))
    assert not sl._is_statement_timeout(OperationalError("s", {}, ValueError("deadlock")))
    assert sl._failure_reason(RuntimeError("connection reset")) == "error"


# ── 5. Owners rotate across calls ────────────────────────────────────────────

def test_owners_are_visited_round_robin_across_calls(iso, monkeypatch):
    """A sweep that spends its slice on one owner must hand the NEXT slice to
    the next owner. Three owners, one stale row each, a clock that advances 1s
    per reading and a 5s slice: each call has time for exactly one owner. With
    the rotation, three calls sweep A, then B, then C. Without it, every call
    would start at A again — re-probing A's now-empty slice eats the reads B
    needed, and B is never reached inside the slice."""
    a = _job(iso, "rot-a", "ra", first_seen=_days(9))
    b = _job(iso, "rot-b", "rb", first_seen=_days(9))
    c = _job(iso, "rot-c", "rc", first_seen=_days(9))

    class _Clock:
        def __init__(self):
            self.t = 1000.0

        def __call__(self):
            self.t += 1.0
            return self.t

    monkeypatch.setattr(sl.time, "monotonic", _Clock())

    out1 = sl._expire_stale_unscored(batch=1, max_seconds=5)
    assert out1["stopped"] == "slice_spent"
    assert _row(iso, a).rerank_score == EXPIRY_SENTINEL_SCORE
    assert _row(iso, b).rerank_score is None and _row(iso, c).rerank_score is None
    assert sl._EXPIRY_RESUME == ["rot-b"], (
        f"the next call must start at the first owner this one did not reach, "
        f"got {sl._EXPIRY_RESUME}")

    out2 = sl._expire_stale_unscored(batch=1, max_seconds=5)
    assert out2["stopped"] == "slice_spent"
    assert _row(iso, b).rerank_score == EXPIRY_SENTINEL_SCORE, (
        "the second call started at A again instead of resuming at B")
    assert _row(iso, c).rerank_score is None
    assert sl._EXPIRY_RESUME == ["rot-c"]

    sl._expire_stale_unscored(batch=1, max_seconds=5)
    assert _row(iso, c).rerank_score == EXPIRY_SENTINEL_SCORE


def test_a_complete_sweep_wraps_the_rotation_to_the_first_owner(iso):
    _job(iso, "wrap-a", "wa", first_seen=_days(9))
    _job(iso, "wrap-b", "wb", first_seen=_days(9))
    out = sl._expire_stale_unscored()
    assert out["stopped"] == "" and out["owners_swept"] == out["owners"] == 3
    assert sl._EXPIRY_RESUME == ["wrap-a"]
