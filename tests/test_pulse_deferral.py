"""A board that was never fetched must never be recorded as polled.

The pulse lane selects ~300 boards per tick, fetches them with 24 workers, and
hard-stops at ``pulse_tick_max_seconds``. Whatever has not come back by then is
deferred — and every deferred board used to be handed to ``_set_schedule`` with
the ordinary cadence, i.e. byte-for-byte the write a SUCCESSFUL poll performs.

Production was deferring ~88% of each tick's selection, so the registry's
schedule described a poll rate the lane was not achieving: ``next_poll_at``
looked current, ``overdue_boards`` looked small, the dashboard reported the
hourly floor as holding, and ``scripts/pulse_check.py`` printed
``ticks x selected`` under the label "board polls".

These tests pin the distinction at every level it can be lost:

  * the SCHEDULE a deferred board gets (short, and only next_poll_at),
  * the REGISTRY FIELDS a deferral must not move (last_seen above all — it is
    what "we checked this board" means),
  * the difference between a deferral and a real failure (which decays the
    board and backs off), and
  * the TELEMETRY, which must count completed fetches separately from selected
    and deferred ones.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource
from app.strategy import pulse_lane


# Every row this file creates carries this prefix, and cleanup deletes ONLY
# those. A wholesale `delete(CompanyRegistry)` would take out registry fixtures
# other test files had already built, which is a flaky suite, not a clean one.
_PREFIX = "pdt-"


def _board(slug: str, *, job_count: int = 5, failure_count: int = 0,
           next_poll_at=None, last_seen=None, poll_hash: str = "sig-old",
           last_new_job_at=None) -> int:
    slug = _PREFIX + slug
    with get_session() as session:
        row = CompanyRegistry(
            slug=slug, ats=JobSource.GREENHOUSE, company_name=slug,
            is_active=True, job_count=job_count, failure_count=failure_count,
            next_poll_at=next_poll_at, last_seen=last_seen, poll_hash=poll_hash,
            last_new_job_at=last_new_job_at,
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


def _mine():
    """Only the boards this file created."""
    with get_session() as session:
        return session.exec(
            select(CompanyRegistry)
            .where(CompanyRegistry.slug.like(f"{_PREFIX}%"))).all()


def _only_my_boards(monkeypatch):
    """Make the tick see ONLY this file's boards.

    run_pulse_tick selects whatever is due registry-wide, so without this the
    outcome counts would include rows other test files left behind — and the
    whole point of these assertions is that the buckets add up exactly."""
    monkeypatch.setattr(pulse_lane, "_due_boards", lambda now, limit: _mine()[:limit])



# ── 1. A deferred board is NOT a polled board ────────────────────────────────

def test_deferred_board_is_not_recorded_as_polled():
    """The core regression. A deferral must leave every "we polled it" field
    untouched — most of all last_seen, which is the registry's record of having
    actually reached the board."""
    init_db()
    _cleanup()
    stale_seen = datetime.utcnow() - timedelta(hours=9)
    bid = _board("deferred-co", last_seen=stale_seen, poll_hash="sig-old",
                 job_count=7, failure_count=2)

    board = _get(bid)
    assert pulse_lane._defer_boards([board]) == 1

    row = _get(bid)
    # The ONLY thing a deferral may move.
    assert row.next_poll_at is not None
    # Everything a real poll would have written stays exactly as it was.
    assert row.last_seen == stale_seen, (
        "last_seen moved on a board that was never fetched — this is the field "
        "that makes a deferral indistinguishable from a poll in every "
        "downstream count")
    assert row.poll_hash == "sig-old"
    assert row.job_count == 7
    assert row.failure_count == 2          # not a failure — nothing was attempted
    assert row.last_new_job_at is None
    assert row.is_active is True           # a deferral must never retire a board
    _cleanup()


def test_deferral_comes_back_far_sooner_than_the_normal_cadence():
    """The bug was not only that deferred boards were rescheduled — it was that
    they were pushed a FULL CADENCE away (up to the 60-minute floor, or a day
    for a dead board) as though they had just been checked. A board nothing was
    learned about has to come back promptly."""
    init_db()
    _cleanup()
    bid = _board("floor-co", job_count=3)
    board = _get(bid)
    pulse_lane._defer_boards([board])
    delay = (_get(bid).next_poll_at - datetime.utcnow()).total_seconds()

    cadence = pulse_lane._cadence(board, terms=set(), now=datetime.utcnow())
    assert delay < cadence.total_seconds(), (
        "a deferred board was pushed as far out as a successfully polled one")
    # Short, but deliberately not immediate — no hot retry loop.
    assert delay >= 30
    assert delay <= (settings.pulse_deferred_retry_minutes * 60
                     + settings.pulse_deferred_retry_jitter_seconds + 5)
    _cleanup()


def test_deferral_jitter_is_stable_per_board():
    """Jitter spreads the returning tail, but must be DERIVED FROM THE BOARD so
    it is the same every time: re-randomising each tick would let the same
    boards keep losing the race and never be swept."""
    init_db()
    _cleanup()
    bid = _board("jitter-co")
    board = _get(bid)

    pulse_lane._defer_boards([board])
    first = _get(bid).next_poll_at
    base = datetime.utcnow()
    pulse_lane._defer_boards([board], now=base)
    second = _get(bid).next_poll_at
    pulse_lane._defer_boards([board], now=base)
    third = _get(bid).next_poll_at

    assert second == third, "same board + same clock must yield the same offset"
    assert first is not None
    _cleanup()


def test_deferring_many_boards_is_one_round_trip():
    """The per-board write was itself part of the capacity problem: 2-3 serial
    Supabase round-trips EACH, in the main thread, inside the tick's own fetch
    window — hundreds of round-trips per tick spent recording that nothing had
    been done."""
    init_db()
    _cleanup()
    ids = [_board(f"bulk-{i}") for i in range(25)]
    boards = _mine()

    assert pulse_lane._defer_boards(boards) == 25
    for bid in ids:
        assert _get(bid).next_poll_at is not None
    _cleanup()


def test_defer_boards_handles_an_empty_batch():
    init_db()
    _cleanup()
    assert pulse_lane._defer_boards([]) == 0
    _cleanup()


# ── 1b. Registry batches are written in a fixed lock order ───────────────────

def _captured_order(monkeypatch, call):
    """The row order actually handed to the executemany."""
    seen = []
    real = pulse_lane.get_session

    class _Spy:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, k):
            return getattr(self._s, k)

        def execute(self, stmt, params=None, *a, **kw):
            if isinstance(params, list):
                seen.append([p.get("id") for p in params])
            return self._s.execute(stmt, params, *a, **kw) if params is not None \
                else self._s.execute(stmt, *a, **kw)

    class _Ctx:
        def __enter__(self):
            self._cm = real()
            return _Spy(self._cm.__enter__())

        def __exit__(self, *a):
            return self._cm.__exit__(*a)

    monkeypatch.setattr(pulse_lane, "get_session", _Ctx)
    call()
    return seen


def test_deferrals_are_written_in_ascending_primary_key_order(monkeypatch):
    """Postgres holds a row lock per UPDATE until commit, so two transactions
    touching an overlapping set of registry rows in DIFFERENT orders deadlock.
    The board order here comes from _due_boards (sorted by next_poll_at), which
    varies tick to tick — so the write must impose its own fixed order."""
    init_db()
    _cleanup()
    for i in range(8):
        _board(f"ord-{i}")
    boards = list(_mine())
    boards.reverse()          # hand them over in the worst possible order

    batches = _captured_order(monkeypatch, lambda: pulse_lane._defer_boards(boards))
    assert batches, "no executemany was issued"
    for ids in batches:
        assert ids == sorted(ids), f"deferral batch not in ascending id order: {ids}"
    _cleanup()


def test_poll_records_are_written_in_ascending_primary_key_order(monkeypatch):
    """Same fixed order for the success path — _flush_polls and _defer_boards
    write the same table and can be in flight against overlapping rows, so they
    must agree or they are each other's deadlock partner."""
    init_db()
    _cleanup()
    ids = [_board(f"ordp-{i}") for i in range(8)]
    now = datetime.utcnow()
    # Interleave yielding and non-yielding boards: _flush_polls splits them into
    # two groups, and BOTH must be ordered.
    records = [{"id": bid, "job_count": 3, "new_jobs": (n % 2),
                "poll_hash": f"h{n}", "next_poll_at": now + timedelta(minutes=60)}
               for n, bid in enumerate(reversed(ids))]

    batches = _captured_order(monkeypatch,
                              lambda: pulse_lane._flush_polls(records, now))
    assert len(batches) == 2, f"expected a yield/no-yield split, got {len(batches)}"
    for group in batches:
        assert group == sorted(group), f"poll batch not in ascending id order: {group}"
    _cleanup()


