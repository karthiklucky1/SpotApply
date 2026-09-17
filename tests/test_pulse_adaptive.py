"""The pulse lane's board cap follows what the tick actually finished.

A fixed ``pulse_max_boards_per_tick = 300`` assumed the tick could consume 300
boards. Production 2026-09-14..16 said otherwise: deferred boards rose from
1-4K/day to 20,367 / 23,753 / 40,950, ticks/day fell 1,100 → 537, consumer
deadline hits went from 2-17/day to 119-241/day, the latest tick selected 300
and deferred 231, one tick took 159s against a 60s interval, and 58% of active
boards (28,293) were past ``next_poll_at`` — on a Supabase instance whose Disk
IO budget was nearly exhausted. Every selected-then-deferred board was still an
HTTP fetch (~230 wasted per tick) and the fetches that landed still paid their
DB writes before the deadline cut the consumer off. Over-selecting made the DB
slower, which made the tick finish less, which deferred more.

AIMD breaks that loop with one number: HALVE the cap after a tick that mostly
failed (deferred share above ``pulse_adaptive_defer_pct`` or the consumer hit
its deadline), floored at ``pulse_min_boards_per_tick``; grow it 25%+1 after a
tick that deferred nothing and finished inside 70% of the tick budget, capped
at ``pulse_max_boards_per_tick``; hold otherwise. ``_next_board_cap`` is pure,
so the controller is driven here with a stats dict and no network; two ticks
through the real ``run_pulse_tick`` check the wiring.

Deferral semantics are untouched and still guarded by tests/test_pulse_deferral.py:
a board the adaptive cap selected and the tick did not finish is deferred, never
recorded as polled.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource
from app.strategy import pulse_lane

_PREFIX = "pad-"


def _board(slug: str, **kw) -> int:
    with get_session() as session:
        row = CompanyRegistry(
            slug=_PREFIX + slug, ats=JobSource.GREENHOUSE,
            company_name=_PREFIX + slug, is_active=True,
            job_count=kw.pop("job_count", 5), failure_count=0,
            poll_hash="sig-old",
            next_poll_at=datetime.utcnow() - timedelta(hours=2), **kw)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _mine():
    with get_session() as session:
        return session.exec(
            select(CompanyRegistry)
            .where(CompanyRegistry.slug.like(f"{_PREFIX}%"))
            .order_by(CompanyRegistry.id)).all()


def _cleanup() -> None:
    with get_session() as session:
        session.exec(delete(CompanyRegistry).where(
            CompanyRegistry.slug.like(f"{_PREFIX}%")))
        session.commit()


@pytest.fixture(autouse=True)
def _fresh_controller(monkeypatch):
    """Known settings and an unlearned cap for every test; the module state is
    swapped for a private list so nothing leaks between tests or files."""
    init_db()
    _cleanup()
    if pulse_lane._TICK_LOCK.locked():
        try:
            pulse_lane._TICK_LOCK.release()
        except RuntimeError:
            pass
    monkeypatch.setattr(pulse_lane, "_BOARD_CAP", [0])
    monkeypatch.setattr(settings, "pulse_adaptive_enabled", True)
    monkeypatch.setattr(settings, "pulse_max_boards_per_tick", 300)
    monkeypatch.setattr(settings, "pulse_min_boards_per_tick", 40)
    monkeypatch.setattr(settings, "pulse_adaptive_defer_pct", 0.5)
    monkeypatch.setattr(settings, "pulse_tick_max_seconds", 150)
    yield
    _cleanup()


def _limited(selected=300, deferred=231, deadline_hit=0):
    """The measured saturated tick: 300 selected, 231 deferred (77%)."""
    return {"selected": selected, "deferred": deferred,
            "consumer_deadline_hit": deadline_hit}


def _clean(selected=40):
    return {"selected": selected, "deferred": 0, "consumer_deadline_hit": 0}


# ── the numbers ──────────────────────────────────────────────────────────────

def test_the_controller_defaults_are_the_documented_ones():
    from app.config import Settings
    d = Settings.model_fields
    assert d["pulse_adaptive_enabled"].default is True
    assert d["pulse_min_boards_per_tick"].default == 40
    assert d["pulse_adaptive_defer_pct"].default == 0.5
    assert d["pulse_min_boards_per_tick"].default < d["pulse_max_boards_per_tick"].default
    assert 0.0 < d["pulse_adaptive_defer_pct"].default < 1.0


# ── multiplicative decrease ──────────────────────────────────────────────────

def test_a_capacity_limited_tick_halves_the_cap():
    """300 selected, 231 deferred, 159s: the production tick. Next: 150."""
    assert pulse_lane._next_board_cap(300, _limited(), 159.0) == 150


def test_a_consumer_deadline_hit_halves_even_when_few_boards_were_deferred():
    """The deadline flag is the honest capacity signal downstream of the fetch;
    a tick that had to stop consuming is over capacity whatever its deferral
    share says."""
    assert pulse_lane._next_board_cap(300, _limited(deferred=12, deadline_hit=1), 150.0) == 150


def test_the_floor_is_respected():
    assert pulse_lane._next_board_cap(40, _limited(selected=40, deferred=39), 150.0) == 40
    assert pulse_lane._next_board_cap(41, _limited(selected=41, deferred=40), 150.0) == 40
    # int(79 * 0.5) = 39 would undershoot the floor.
    assert pulse_lane._next_board_cap(79, _limited(selected=79, deferred=70), 150.0) == 40


def test_a_deferral_share_at_or_under_the_threshold_holds():
    """50% is the threshold, and it is strict: exactly half deferred is a tick
    that finished half its work, not one that mostly failed."""
    assert pulse_lane._next_board_cap(200, _limited(selected=200, deferred=100), 100.0) == 200
    assert pulse_lane._next_board_cap(200, _limited(selected=200, deferred=101), 100.0) == 100


# ── additive increase ────────────────────────────────────────────────────────

def test_clean_fast_ticks_grow_the_cap_back_to_the_ceiling():
    """From the floor, a run of ticks that deferred nothing and finished early
    climbs back to the ceiling and stays there."""
    cap, path = 40, []
    for _ in range(20):
        cap = pulse_lane._next_board_cap(cap, _clean(selected=cap), 30.0)
        path.append(cap)
    assert path[0] == 51                      # int(40 * 1.25) + 1
    assert all(b >= a for a, b in zip(path, path[1:])), path
    assert 300 in path, f"never reached the ceiling: {path}"
    assert path[-1] == 300 and path[-2] == 300, "the ceiling must hold"


def test_growth_needs_both_a_clean_tick_and_time_to_spare():
    # Nothing deferred but the tick used most of its budget: hold.
    assert pulse_lane._next_board_cap(100, _clean(selected=100), 120.0) == 100
    # Fast, but something was deferred: hold.
    assert pulse_lane._next_board_cap(100, _limited(selected=100, deferred=1), 20.0) == 100
    # Just inside the 70% margin grows; on it does not.
    assert pulse_lane._next_board_cap(100, _clean(selected=100), 104.9) == 126
    assert pulse_lane._next_board_cap(100, _clean(selected=100), 105.0) == 100


def test_the_ceiling_is_respected():
    assert pulse_lane._next_board_cap(300, _clean(selected=300), 10.0) == 300
    assert pulse_lane._next_board_cap(299, _clean(selected=299), 10.0) == 300


def test_a_tick_that_selected_nothing_learns_nothing():
    assert pulse_lane._next_board_cap(200, {"selected": 0, "deferred": 0}, 0.1) == 200


# ── disabled → constant ──────────────────────────────────────────────────────

def test_disabled_means_the_constant_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "pulse_adaptive_enabled", False)
    assert pulse_lane._next_board_cap(40, _limited(), 159.0) == 300
    assert pulse_lane._next_board_cap(300, _clean(selected=300), 1.0) == 300
    pulse_lane._BOARD_CAP[0] = 40
    assert pulse_lane._board_cap() == 300, (
        "with adaptation off the tick must select the settings value, whatever "
        "a previous adaptive run left behind")


def test_the_cap_starts_at_the_ceiling_and_clamps_to_a_lowered_one(monkeypatch):
    assert pulse_lane._board_cap() == 300           # nothing learned yet
    pulse_lane._BOARD_CAP[0] = 120
    assert pulse_lane._board_cap() == 120
    monkeypatch.setattr(settings, "pulse_max_boards_per_tick", 100)
    assert pulse_lane._board_cap() == 100, "a runtime-lowered ceiling must win"


# ── the wiring: two ticks through run_pulse_tick ─────────────────────────────

def _stub_world(monkeypatch, n_boards: int):
    """A tick over this file's boards whose fetches all succeed instantly."""
    ids = [_board(f"b{i}") for i in range(n_boards)]
    monkeypatch.setattr(pulse_lane, "_due_boards", lambda now, limit: _mine()[:limit])
    monkeypatch.setattr("app.strategy.hot_lane._active_users", lambda: [])
    monkeypatch.setattr(pulse_lane, "_watchlist_terms", lambda: set())

    class _Scraper:
        def fetch(self):
            return []

    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, url=None: _Scraper())
    return ids


