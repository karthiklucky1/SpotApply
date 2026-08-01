"""A 429 means "slow down", not "this board is dead".

Found 2026-08-01 in the Railway logs: `Workday fetch failed for embryriddle:
HTTP 429`, `dowjones` twice, `draftkings`. Upstream ATS rate-limiting, triggered
by the pulse lane's polling cadence — not a fault in those boards.

But record_board_failure counted every non-404 error toward
BOARD_DEACTIVATE_AFTER_FAILURES, so five throttled polls in a row set
`is_active = False` with reason "unreachable x5". That silently and permanently
removes a live company from discovery, shrinking job coverage a little at a time,
with nothing to alert on — the board simply stops appearing.

Throttling now backs the board off via next_poll_at and leaves failure_count
untouched. Genuine faults must still retire, so both directions are pinned.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import CompanyRegistry, JobSource
from app.discovery.pipeline import (BOARD_DEACTIVATE_AFTER_FAILURES,
                                    record_board_failure,
                                    record_board_failures_bulk)

_SLUG = "throttle-probe"


def _seed(slug=_SLUG):
    with get_session() as s:
        for r in s.exec(select(CompanyRegistry).where(
                CompanyRegistry.slug == slug)).all():
            s.delete(r)
        s.add(CompanyRegistry(slug=slug, ats=JobSource.WORKDAY, company_name=slug))
        s.commit()


def _state(slug=_SLUG):
    with get_session() as s:
        r = s.exec(select(CompanyRegistry).where(
            CompanyRegistry.slug == slug)).first()
        return r.is_active, r.failure_count, r.next_poll_at


@pytest.fixture(autouse=True)
def _clean():
    yield
    with get_session() as s:
        for r in s.exec(select(CompanyRegistry).where(
                CompanyRegistry.slug.like("throttle-probe%"))).all():
            s.delete(r)
        s.commit()


# ── throttled boards survive ─────────────────────────────────────────────────

@pytest.mark.parametrize("err", [
    "Workday fetch failed for dowjones: HTTP 429",          # the real log line
    "HTTP 429 Too Many Requests",
    "Client error '429 Too Many Requests' for url ...",
    "rate limit exceeded",
])
def test_repeated_throttling_never_retires_a_board(err):
    _seed()
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES + 1):
        record_board_failure("workday", _SLUG, err)
    active, failures, next_poll = _state()
    assert active is True, (
        "a throttled board was deactivated — that permanently drops a live "
        "company from discovery")
    assert failures == 0, (
        f"failure_count reached {failures}; throttling must not accumulate "
        f"toward the retirement threshold")
    assert next_poll is not None, "the board should have been backed off"


def test_the_bulk_path_applies_the_same_policy():
    """record_board_failures_bulk is a separate implementation of the same rules,
    and it is the one the concurrent fetch path actually calls."""
    _seed()
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES + 1):
        record_board_failures_bulk([(_SLUG, "workday", "HTTP 429")])
    active, failures, next_poll = _state()
    assert active is True and failures == 0 and next_poll is not None


def test_the_backoff_is_in_the_future():
    from datetime import datetime
    _seed()
    record_board_failure("workday", _SLUG, "HTTP 429")
    _, _, next_poll = _state()
    assert next_poll > datetime.utcnow(), (
        "next_poll_at must be in the future or the board is polled again "
        "immediately and re-throttled")


def test_the_error_is_still_recorded_for_diagnosis():
    _seed()
    record_board_failure("workday", _SLUG, "HTTP 429 Too Many Requests")
    with get_session() as s:
        row = s.exec(select(CompanyRegistry).where(
            CompanyRegistry.slug == _SLUG)).first()
    assert row.last_error and "429" in row.last_error, (
        "backing off must not throw away the reason")


# ── genuinely broken boards must still retire ────────────────────────────────

@pytest.mark.parametrize("err", ["HTTP 503", "connection timeout", "HTTP 500"])
def test_a_real_fault_still_retires_after_the_threshold(err):
    _seed()
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES + 1):
        record_board_failure("workday", _SLUG, err)
    active, failures, _ = _state()
    assert active is False and failures >= BOARD_DEACTIVATE_AFTER_FAILURES, (
        "the throttle exemption must not have disabled retirement entirely")


def test_a_404_still_retires_immediately():
    """A 404 means the board moved or was renamed — it will 404 forever."""
    _seed()
    record_board_failure("workday", _SLUG, "HTTP 404")
    active, _, _ = _state()
    assert active is False


def test_a_fault_after_throttling_still_counts():
    """Backing off must not reset or mask a board that then genuinely breaks."""
    _seed()
    for _ in range(3):
        record_board_failure("workday", _SLUG, "HTTP 429")
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES):
        record_board_failure("workday", _SLUG, "HTTP 503")
    active, failures, _ = _state()
    assert active is False and failures >= BOARD_DEACTIVATE_AFTER_FAILURES