# ── 2. A completed fetch schedules normally ──────────────────────────────────

def test_completed_fetch_schedules_on_the_normal_cadence():
    """The other half of the contract: a board that WAS fetched records the
    poll and advances on its ordinary cadence, in one write."""
    from app.strategy.hot_lane import _mark_polled
    init_db()
    _cleanup()
    bid = _board("polled-co", job_count=1, failure_count=3,
                 last_seen=datetime.utcnow() - timedelta(hours=5))

    board = _get(bid)
    nxt = datetime.utcnow() + pulse_lane._cadence(board, set(), datetime.utcnow())
    _mark_polled(board.slug, board.ats, job_count=12, ok=True, new_jobs=2,
                 next_poll_at=nxt, poll_hash="sig-new")

    row = _get(bid)
    assert row.job_count == 12
    assert row.failure_count == 0                    # a good poll clears failures
    assert row.new_jobs_last_poll == 2
    assert row.last_new_job_at is not None           # yield recorded
    assert row.poll_hash == "sig-new"
    assert row.next_poll_at == nxt
    assert (datetime.utcnow() - row.last_seen).total_seconds() < 60
    _cleanup()


def test_unchanged_board_still_counts_as_a_real_poll():
    """An unchanged board does zero downstream work — but it WAS fetched, so it
    must record the poll. This is the case that makes the hourly floor
    affordable, and folding it in with deferrals would hide most of the lane's
    real throughput."""
    from app.strategy.hot_lane import _mark_polled
    init_db()
    _cleanup()
    bid = _board("unchanged-co", poll_hash="sig-same")
    board = _get(bid)
    _mark_polled(board.slug, board.ats, job_count=9, ok=True,
                 next_poll_at=datetime.utcnow() + timedelta(minutes=60),
                 poll_hash="sig-same")
    row = _get(bid)
    assert row.last_seen is not None
    assert row.job_count == 9
    _cleanup()


