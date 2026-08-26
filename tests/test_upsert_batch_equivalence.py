"""The batched upsert must produce the same database as the per-job one.

``_upsert`` used to open a session PER CANDIDATE: SELECT the whole row,
mutate one column, COMMIT. The prefetch snapshot was supposed to make that
rare, but it only skips a re-seen posting while ``last_seen`` is inside
``_LAST_SEEN_REFRESH_SECONDS`` (6h) — and production's measured productive
revisit was ~9.6h, so the fast path essentially never fired and a 40-posting
board cost 82 statements and 40 commits instead of 2 and 0.

The batched path decides the same things from the same snapshot and defers the
writes into bulk statements. This file is the proof that "decides the same
things" is literally true: every case runs BOTH implementations against
identical inputs into two isolated pools, then diffs every column of every row.

The old path is still reachable in production (the prefetch can fail, and a
cross-source upgrade is never batched), so this is not testing dead code —
it is pinning the two against each other.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Application, FunnelEvent, Job, JobSource
from app.discovery import pipeline as P
from app.discovery.base import RawJob

OLD_POOL = "eqv-old"
NEW_POOL = "eqv-new"

# Every column that describes the posting. `id` and `discovered_at` are excluded
# on purpose: ids are pool-local and discovered_at is a wall-clock default, so
# neither can be equal across two runs. Everything else must be.
_SKIP = {"id", "discovered_at"}
COMPARED = sorted(c.name for c in Job.__table__.columns if c.name not in _SKIP)


def _clean():
    with get_session() as s:
        jobs = s.exec(select(Job).where(Job.user_id.in_([OLD_POOL, NEW_POOL]))).all()
        ids = [j.id for j in jobs]
        if ids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(ids)))
            s.exec(delete(Application).where(Application.job_id.in_(ids)))
            s.exec(delete(Job).where(Job.id.in_(ids)))
        s.commit()


@pytest.fixture(autouse=True)
def _isolate():
    _clean()
    yield
    _clean()


def raw(i, *, title="Senior Backend Engineer", company=None, desc=None,
        source="greenhouse", location="Remote - US", posted_days=2, ext=None):
    return RawJob(
        source=source,
        external_id=ext if ext is not None else f"eq-{i}",
        company=company if company is not None else f"Co{i % 5}",
        title=f"{title} {i}",
        location=location,
        remote=True,
        url=f"https://boards.greenhouse.io/co/jobs/eq-{i}",
        description=desc if desc is not None else f"Build things with Python. Role {i}. " * 8,
        posted_at=None if posted_days is None
        else datetime.utcnow() - timedelta(days=posted_days),
    )


def _seed(pool, raws, *, last_seen_age_h):
    """Pre-load rows as a previous poll would have, at a chosen last_seen age."""
    seen = datetime.utcnow() - timedelta(hours=last_seen_age_h)
    with get_session() as s:
        for n, r in enumerate(raws):
            s.add(Job(
                # A real seeded row has ALREADY been embedded. Without this the
                # "content changed -> force a re-embed" assertion is vacuous:
                # embedding_id starts None, so a path that forgot to clear it
                # still looks correct. (It did — this fixture missed a mutation.)
                embedding_id=1000 + n,
                source=JobSource(r.source), external_id=r.external_id,
                company=r.company, title=r.title, location=r.location,
                remote=r.remote, url=r.url, description=r.description,
                posted_at=r.posted_at, first_seen=seen, last_seen=seen,
                content_hash=hashlib.sha256((r.description or "").encode()).hexdigest(),
                cross_source_slug=P._cross_source_slug(r.company, r.title, r.location),
                user_id=pool,
            ))
        s.commit()


def _snapshot(pool):
    """Every compared column of every row, keyed by the dedupe key."""
    with get_session() as s:
        rows = s.exec(select(Job).where(Job.user_id == pool)).all()
    out = {}
    for j in rows:
        key = (j.source.value if hasattr(j.source, "value") else str(j.source),
               j.external_id)
        out[key] = {c: getattr(j, c) for c in COMPARED}
    return out


def _normalise(snap, pool):
    """Erase the two legitimately-different values: the pool name itself, and
    timestamps the two runs cannot produce identically. Timestamps are compared
    as a COARSE bucket rather than dropped, so a path that forgot to write one
    at all still fails."""
    out = {}
    for key, row in snap.items():
        row = dict(row)
        row["user_id"] = "<pool>" if row["user_id"] == pool else row["user_id"]
        for col in ("first_seen", "last_seen"):
            v = row.get(col)
            row[col] = None if v is None else \
                round((datetime.utcnow() - v).total_seconds() / 60)
        out[key] = row
    return out


def _run_both(raws_old, raws_new=None, **kw):
    """Same input through both implementations, into two isolated pools."""
    raws_new = raws_old if raws_new is None else raws_new
    monkey_off = P._upsert
    # OLD path: force the per-job branch by making the prefetch report failure.
    with_prefetch = {}

    old_n = _upsert_legacy(raws_old, user_id=OLD_POOL, **kw)
    new_n = monkey_off(raws_new, user_id=NEW_POOL, **kw)
    del with_prefetch
    return old_n, new_n


def _upsert_legacy(raws, **kw):
    """The per-job path, reached the way production reaches it: a failed
    prefetch. That branch is unchanged by this patch, so it is the reference."""
    import app.discovery.pipeline as mod
    real = mod.get_session
    state = {"n": 0}

    class _Boom:
        def __enter__(self):
            raise RuntimeError("prefetch disabled for equivalence run")

        def __exit__(self, *a):
            return False

    def fake_get_session():
        # Only the FIRST get_session() inside _upsert is the prefetch; fail it
        # and let every later call through, which is exactly the production
        # failure mode the fallback exists for.
        if state["n"] == 0:
            state["n"] += 1
            return _Boom()
        return real()

    mod.get_session = fake_get_session
    try:
        return mod._upsert(raws, **kw)
    finally:
        mod.get_session = real


def assert_equivalent(label=""):
    old = _normalise(_snapshot(OLD_POOL), OLD_POOL)
    new = _normalise(_snapshot(NEW_POOL), NEW_POOL)
    assert set(old) == set(new), (
        f"{label}: different rows.\n only-old={sorted(set(old) - set(new))}\n"
        f" only-new={sorted(set(new) - set(old))}")
    for key in old:
        for col in COMPARED:
            assert old[key][col] == new[key][col], (
                f"{label}: {key} column '{col}' diverged: "
                f"old={old[key][col]!r} new={new[key][col]!r}")


# ── The cases the task asked for ─────────────────────────────────────────────

def test_entirely_new_board():
    rs = [raw(i) for i in range(25)]
    old_n, new_n = _run_both(rs)
    assert old_n == new_n == 25
    assert_equivalent("new board")


def test_unchanged_board_inside_the_refresh_window():
    rs = [raw(i) for i in range(12)]
    _seed(OLD_POOL, rs, last_seen_age_h=1)
    _seed(NEW_POOL, rs, last_seen_age_h=1)
    old_n, new_n = _run_both(rs)
    assert old_n == new_n == 0
    assert_equivalent("unchanged, fresh last_seen")


def test_unchanged_board_past_the_refresh_window():
    """THE PRODUCTION CASE: revisit ~9.6h against a 6h window. Every row needs
    its last_seen refreshed; the two paths must refresh exactly the same set."""
    rs = [raw(i) for i in range(12)]
    _seed(OLD_POOL, rs, last_seen_age_h=9.6)
    _seed(NEW_POOL, rs, last_seen_age_h=9.6)
    old_n, new_n = _run_both(rs)
    assert old_n == new_n == 0
    assert_equivalent("unchanged, stale last_seen")
    # ...and the refresh actually happened.
    with get_session() as s:
        for pool in (OLD_POOL, NEW_POOL):
            for j in s.exec(select(Job).where(Job.user_id == pool)).all():
                assert (datetime.utcnow() - j.last_seen).total_seconds() < 300, pool


def test_one_new_job_on_a_known_board():
    known = [raw(i) for i in range(10)]
    _seed(OLD_POOL, known, last_seen_age_h=9.6)
    _seed(NEW_POOL, known, last_seen_age_h=9.6)
    old_n, new_n = _run_both(known + [raw(99)])
    assert old_n == new_n == 1
    assert_equivalent("one new job")


def test_hundreds_of_new_jobs_cross_the_insert_chunk():
    n = P._BULK_INSERT_CHUNK * 2 + 37       # forces multiple INSERT batches
    rs = [raw(i) for i in range(n)]
    old_n, new_n = _run_both(rs)
    assert old_n == new_n == n
    assert_equivalent("multi-chunk insert")


def test_existing_jobs_updated():
    rs = [raw(i) for i in range(8)]
    _seed(OLD_POOL, rs, last_seen_age_h=1)
    _seed(NEW_POOL, rs, last_seen_age_h=1)
    edited = [raw(i, desc=f"REWRITTEN description for {i}. " * 9) if i < 4 else raw(i)
              for i in range(8)]
    old_n, new_n = _run_both(edited)
    assert old_n == new_n == 0
    assert_equivalent("content edits")
    with get_session() as s:
        for pool in (OLD_POOL, NEW_POOL):
            for j in s.exec(select(Job).where(Job.user_id == pool)).all():
                if "REWRITTEN" in (j.description or ""):
                    assert j.embedding_id is None, f"{pool}: re-embed not forced"


def test_duplicate_jobs_within_one_batch():
    """Same external_id twice. The later occurrence must win the description
    and must NOT produce a second row."""
    rs = [raw(1, ext="dup-1"), raw(2), raw(3, ext="dup-1", desc="SECOND version. " * 9)]
    old_n, new_n = _run_both(rs)
    assert old_n == new_n
    assert_equivalent("in-batch duplicate")
    with get_session() as s:
        for pool in (OLD_POOL, NEW_POOL):
            rows = s.exec(select(Job).where(Job.user_id == pool,
                                            Job.external_id == "dup-1")).all()
            assert len(rows) == 1, f"{pool}: duplicate row created"
            assert "SECOND" in (rows[0].description or ""), f"{pool}: later write lost"


def test_malformed_and_missing_optional_fields():
    rs = [
        raw(1, desc=""),                       # no description at all
        raw(2, posted_days=None),              # no posted_at
        raw(3, location=""),                   # no location
        raw(4, company="Ünïcødé & Co, Inc."),  # awkward company text
        raw(5, desc="x" * 40000),              # very long description
        raw(6, title="Senior Backend Engineer (Remote) — $180k–$220k"),
    ]
    old_n, new_n = _run_both(rs)
    assert old_n == new_n == 6
    assert_equivalent("malformed / missing optionals")


def test_future_dated_and_ancient_source_timestamps():
    rs = [raw(1, posted_days=-3), raw(2, posted_days=900), raw(3, posted_days=0)]
    old_n, new_n = _run_both(rs)
    assert old_n == new_n == 3
    assert_equivalent("odd source timestamps")


def test_mixed_board_every_case_at_once():
    """New + unchanged-fresh + unchanged-stale + edited, in one call — the shape
    a real changed board actually has."""
    known_fresh = [raw(i) for i in range(5)]
    known_stale = [raw(100 + i) for i in range(5)]
    _seed(OLD_POOL, known_fresh, last_seen_age_h=1)
    _seed(NEW_POOL, known_fresh, last_seen_age_h=1)
    _seed(OLD_POOL, known_stale, last_seen_age_h=20)
    _seed(NEW_POOL, known_stale, last_seen_age_h=20)
    batch = (known_fresh
             + [raw(100 + i, desc=f"edited {i}. " * 9) for i in range(2)]
             + known_stale[2:]
             + [raw(200 + i) for i in range(4)])
    old_n, new_n = _run_both(batch)
    assert old_n == new_n == 4
    assert_equivalent("mixed board")


def test_return_value_counts_inserts_only():
    rs = [raw(i) for i in range(6)]
    _seed(OLD_POOL, rs[:4], last_seen_age_h=9.6)
    _seed(NEW_POOL, rs[:4], last_seen_age_h=9.6)
    old_n, new_n = _run_both(rs)
    assert old_n == new_n == 2, "updates and touches must not be counted"


def test_funnel_discovered_event_per_inserted_row():
    rs = [raw(i) for i in range(7)]
    _run_both(rs)
    with get_session() as s:
        for pool in (OLD_POOL, NEW_POOL):
            ids = [j.id for j in s.exec(select(Job).where(Job.user_id == pool)).all()]
            events = s.exec(select(FunnelEvent).where(
                FunnelEvent.job_id.in_(ids),
                FunnelEvent.stage == "discovered")).all()
            assert len(events) == 7, f"{pool}: {len(events)} discovered events"
            assert all(e.passed for e in events)


def test_inserted_row_is_fully_populated():
    """ABSOLUTE assertions, not relative ones.

    Both paths now build new rows through the same ``_build_job``, which is what
    stops them drifting — but it also means an equivalence diff cannot see a
    field that is wrong in BOTH. A mutation run proved that: dropping on_role,
    dropping the card-face facets, and discarding posted_at all left every
    equivalence case green. These are the checks that fail instead.
    """
    posted = datetime.utcnow() - timedelta(days=2)
    r = RawJob(
        source="greenhouse", external_id="abs-1", company="Acme Corp",
        title="Senior Backend Engineer", location="Remote - US", remote=True,
        url="https://boards.greenhouse.io/acme/jobs/abs-1",
        description="Backend engineer. Python and SQL. $150,000 - $200,000 a year. "
                    "We sponsor H-1B visas for the right candidate. " * 4,
        posted_at=posted,
    )
    assert P._upsert([r], user_id=NEW_POOL, user_keywords=["backend engineer"]) == 1

    with get_session() as s:
        job = s.exec(select(Job).where(Job.user_id == NEW_POOL,
                                       Job.external_id == "abs-1")).one()

        # Identity + dedupe keys
        assert job.source == JobSource.GREENHOUSE
        assert job.company == "Acme Corp"
        assert job.title == "Senior Backend Engineer"
        assert job.url == r.url
        assert job.description == r.description
        assert job.content_hash == hashlib.sha256(r.description.encode()).hexdigest()
        assert job.cross_source_slug == P._cross_source_slug(
            r.company, r.title, r.location)

        # The SOURCE's date must survive intact — it is one of the two
        # freshness axes (app/common/freshness.py) and cannot be re-derived.
        assert job.posted_at == posted

        # Both known-age references are stamped at insert time.
        assert job.first_seen is not None and job.last_seen is not None
        assert (datetime.utcnow() - job.first_seen).total_seconds() < 300

        # Board-render precomputation: on_role answers the role filter from a
        # column, and the facets draw the salary/sponsorship chips without the
        # board loading the posting.
        assert job.on_role is True, "role gate answer not precomputed"
        assert job.salary_text, "salary facet not computed"
        assert job.sponsorship_json, "sponsorship facet not computed"


def test_role_and_country_gates_still_apply_before_any_batching():
    """The cheap in-process gates run BEFORE the snapshot, so batching must not
    let a gated posting through."""
    off_role = raw(1, title="Mechanical Engineer")
    on_role = raw(2, title="Backend Engineer")
    n = P._upsert([off_role, on_role], user_id=NEW_POOL,
                  user_keywords=["backend engineer"],
                  role_gate_terms=["backend engineer"])
    assert n == 1
    with get_session() as s:
        rows = s.exec(select(Job).where(Job.user_id == NEW_POOL)).all()
        assert len(rows) == 1
        assert "Backend" in rows[0].title


def test_upsert_cost_does_not_scale_with_board_size():
    """THE GUARD. The defect this patch fixes was not a wrong answer — it was a
    right answer bought one round trip at a time.

    A re-seen board costs a fixed handful of statements now, whatever its size.
    If someone reintroduces a per-posting SELECT/UPDATE/COMMIT the counts here
    go linear again, and against Supabase (a measured ~44ms per round trip)
    that is what made upsert_shared 76.7% of the discovery consumer.
    """
    from sqlalchemy import event
    from app.db.init_db import engine

    seen = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        seen["n"] += 1

    try:
        counts = {}
        for size in (10, 80):
            _clean()
            rs = [raw(i) for i in range(size)]
            # Past the refresh window: every posting needs its last_seen written,
            # which is the production case (revisit ~9.6h vs a 6h window).
            _seed(NEW_POOL, rs, last_seen_age_h=9.6)
            seen["n"] = 0
            assert P._upsert(rs, user_id=NEW_POOL) == 0
            counts[size] = seen["n"]
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert counts[80] <= counts[10] + 2, (
        f"statement count scaled with board size: {counts} — a per-posting "
        "round trip has been reintroduced")
    assert counts[80] <= 8, f"unexpectedly chatty for one board: {counts}"


def test_bulk_insert_failure_falls_back_to_per_job(monkeypatch):
    """A failed bulk statement must not lose the board.

    ``_bulk_insert_jobs`` degrades to the per-job insert on any failure — the
    same guarantee ``_insert_job_returning_id`` already makes for an unknown
    dialect. Simulated by making the multi-row statement itself unusable.
    """
    import sqlalchemy.dialects.sqlite as _sq

    def _fail(*a, **k):
        raise RuntimeError("simulated bulk statement failure")

    monkeypatch.setattr(_sq, "insert", _fail)

    n = P._upsert([raw(i) for i in range(5)], user_id=NEW_POOL)
    assert n == 5, "fallback must still insert every new posting"
    with get_session() as s:
        rows = s.exec(select(Job).where(Job.user_id == NEW_POOL)).all()
        assert len(rows) == 5
        assert all(r.content_hash and r.cross_source_slug for r in rows)
        assert all(r.first_seen and r.last_seen for r in rows)