def test_the_tick_selects_with_the_learned_cap_and_reports_it(monkeypatch):
    """``board_cap`` is in every tick's stats and bounds ``selected``; a clean
    tick then hands a larger cap to the next one."""
    _stub_world(monkeypatch, 6)
    monkeypatch.setattr(settings, "pulse_min_boards_per_tick", 2)
    pulse_lane._BOARD_CAP[0] = 4

    stats = pulse_lane.run_pulse_tick()

    assert stats["board_cap"] == 4
    assert stats["selected"] == 4, "the selection must be bounded by the adaptive cap"
    assert stats["deferred"] == 0 and stats["fetch_ok"] == 4
    assert stats["board_cap_next"] == 6            # int(4 * 1.25) + 1
    assert pulse_lane._BOARD_CAP[0] == 6

    stats2 = pulse_lane.run_pulse_tick()
    assert stats2["board_cap"] == 6
    assert stats2["selected"] == 6


def test_a_tick_that_defers_its_selection_shrinks_the_cap_and_records_no_polls(monkeypatch):
    """The production shape: the deadline is already gone when the consumer
    starts, so everything selected is deferred. The cap halves for the next
    tick — and, unchanged from before, not one deferred board is written as
    polled."""
    _stub_world(monkeypatch, 5)
    before = {r.id: (r.last_seen, r.poll_hash) for r in _mine()}
    monkeypatch.setattr(settings, "pulse_tick_max_seconds", 0)

    stats = pulse_lane.run_pulse_tick()

    assert stats["board_cap"] == 300
    assert stats["deferred"] == stats["selected"] == 5
    assert stats["board_cap_next"] == 150
    assert pulse_lane._BOARD_CAP[0] == 150
    for r in _mine():
        assert (r.last_seen, r.poll_hash) == before[r.id], (
            f"board {r.slug} was recorded as polled although it was deferred")
        assert r.next_poll_at is not None


def test_a_tick_with_nothing_due_leaves_the_cap_alone(monkeypatch):
    monkeypatch.setattr(pulse_lane, "_due_boards", lambda now, limit: [])
    pulse_lane._BOARD_CAP[0] = 120
    stats = pulse_lane.run_pulse_tick()
    assert stats["board_cap"] == 120
    assert "board_cap_next" not in stats
    assert pulse_lane._BOARD_CAP[0] == 120