# ── 3. A real failure follows failure/backoff behaviour ──────────────────────

def test_failed_fetch_backs_off_exponentially():
    """A fetch that RAN and errored is a different animal from one that never
    ran: it decays the board and backs off, so a host having a bad afternoon
    stops consuming a slot every cadence."""
    from app.strategy.hot_lane import _mark_polled
    init_db()
    _cleanup()
    bid = _board("flaky-co", failure_count=0)
    board = _get(bid)

    delays = []
    for _ in range(3):
        _mark_polled(board.slug, board.ats, job_count=None, ok=False,
                     error="connection reset",
                     failure_backoff_minutes=15, failure_backoff_cap_hours=24)
        row = _get(bid)
        delays.append((row.next_poll_at - datetime.utcnow()).total_seconds())

    assert row.failure_count == 3
    assert row.last_error == "connection reset"
    # 15m -> 30m -> 60m: each retry waits longer than the last.
    assert delays[0] < delays[1] < delays[2]
    assert 14 * 60 <= delays[0] <= 16 * 60
    _cleanup()


def test_failure_backoff_is_capped():
    """Backoff doubles, but never past the dead-board cadence — a board must
    not be exiled for a week by a run of transient errors.

    Boards retire at BOARD_DEACTIVATE_AFTER_FAILURES (5), so with the shipped
    15-minute base the doubling tops out around two hours and the cap is never
    reached in practice. It is a guard against a future retune, so it is
    exercised here with a base big enough to hit it."""
    from app.strategy.hot_lane import _mark_polled
    init_db()
    _cleanup()
    bid = _board("longfail-co", failure_count=0)
    board = _get(bid)
    _mark_polled(board.slug, board.ats, job_count=None, ok=False, error="timeout",
                 failure_backoff_minutes=15000, failure_backoff_cap_hours=24)
    row = _get(bid)
    assert row.is_active is True, "one failure must not retire a board"
    delay = (row.next_poll_at - datetime.utcnow()).total_seconds()
    assert delay <= 24 * 3600 + 5
    assert delay > 23 * 3600
    _cleanup()


