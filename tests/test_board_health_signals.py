"""A board that is throttled, empty, or gone are three different things.

The adapters collapsed all three into ``return []``, and the lanes collapsed
them again into "we polled it and it has no jobs". Two consequences, both
measured in production over 2026-09-05/11:

  * ~55 distinct Workday employers per hour and ~179 join.com polls per hour
    returned HTTP 429. In the FULL lane those are spared from retirement
    (pipeline.record_board_failure has checked _is_throttled since 09-02); in
    the pulse lane — which does ~288k polls a day — they counted toward
    BOARD_DEACTIVATE_AFTER_FAILURES, so five busy afternoons retired a live
    employer as "unreachable x5".
  * an error-empty fetch wrote job_count=0, which is the value _cadence uses to
    put a board on the 72-hour zero-yield tier.

Rows are prefixed ``bh_`` and removed by that prefix.
"""
from __future__ import annotations

import httpx
import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import CompanyRegistry
from app.discovery.base import fetch_error
from app.discovery.pipeline import BOARD_DEACTIVATE_AFTER_FAILURES
from app.strategy.hot_lane import _mark_polled

_P = "bh_"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_P}%")))
            s.commit()
    _wipe()
    yield
    _wipe()


def _board(slug: str, **kw) -> CompanyRegistry:
    with get_session() as s:
        row = CompanyRegistry(slug=_P + slug, ats="greenhouse",
                              company_name=slug, is_active=True, **kw)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def _read(slug: str) -> CompanyRegistry:
    with get_session() as s:
        return s.exec(select(CompanyRegistry).where(
            CompanyRegistry.slug == _P + slug)).first()


# ── The adapters say WHY they came back empty ────────────────────────────────

def test_an_adapter_reports_the_http_error_it_swallowed(monkeypatch):
    from app.discovery.greenhouse import GreenhouseScraper

    def _boom(*a, **k):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "get", _boom)
    sc = GreenhouseScraper("anthropic")
    assert sc.fetch() == []
    assert fetch_error(sc), "an error-empty fetch is indistinguishable from an empty board"
    assert "ConnectTimeout" in fetch_error(sc)


def test_lever_and_ashby_report_it_too(monkeypatch):
    from app.discovery.ashby import AshbyScraper
    from app.discovery.lever import LeverScraper

    def _boom(*a, **k):
        raise httpx.HTTPStatusError("429", request=None, response=None)

    monkeypatch.setattr(httpx, "get", _boom)
    for sc in (LeverScraper("acme"), AshbyScraper("acme")):
        assert sc.fetch() == []
        assert fetch_error(sc)


def test_an_adapter_with_no_such_attribute_is_treated_as_a_real_empty_board():
    class Old:
        name = "old"

        def fetch(self):
            return []

    assert fetch_error(Old()) == "", "adapters that raise on failure must not be second-guessed"


# ── A rate limit is not a death certificate ──────────────────────────────────

def test_a_rate_limit_never_retires_a_board():
    _board("throttled", failure_count=BOARD_DEACTIVATE_AFTER_FAILURES - 1)
    _mark_polled(_P + "throttled", "greenhouse", job_count=None, ok=False,
                 error="HTTPStatusError: 429 Too Many Requests")
    row = _read("throttled")
    assert row.is_active, "a throttled board was retired as unreachable"
    assert row.failure_count == BOARD_DEACTIVATE_AFTER_FAILURES - 1, (
        "429s accumulated toward the retirement threshold")
    assert row.next_poll_at is not None, "a throttled board must be backed off"


def test_a_real_failure_still_decays_a_board():
    _board("broken", failure_count=BOARD_DEACTIVATE_AFTER_FAILURES - 1)
    _mark_polled(_P + "broken", "greenhouse", job_count=None, ok=False,
                 error="HTTPStatusError: 500 Internal Server Error")
    row = _read("broken")
    assert not row.is_active, "a board that answers 500 forever is unusable"


def test_a_404_still_retires_immediately():
    _board("gone")
    _mark_polled(_P + "gone", "greenhouse", job_count=None, ok=False,
                 error="HTTPStatusError: 404 Not Found")
    assert not _read("gone").is_active


def test_a_throttled_poll_does_not_zero_the_job_count():
    """job_count is what _cadence reads to decide the 72-hour zero-yield tier,
    so a throttled poll must leave it alone."""
    _board("busy", job_count=42)
    _mark_polled(_P + "busy", "greenhouse", job_count=None, ok=False,
                 error="429 rate limit")
    assert _read("busy").job_count == 42
