"""Two production hotspots found in the Postgres logs on 2026-08-01.

Neither was breaking anything visibly, which is exactly why both had run for
days: one was drowning the error log, the other was eating most of a lane's
wall clock.

1. 6,099 duplicate-key ERRORs in 24 hours on uq_job_user_source_external_id —
   100% of all Postgres errors in the window, 95.7% inside one two-hour ingest
   burst. The duplicate checks in _upsert run in a DIFFERENT transaction from
   the insert, so two lanes polling the same board both saw "no row" and both
   inserted. The IntegrityError handler meant behaviour was correct, but every
   collision still cost a failed round trip, a transaction abort and an ERROR
   line — and buried any real error underneath.

2. `SELECT DISTINCT user_id FROM job WHERE rerank_score IS NOT NULL AND ...`
   measured at 31-38s per execution, 277 times in a day, on a lane that ticks
   every 90s. It is an EXISTS question per user, asked in a form that has to
   visit every scored open row and then dedupe.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job, JobSource

_U = "hotspot-user"


def _job(**kw):
    base = dict(user_id=_U, source=JobSource.GREENHOUSE, external_id="HS-1",
                company="Patrique Mercier", title="French Speaking Consultant",
                url="https://example.test/hs/1", description="d")
    base.update(kw)
    return Job(**base)


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            for j in s.exec(select(Job).where(Job.user_id.like("hotspot-%"))).all():
                s.delete(j)
            s.commit()
    _wipe()
    yield
    _wipe()


# ── 1. the losing side of an insert race must not raise ──────────────────────

def test_a_racing_duplicate_insert_returns_none_instead_of_raising():
    """THE fix. A collision must resolve server-side via ON CONFLICT, because an
    exception here is what produced 6,099 Postgres ERROR lines a day."""
    from app.discovery.pipeline import _insert_job_returning_id

    with get_session() as s:
        first = _insert_job_returning_id(s, _job())
        s.commit()
    assert first is not None, "the first insert must actually insert"

    # The racing worker: same key, separate transaction, no prior SELECT.
    with get_session() as s:
        second = _insert_job_returning_id(s, _job())
        s.commit()
    assert second is None, (
        "a duplicate insert raised or inserted instead of quietly losing the "
        "race — this is the 23505 flood in the Postgres log")


def test_the_race_leaves_exactly_one_row():
    from app.discovery.pipeline import _insert_job_returning_id
    for _ in range(3):
        with get_session() as s:
            _insert_job_returning_id(s, _job())
            s.commit()
    with get_session() as s:
        rows = s.exec(select(Job.id).where(
            Job.user_id == _U, Job.external_id == "HS-1")).all()
    assert len(rows) == 1, f"expected one row, got {len(rows)}"


def test_distinct_keys_still_insert_normally():
    """The conflict clause must not swallow legitimate new postings."""
    from app.discovery.pipeline import _insert_job_returning_id
    ids = []
    for n in range(3):
        with get_session() as s:
            ids.append(_insert_job_returning_id(
                s, _job(external_id=f"HS-{n}", url=f"https://example.test/hs/{n}")))
            s.commit()
    assert all(i is not None for i in ids), ids
    assert len(set(ids)) == 3


def test_the_returned_id_is_the_real_row_id():
    """The caller records funnel events against it, so a wrong id is silent
    corruption of the analytics."""
    from app.discovery.pipeline import _insert_job_returning_id
    with get_session() as s:
        new_id = _insert_job_returning_id(s, _job())
        s.commit()
    with get_session() as s:
        row = s.get(Job, new_id)
    assert row is not None and row.external_id == "HS-1"


def test_the_none_return_is_handled_at_the_caller_not_just_produced():
    """`None` on a lost race is now load-bearing in a way the exception was not.

    An exception could not be ignored; a quiet `None` can. This drives the real
    caller — _upsert — rather than the helper, so the guard cannot be dropped
    without a test failing: re-upserting an identical batch must return 0 new
    rows, raise nothing, and leave the row count unchanged.
    """
    from app.discovery.pipeline import RawJob, _upsert

    batch = [RawJob(source="greenhouse", external_id="HS-caller",
                    company="Patrique Mercier", title="Backend Engineer",
                    location="Remote", remote=True,
                    url="https://example.test/hs/caller",
                    description="Python and Postgres. " * 20, posted_at=None)]

    first = _upsert(batch, user_id=_U)
    assert first == 1, f"first upsert should insert one row, got {first}"

    second = _upsert(batch, user_id=_U)          # must not raise
    assert second == 0, (
        f"a re-upsert reported {second} new rows — the None path is being counted "
        f"as an insert")

    with get_session() as s:
        rows = s.exec(select(Job.id).where(
            Job.user_id == _U, Job.external_id == "HS-caller")).all()
    assert len(rows) == 1


def test_upsert_returns_a_count_not_a_row():
    """Eight call sites do `n += _upsert(...)`. If it ever returns a model or
    None instead of an int, all eight break at once."""
    from app.discovery.pipeline import _upsert
    assert isinstance(_upsert([], user_id=_U), int)


# ── 2. the EXISTS-per-user rewrite must mean the same thing ──────────────────

def _old_form(session, users):
    """The query that was replaced — kept here as the equivalence oracle."""
    return {r[0] if isinstance(r, tuple) else r for r in session.exec(
        select(Job.user_id).where(
            Job.rerank_score.is_not(None),
            Job.is_closed == False,   # noqa: E712
            Job.user_id.in_(users),
        ).distinct()
    ).all()}


def _new_form(session, users):
    out = set()
    for u in users:
        if not u:
            continue
        hit = session.exec(
            select(Job.id).where(
                Job.user_id == u,
                Job.is_closed == False,   # noqa: E712
                Job.rerank_score.is_not(None),
            ).limit(1)
        ).first()
        if hit is not None:
            out.add(u)
    return out


@pytest.fixture
def seeded():
    """u1 scored+open · u2 unscored only · u3 scored but CLOSED · u4 no jobs."""
    rows = [("hotspot-u1", 70.0, False), ("hotspot-u1", None, False),
            ("hotspot-u2", None, False), ("hotspot-u3", 90.0, True)]
    with get_session() as s:
        for i, (uid, score, closed) in enumerate(rows):
            s.add(_job(user_id=uid, external_id=f"eq{i}",
                       url=f"https://example.test/eq{i}",
                       rerank_score=score, is_closed=closed))
        s.commit()
    return ["hotspot-u1", "hotspot-u2", "hotspot-u3", "hotspot-u4"]


def test_the_rewrite_returns_exactly_what_the_distinct_scan_returned(seeded):
    with get_session() as s:
        assert _new_form(s, seeded) == _old_form(s, seeded)


def test_the_rewrite_gets_the_semantics_right(seeded):
    """Equivalence to a wrong query would be worthless — assert the meaning too:
    only a user with at least one scored, still-OPEN job counts."""
    with get_session() as s:
        assert _new_form(s, seeded) == {"hotspot-u1"}


def test_the_lane_still_orders_new_users_first(seeded):
    """The whole point of the query: users with no scored job yet are staring at
    a blank dashboard and must sort ahead of everyone else."""
    with get_session() as s:
        has_scored = _new_form(s, seeded)
    ordered = sorted(seeded, key=lambda u: (u in has_scored,))
    assert ordered[-1] == "hotspot-u1", (
        f"the only user with scored jobs must sort last, got {ordered}")