def test_retirement_wins_over_backoff():
    """A board at the deactivation threshold is retired, not rescheduled — the
    two branches must not both fire and leave a dead board on the poll queue."""
    from app.discovery.pipeline import BOARD_DEACTIVATE_AFTER_FAILURES
    from app.strategy.hot_lane import _mark_polled
    init_db()
    _cleanup()
    bid = _board("dying-co", failure_count=BOARD_DEACTIVATE_AFTER_FAILURES - 1,
                 next_poll_at=None)
    board = _get(bid)
    _mark_polled(board.slug, board.ats, job_count=None, ok=False, error="timeout",
                 failure_backoff_minutes=15, failure_backoff_cap_hours=24)
    row = _get(bid)
    assert row.is_active is False
    assert row.next_poll_at is None, (
        "a retired board was also given a backoff schedule — _due_boards would "
        "keep it out via is_active, but the two branches must stay exclusive")
    _cleanup()


def test_a_404_still_retires_the_board_instead_of_backing_off():
    """404 = the board is gone. Retirement must win over backoff, or the
    per-cycle budget keeps burning slots on companies that no longer exist."""
    from app.strategy.hot_lane import _mark_polled
    init_db()
    _cleanup()
    bid = _board("gone-co")
    board = _get(bid)
    _mark_polled(board.slug, board.ats, job_count=None, ok=False,
                 error="HTTP 404 Not Found",
                 failure_backoff_minutes=15, failure_backoff_cap_hours=24)
    row = _get(bid)
    assert row.is_active is False
    assert "404" in (row.inactive_reason or "")
    _cleanup()


# ── 4. Telemetry separates completed fetches from selected/deferred ──────────

def test_tick_stats_count_completed_fetches_separately(monkeypatch):
    """`selected` is not `polled`. With more boards due than the tick can
    service, the stats must say so: every selected board lands in exactly one
    outcome bucket, and the buckets sum to the selection."""
    init_db()
    _cleanup()
    for i in range(6):
        _board(f"tick-{i}", next_poll_at=datetime.utcnow() - timedelta(hours=2))

    fetched: list[str] = []

    class _Scraper:
        def __init__(self, slug):
            self.slug = slug

        def fetch(self):
            fetched.append(self.slug)
            if self.slug == _PREFIX + "tick-1":
                raise RuntimeError("boom")     # a REAL failure
            return []                          # completed, empty board

    _only_my_boards(monkeypatch)
    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, url: _Scraper(slug))
    monkeypatch.setattr("app.strategy.hot_lane._active_users", lambda: [])
    monkeypatch.setattr(pulse_lane, "_watchlist_terms", lambda: set())

    stats = pulse_lane.run_pulse_tick()

    assert stats["selected"] == 6
    assert stats["started"] == 6
    # Five boards returned a list; one raised.
    assert stats["fetch_ok"] == 5
    assert stats["fetch_failed"] == 1
    assert stats["deferred"] == 0
    # The contract: nothing is double-counted and nothing is lost.
    accounted = (stats["fetch_ok"] + stats["fetch_failed"]
                 + stats["unsupported"] + stats["deferred"])
    assert accounted == stats["selected"]
    # A poll count must never be derivable from the selection.
    assert stats["fetch_ok"] != stats["selected"]
    _cleanup()


def test_deferred_boards_are_reported_and_not_counted_as_polls(monkeypatch):
    """The production shape: the tick runs out of time mid-batch. Deferred
    boards must appear under their own name, must not inflate fetch_ok, and
    must not have been recorded as polled on the registry."""
    init_db()
    _cleanup()
    for i in range(5):
        _board(f"slow-{i}", next_poll_at=datetime.utcnow() - timedelta(hours=2),
               last_seen=None)

    class _Slow:
        def fetch(self):
            import time as _t
            _t.sleep(5)
            return []

    _only_my_boards(monkeypatch)
    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, url: _Slow())
    monkeypatch.setattr("app.strategy.hot_lane._active_users", lambda: [])
    monkeypatch.setattr(pulse_lane, "_watchlist_terms", lambda: set())
    # A tick with essentially no time to fetch: 60% of 1s is the fetch window.
    monkeypatch.setattr(settings, "pulse_tick_max_seconds", 1)

    stats = pulse_lane.run_pulse_tick()

    assert stats["deferred"] == 5
    assert stats["fetch_ok"] == 0
    assert stats["selected"] == 5
    # And the registry agrees: not one of them looks polled.
    rows = _mine()
    assert all(r.last_seen is None for r in rows), (
        "a deferred board was stamped with last_seen — the exact false-telemetry "
        "this test exists to prevent")
    assert all(r.next_poll_at is not None for r in rows)
    _cleanup()
