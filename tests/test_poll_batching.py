"""Batched poll records must leave the board in the SAME state as the old path.

The tick used to call ``_mark_polled`` once per processed board — SELECT,
mutate, COMMIT — serially in the main thread. Production measured ~481ms of
post-fetch DB work per board against a 519ms p50 FETCH, and the deferral split
came back ``0 never-started / 0 running / 1,586 unprocessed``: every fetch had
completed and the consumer could not keep up. Fetch concurrency was never the
constraint.

``_flush_polls`` replaces that with a bulk UPDATE keyed by primary key. A
throughput change is only safe if the resulting row is indistinguishable from
what the old path wrote, so the central test here runs both and diffs every
column. The rest pin the properties that make the batch safe: it addresses rows
by their own id, it never touches a board that is not in the batch, and it never
takes the failure path's semantics with it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource
from app.strategy.hot_lane import _mark_polled
from app.strategy.pulse_lane import _POLL_FLUSH_BATCH, _flush_polls

_PREFIX = "pb-"

# The columns a successful poll is allowed to touch, plus the ones it must not.
_ALL_COLUMNS = (
    "slug", "ats", "company_name", "career_url", "source", "confidence_score",
    "target_fit_score", "first_seen", "last_seen", "last_validated_at",
    "is_active", "job_count", "failure_count", "new_jobs_last_poll",
    "last_new_job_at", "sponsorship_signal", "last_error", "inactive_reason",
    "next_retry_at", "next_poll_at", "poll_hash",
)


def _cleanup():
    with get_session() as s:
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_PREFIX}%")))
        s.commit()


def _board(slug, **kw):
    defaults = dict(
        ats=JobSource.GREENHOUSE, company_name=slug, is_active=True,
        job_count=3, failure_count=2, poll_hash="sig-old",
        new_jobs_last_poll=0, last_new_job_at=None,
        last_seen=datetime.utcnow() - timedelta(hours=9),
        next_poll_at=datetime.utcnow() - timedelta(hours=1),
    )
    defaults.update(kw)
    with get_session() as s:
        row = CompanyRegistry(slug=_PREFIX + slug, **defaults)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _snapshot(bid) -> dict:
    with get_session() as s:
        row = s.exec(select(CompanyRegistry).where(CompanyRegistry.id == bid)).one()
        return {c: getattr(row, c) for c in _ALL_COLUMNS}


# Identity columns differ between the two fixtures by construction.
_IDENTITY = {"slug", "company_name"}


def _close(a, b, *, tol_seconds=5, ignore=frozenset()):
    """Compare two snapshots, allowing tiny clock drift on the timestamps the
    two paths each stamp with their own utcnow()."""
    diffs = {}
    for k in _ALL_COLUMNS:
        if k in ignore:
            continue
        va, vb = a[k], b[k]
        if isinstance(va, datetime) and isinstance(vb, datetime):
            if abs((va - vb).total_seconds()) > tol_seconds:
                diffs[k] = (va, vb)
        elif va != vb:
            diffs[k] = (va, vb)
    return diffs


# ── 1. Successful boards retain correct scheduling after batching ────────────

def test_batched_write_matches_the_old_per_board_write_exactly():
    """The load-bearing test: same input, both paths, diff every column."""
    init_db()
    _cleanup()
    nxt = datetime.utcnow() + timedelta(minutes=60)

    old_id = _board("old-path")
    with get_session() as s:
        old = s.exec(select(CompanyRegistry).where(
            CompanyRegistry.id == old_id)).one()
    _mark_polled(old.slug, old.ats, job_count=12, ok=True, new_jobs=0,
                 next_poll_at=nxt, poll_hash="sig-new")

    new_id = _board("new-path")
    _flush_polls([{"id": new_id, "job_count": 12, "new_jobs": 0,
                   "next_poll_at": nxt, "poll_hash": "sig-new"}])

    diffs = _close(_snapshot(old_id), _snapshot(new_id), ignore=_IDENTITY)
    assert not diffs, f"batched path diverged from the old path: {diffs}"
    _cleanup()


def test_batched_write_matches_the_old_path_when_the_board_yielded():
    """The branch that moves last_new_job_at — which is what promotes a board to
    the fast lane, so a divergence here would quietly change every future
    cadence decision."""
    init_db()
    _cleanup()
    nxt = datetime.utcnow() + timedelta(minutes=5)

    old_id = _board("old-yield")
    with get_session() as s:
        old = s.exec(select(CompanyRegistry).where(
            CompanyRegistry.id == old_id)).one()
    _mark_polled(old.slug, old.ats, job_count=20, ok=True, new_jobs=4,
                 next_poll_at=nxt, poll_hash="sig-y")

    new_id = _board("new-yield")
    _flush_polls([{"id": new_id, "job_count": 20, "new_jobs": 4,
                   "next_poll_at": nxt, "poll_hash": "sig-y"}])

    diffs = _close(_snapshot(old_id), _snapshot(new_id), ignore=_IDENTITY)
    assert not diffs, f"yield branch diverged: {diffs}"

    got = _snapshot(new_id)
    assert got["last_new_job_at"] is not None
    assert got["new_jobs_last_poll"] == 4
    assert got["failure_count"] == 0, "a good poll must clear the failure count"
    _cleanup()


def test_a_board_without_new_jobs_keeps_its_previous_last_new_job_at():
    """The reason the batch is split in two. Writing a stale value back for
    every board would reset yield history and demote boards off the fast lane."""
    init_db()
    _cleanup()
    posted_before = datetime.utcnow() - timedelta(days=2)
    bid = _board("keeps-yield", last_new_job_at=posted_before)

    _flush_polls([{"id": bid, "job_count": 7, "new_jobs": 0,
                   "next_poll_at": datetime.utcnow() + timedelta(minutes=60),
                   "poll_hash": "sig"}])

    got = _snapshot(bid)
    assert got["last_new_job_at"] is not None
    assert abs((got["last_new_job_at"] - posted_before).total_seconds()) < 5, (
        "a board with no new jobs had its yield history rewritten")
    _cleanup()


# ── 4. A batch cannot touch a board that is not in it ────────────────────────

def test_a_batch_only_updates_the_rows_it_names():
    """Bulk UPDATE by primary key — a board can only ever update itself. A
    slug/ats-keyed write could collide; an id-keyed one cannot."""
    init_db()
    _cleanup()
    target = _board("target")
    bystander = _board("bystander", poll_hash="untouched",
                       job_count=99, failure_count=7)
    before = _snapshot(bystander)

    _flush_polls([{"id": target, "job_count": 1, "new_jobs": 0,
                   "next_poll_at": datetime.utcnow() + timedelta(minutes=60),
                   "poll_hash": "sig-target"}])

    after = _snapshot(bystander)
    assert not _close(before, after), "a board outside the batch was modified"
    assert _snapshot(target)["poll_hash"] == "sig-target"
    _cleanup()


def test_a_mixed_batch_writes_every_row_correctly():
    """Yield and no-yield rows in ONE call must each get their own values, not
    the first row's."""
    init_db()
    _cleanup()
    a = _board("mix-a", last_new_job_at=None)
    b = _board("mix-b", last_new_job_at=None)
    c = _board("mix-c", last_new_job_at=None)
    nxt = datetime.utcnow() + timedelta(minutes=30)

    assert _flush_polls([
        {"id": a, "job_count": 1, "new_jobs": 0, "next_poll_at": nxt, "poll_hash": "A"},
        {"id": b, "job_count": 2, "new_jobs": 5, "next_poll_at": nxt, "poll_hash": "B"},
        {"id": c, "job_count": 3, "new_jobs": 0, "next_poll_at": nxt, "poll_hash": "C"},
    ]) == 3

    sa, sb, sc = _snapshot(a), _snapshot(b), _snapshot(c)
    assert (sa["job_count"], sa["poll_hash"], sa["last_new_job_at"]) == (1, "A", None)
    assert (sb["job_count"], sb["poll_hash"]) == (2, "B")
    assert sb["last_new_job_at"] is not None, "the yielding row lost its promotion"
    assert (sc["job_count"], sc["poll_hash"], sc["last_new_job_at"]) == (3, "C", None)
    _cleanup()


