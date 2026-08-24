"""Raising the zero-yield cadence must not touch a productive board.

59.2% of the active registry (31,219 of 52,698) holds zero jobs and was being
re-polled daily to re-confirm emptiness, consuming ~59.9% of the lane's real
completed-fetch capacity while live boards sat at an ~8.7h effective revisit
against a 60-minute promise. Raising that cadence to 72h is the cheapest
capacity available.

The risk is not the number — it is BLAST RADIUS. ``pulse_dead_interval_hours``
was doing two jobs: the zero-yield cadence AND the ceiling on the exponential
backoff a board gets after a real fetch failure. Raising it would therefore
have tripled the failure ceiling for every board, productive ones included —
a live-board cadence change smuggled in behind a dead-board setting. The two
are now separate knobs, and these tests hold them apart.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource
from app.strategy.pulse_lane import _cadence

_PREFIX = "zy-"


def _cleanup():
    with get_session() as s:
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_PREFIX}%")))
        s.commit()


def _row(slug, *, job_count=0, last_new_job_at=None, company_name=None):
    return CompanyRegistry(
        slug=_PREFIX + slug, ats=JobSource.GREENHOUSE,
        company_name=company_name or slug, is_active=True,
        job_count=job_count, last_new_job_at=last_new_job_at)


# ── the cadence branch is reachable only by zero-yield boards ────────────────

def test_only_a_zero_yield_board_gets_the_dead_cadence():
    """Every branch of _cadence, so the blast radius is explicit rather than
    assumed. A board with even ONE job never reaches the dead branch."""
    now = datetime.utcnow()
    terms: set = set()
    dead = timedelta(hours=settings.pulse_dead_interval_hours)
    floor = timedelta(minutes=settings.pulse_floor_interval_minutes)
    fast = timedelta(minutes=settings.pulse_fast_interval_minutes)

    assert _cadence(_row("zero", job_count=0), terms, now) == dead
    assert _cadence(_row("zero-null", job_count=None), terms, now) == dead

    # PRODUCTIVE — must be unaffected by the dead cadence entirely.
    assert _cadence(_row("one-job", job_count=1), terms, now) == floor
    assert _cadence(_row("many", job_count=500), terms, now) == floor

    # Recently posted -> fast lane, even at job_count == 0 (a board that just
    # produced its first posting must not be demoted to the slow cadence).
    assert _cadence(_row("just-posted", job_count=0, last_new_job_at=now),
                    terms, now) == fast

    # Watched -> fast lane regardless of yield.
    assert _cadence(_row("watched", job_count=0), {"zywatched"}, now) == fast


def test_a_zero_yield_board_that_posts_returns_to_the_fast_lane():
    """The safety valve that makes 72h acceptable: quiet is not dead. The
    moment a board produces anything, last_new_job_at moves and its very next
    cadence is the 5-minute lane."""
    now = datetime.utcnow()
    quiet = _row("wakes", job_count=0)
    assert _cadence(quiet, set(), now) == timedelta(
        hours=settings.pulse_dead_interval_hours)

    quiet.last_new_job_at = now      # what _flush_polls writes on a yield
    quiet.job_count = 3
    assert _cadence(quiet, set(), now) == timedelta(
        minutes=settings.pulse_fast_interval_minutes)


def test_the_dead_cadence_is_the_configured_value():
    now = datetime.utcnow()
    assert _cadence(_row("cfg", job_count=0), set(), now) == timedelta(
        hours=settings.pulse_dead_interval_hours)


# ── the two knobs are genuinely separate ─────────────────────────────────────

def test_the_failure_backoff_cap_is_not_the_zero_yield_cadence():
    """The coupling this commit removes. These were ONE setting, so raising the
    zero-yield cadence tripled the failure ceiling for productive boards too."""
    assert hasattr(settings, "pulse_failure_backoff_cap_hours")
    assert settings.pulse_failure_backoff_cap_hours != settings.pulse_dead_interval_hours, (
        "the failure-backoff cap and the zero-yield cadence are the same value "
        "again — if they are ever re-coupled, changing one silently changes the "
        "other")
    assert settings.pulse_failure_backoff_cap_hours == 24


def test_a_failing_productive_board_keeps_the_24h_ceiling():
    """End to end on the real write path: a productive board that fails must not
    inherit the raised zero-yield cadence as its backoff ceiling."""
    from app.strategy.hot_lane import _mark_polled
    init_db()
    _cleanup()
    with get_session() as s:
        row = _row("productive-failing", job_count=42)
        row.failure_count = 0
        s.add(row)
        s.commit()
        s.refresh(row)
        bid, slug, ats = row.id, row.slug, row.ats

    # A huge base so the cap is what binds, then assert which cap applied.
    _mark_polled(slug, ats, job_count=None, ok=False, error="timeout",
                 failure_backoff_minutes=100000,
                 failure_backoff_cap_hours=settings.pulse_failure_backoff_cap_hours)

    with get_session() as s:
        got = s.exec(select(CompanyRegistry).where(
            CompanyRegistry.id == bid)).one()
    delay_h = (got.next_poll_at - datetime.utcnow()).total_seconds() / 3600.0
    assert delay_h <= 24.05, (
        f"a productive board was backed off {delay_h:.1f}h — it has inherited "
        f"the {settings.pulse_dead_interval_hours}h zero-yield cadence as its "
        f"failure ceiling")
    assert got.job_count == 42, "the failure path rewrote a productive job_count"
    _cleanup()


def test_the_zero_yield_cadence_is_long_enough_to_matter_and_short_enough_to_be_safe():
    """Bounds, with the reasoning attached. Below ~48h the capacity saving is
    marginal; beyond a week a company opening its first req waits too long, and
    at that point retiring the board would be the honest choice instead."""
    assert 48 <= settings.pulse_dead_interval_hours <= 168
    assert settings.pulse_dead_interval_hours > (
        settings.pulse_floor_interval_minutes / 60.0), (
        "zero-yield boards must poll LESS often than productive ones")
