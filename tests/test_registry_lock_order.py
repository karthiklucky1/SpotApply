"""Every multi-row companyregistry write acquires locks in one fixed order.

Postgres takes a row lock per UPDATE and holds it to commit. Two transactions
that touch overlapping registry rows in DIFFERENT orders are each other's
deadlock partner — and production ran 68-99 flush deadlocks per DAY through the
overlapping-consumers era, plus exactly one after the single-consumer fix
(2026-08-29 03:21:14, the victim verifiably `_flush_polls`' without-yield
group). The class of bug is one thing: a multi-row registry transaction whose
lock order is not strictly ascending primary key.

These tests pin the discipline for every batch writer:

  * ``_flush_polls`` — each group ascending, and the two groups in SEPARATE
    transactions (one transaction spanning both is ascending-then-RESTARTING,
    piecewise sorted but not monotonic: the exact shape of the post-fix
    deadlock),
  * ``_defer_boards`` — ascending (covered in test_pulse_deferral, re-pinned
    here for the transaction count),
  * ``record_board_failures_bulk`` — one ordered read, per-shape groups each
    ascending in their own transaction,
  * ``pull_boards_forward`` (the watchlist route's write) — ascending
    executemany by primary key, never ``UPDATE … WHERE id IN (…)`` whose
    in-statement lock order is the executor's scan order,

plus the retry-once-on-40P01 wrapper they all share (all four writers are
idempotent, so a deadlock handed to us by a writer OUTSIDE this discipline —
a deploy-overlap twin, a session outside the app — costs a retry, not a lost
write).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource
from app.strategy import pulse_lane

_PREFIX = "rlo-"


def _board(slug: str, *, job_count: int = 5, failure_count: int = 0,
           is_active: bool = True, company_name: str | None = None,
           ats: JobSource = JobSource.GREENHOUSE) -> int:
    with get_session() as session:
        row = CompanyRegistry(
            slug=_PREFIX + slug, ats=ats, company_name=company_name or (_PREFIX + slug),
            is_active=is_active, job_count=job_count, failure_count=failure_count,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _get(bid: int) -> CompanyRegistry:
    with get_session() as session:
        return session.exec(
            select(CompanyRegistry).where(CompanyRegistry.id == bid)).one()


def _cleanup() -> None:
    with get_session() as session:
        session.exec(
            delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_PREFIX}%")))
        session.commit()


class _TxnSpy:
    """Capture, per transaction (= per get_session context), the id-order of
    every executemany parameter list handed to session.execute."""

    def __init__(self, module):
        self.module = module
        self.txns: list[list[list[int]]] = []
        self._real = module.get_session

    def install(self, monkeypatch):
        spy = self

        class _Session:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, k):
                return getattr(self._inner, k)

            def execute(self, stmt, params=None, *a, **kw):
                if isinstance(params, list):
                    spy.txns[-1].append([p.get("id") for p in params])
                if params is not None:
                    return self._inner.execute(stmt, params, *a, **kw)
                return self._inner.execute(stmt, *a, **kw)

        class _Ctx:
            def __enter__(self):
                spy.txns.append([])
                self._cm = spy._real()
                return _Session(self._cm.__enter__())

            def __exit__(self, *args):
                return self._cm.__exit__(*args)

        monkeypatch.setattr(spy.module, "get_session", _Ctx)
        return self

    @property
    def write_txns(self) -> list[list[list[int]]]:
        return [t for t in self.txns if t]


# ── _flush_polls: each group its own monotonic transaction ───────────────────

def test_flush_groups_are_separate_transactions_each_ascending(monkeypatch):
    """The post-fix production deadlock in one assertion: with-yield and
    without-yield rows in ONE transaction lock ascending, then restart lower at
    the group boundary. Each group must be its own transaction, ascending."""
    init_db()
    _cleanup()
    ids = [_board(f"fl-{i}") for i in range(9)]
    now = datetime.utcnow()
    records = [{"id": bid, "job_count": 3, "new_jobs": (n % 2),
                "poll_hash": f"h{n}", "next_poll_at": now + timedelta(hours=1)}
               for n, bid in enumerate(reversed(ids))]

    spy = _TxnSpy(pulse_lane).install(monkeypatch)
    assert pulse_lane._flush_polls(records, now) == 9

    writes = spy.write_txns
    assert len(writes) == 2, (
        f"expected the yield/no-yield groups in TWO transactions, got "
        f"{len(writes)} — a single transaction spanning both is "
        f"ascending-then-restarting, the post-fix deadlock's exact shape")
    all_ids: list[int] = []
    for txn in writes:
        assert len(txn) == 1, "one executemany per flush transaction"
        assert txn[0] == sorted(txn[0]), f"group not ascending: {txn[0]}"
        all_ids += txn[0]
    assert sorted(all_ids) == sorted(ids), "every record written exactly once"
    _cleanup()


def test_flush_single_group_is_one_transaction(monkeypatch):
    init_db()
    _cleanup()
    ids = [_board(f"fs-{i}") for i in range(4)]
    now = datetime.utcnow()
    records = [{"id": bid, "job_count": 1, "new_jobs": 0, "poll_hash": "h",
                "next_poll_at": now} for bid in reversed(ids)]
    spy = _TxnSpy(pulse_lane).install(monkeypatch)
    assert pulse_lane._flush_polls(records, now) == 4
    assert len(spy.write_txns) == 1
    _cleanup()


# ── record_board_failures_bulk: ordered read, per-shape ordered writes ───────

def test_board_failures_bulk_writes_ascending_per_shape(monkeypatch):
    """Throttle (429), plain failure, and retirement carry different SET
    columns, so they land in different executemany groups — each must be
    ascending, in its own transaction, and the semantics must survive the
    rewrite from mutate-ORM-rows-in-fetch-order (whose UPDATE order was an
    unpinned SQLAlchemy internal)."""
    from app.discovery import pipeline
    init_db()
    _cleanup()
    b_throttle = _board("thr")
    b_fail = _board("fail")
    b_gone = _board("gone")
    b_dying = _board("dying", failure_count=4)  # threshold is 5

    spy = _TxnSpy(pipeline).install(monkeypatch)
    # Hand the failures over in the worst order.
    deactivated = pipeline.record_board_failures_bulk([
        (_PREFIX + "dying", "greenhouse", "connection timeout"),
        (_PREFIX + "gone", "greenhouse", "HTTP 404 Not Found"),
        (_PREFIX + "fail", "greenhouse", "connection reset"),
        (_PREFIX + "thr", "greenhouse", "HTTP 429 Too Many Requests"),
    ])
    assert deactivated == 2  # 404 + fifth strike

    for txn in spy.write_txns:
        for params in txn:
            assert params == sorted(params), f"shape group not ascending: {params}"
    assert len(spy.write_txns) == 3, "throttle/fail/retire each in their own txn"

    thr = _get(b_throttle)
    assert thr.is_active is True and thr.failure_count == 0
    assert thr.next_poll_at is not None, "429 backs off instead of retiring"
    assert "429" in (thr.last_error or "")
    fail = _get(b_fail)
    assert fail.is_active is True and fail.failure_count == 1
    gone = _get(b_gone)
    assert gone.is_active is False and "404" in (gone.inactive_reason or "")
    dying = _get(b_dying)
    assert dying.is_active is False and dying.failure_count == 5
    _cleanup()


def test_board_failures_bulk_skips_inactive_and_unknown(monkeypatch):
    from app.discovery import pipeline
    init_db()
    _cleanup()
    b_off = _board("off", is_active=False)
    assert pipeline.record_board_failures_bulk([
        (_PREFIX + "off", "greenhouse", "HTTP 404"),
        (_PREFIX + "nosuch", "greenhouse", "HTTP 404"),
        (None, "greenhouse", "x"),
        (_PREFIX + "off", "not-a-source", "x"),
    ]) == 0
    row = _get(b_off)
    assert row.last_error is None, "an inactive board must not be touched"
    _cleanup()


# ── pull_boards_forward: the watchlist route's write, disciplined ────────────

def test_pull_boards_forward_is_ascending_executemany(monkeypatch):
    """The route's original in-place version issued un-ordered
    ``UPDATE … WHERE id IN (batch)`` statements — in-statement lock order is
    the executor's SCAN order, aimed at exactly the boards the pulse lane
    polls most. The canonical helper must write per-primary-key, ascending."""
    init_db()
    _cleanup()
    ids = [_board(f"watch-{i}", company_name=f"{_PREFIX}watchco{i}")
           for i in range(6)]
    _board("bystander", company_name="unrelated-co")

    spy = _TxnSpy(pulse_lane).install(monkeypatch)
    touched = pulse_lane.pull_boards_forward({pulse_lane._norm(_PREFIX + "watch")})
    assert touched == 6

    writes = spy.write_txns
    assert len(writes) == 1
    assert writes[0][0] == sorted(writes[0][0])
    assert set(writes[0][0]) == set(ids)

    for bid in ids:
        assert _get(bid).next_poll_at is not None
    _cleanup()


def test_pull_boards_forward_moves_only_next_poll_at():
    init_db()
    _cleanup()
    bid = _board("only-schedule", company_name=_PREFIX + "onlyco")
    before = _get(bid)
    assert pulse_lane.pull_boards_forward({pulse_lane._norm(_PREFIX + "onlyco")}) == 1
    row = _get(bid)
    assert row.next_poll_at is not None
    assert row.last_seen == before.last_seen
    assert row.poll_hash == before.poll_hash
    assert row.failure_count == before.failure_count
    _cleanup()


def test_pull_boards_forward_empty_terms_is_a_noop():
    init_db()
    assert pulse_lane.pull_boards_forward(set()) == 0


# ── the shared deadlock retry ────────────────────────────────────────────────

def _deadlock_error():
    from sqlalchemy.exc import OperationalError

    class _Orig(Exception):
        pgcode = "40P01"

    return OperationalError("stmt", {}, _Orig("deadlock detected"))


def test_deadlock_retry_retries_exactly_once():
    from app.common.db_retry import run_with_deadlock_retry
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise _deadlock_error()
        return "ok"

    assert run_with_deadlock_retry("t", flaky, delay_seconds=0) == "ok"
    assert len(calls) == 2


def test_deadlock_retry_gives_up_after_second_deadlock():
    import pytest
    from sqlalchemy.exc import OperationalError
    from app.common.db_retry import run_with_deadlock_retry

    def always():
        raise _deadlock_error()

    with pytest.raises(OperationalError):
        run_with_deadlock_retry("t", always, delay_seconds=0)


def test_deadlock_retry_does_not_swallow_other_errors():
    import pytest
    from app.common.db_retry import run_with_deadlock_retry
    calls = []

    def broken():
        calls.append(1)
        raise ValueError("not a deadlock")

    with pytest.raises(ValueError):
        run_with_deadlock_retry("t", broken, delay_seconds=0)
    assert len(calls) == 1, "non-deadlock errors must not be retried"