def test_empty_and_idless_batches_are_no_ops():
    init_db()
    _cleanup()
    assert _flush_polls([]) == 0
    assert _flush_polls([{"id": None, "job_count": 1, "new_jobs": 0,
                          "next_poll_at": datetime.utcnow(), "poll_hash": "x"}]) == 0
    _cleanup()


# ── 2/3. Failures and deferrals keep their own paths ─────────────────────────

def test_batching_did_not_take_the_failure_path_with_it():
    """Failures still read-modify-write: the counter increments, backoff is
    derived from it, and retirement still fires. Those semantics depend on the
    CURRENT counter, which is exactly why they stay off the batch path."""
    init_db()
    _cleanup()
    bid = _board("fails", failure_count=0)
    with get_session() as s:
        row = s.exec(select(CompanyRegistry).where(CompanyRegistry.id == bid)).one()

    delays = []
    for _ in range(3):
        _mark_polled(row.slug, row.ats, job_count=None, ok=False,
                     error="connection reset",
                     failure_backoff_minutes=15, failure_backoff_cap_hours=24)
        got = _snapshot(bid)
        delays.append((got["next_poll_at"] - datetime.utcnow()).total_seconds())

    assert _snapshot(bid)["failure_count"] == 3
    assert delays[0] < delays[1] < delays[2], "exponential backoff was lost"
    _cleanup()


def test_a_deferred_board_is_still_not_a_poll_record():
    """The previous fix must survive this one: deferral moves next_poll_at and
    nothing else, and must never be routed through the poll-record batch."""
    from app.strategy.pulse_lane import _defer_boards
    init_db()
    _cleanup()
    seen = datetime.utcnow() - timedelta(hours=9)
    bid = _board("deferred", last_seen=seen, poll_hash="sig-old", job_count=7,
                 failure_count=2)

    with get_session() as s:
        board = s.exec(select(CompanyRegistry).where(
            CompanyRegistry.id == bid)).one()
    assert _defer_boards([board]) == 1

    got = _snapshot(bid)
    assert abs((got["last_seen"] - seen).total_seconds()) < 5, (
        "a deferred board was stamped as polled")
    assert got["poll_hash"] == "sig-old"
    assert got["job_count"] == 7
    assert got["failure_count"] == 2
    _cleanup()


def test_flush_batch_size_is_bounded():
    """A tick that dies mid-way must lose at most one batch, and the records it
    holds must stay flat in memory."""
    assert 1 <= _POLL_FLUSH_BATCH <= 500
