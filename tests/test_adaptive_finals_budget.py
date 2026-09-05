"""The finals budget: spend until the day's jobs are DELIVERED.

Two designs have now failed here, and every test below pins the reason one of
them failed. Both were budgets of CALLS:

  1. a flat per-plan cap  — wrong in both directions on the same day: it stopped
     a strong user at 50 while finals were still landing 70-fits, and spent 50
     finals on a Saturday proving 20 weak jobs are weak.
  2. soft / burst / WEEKLY, released along a curve — paced a PRO user to 1.77
     finals/hour (measured prescore->final p50: 685 minutes, p90: 55 hours), and
     on 2026-09-03 the weekly curve, applied to a week whose spend had already
     happened, took production to zero finals for 39 hours while reporting
     itself as "paced", i.e. healthy.

What replaced them asks one question — has this user received the jobs their
plan promises today? — and the invariants that matter most are the ones those
outages produced: no window is longer than a day, no control may retroactively
invalidate spend already made, and a reason meaning "you get nothing" is never
filed under healthy.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.config import settings
from app.db.models import PLAN_LIMITS, PlanTier
from app.matching import finals_budget as fb
from app.strategy import scoring_lane as sl


@pytest.fixture(autouse=True)
def _clean():
    fb.reset_state()
    yield
    fb.reset_state()


def _spend(uid, n, hit=False):
    for _ in range(n):
        fb.record_final(uid)
        fb.record_outcome(uid, 90.0 if hit else 10.0)


MONDAY = datetime(2026, 9, 7)                  # 2026-09-07 is a Monday


def _at(monkeypatch, when: datetime) -> None:
    """Move THE clock. The ledger is keyed by UTC day, so this is how a test
    crosses a day boundary deliberately instead of waiting for one."""
    monkeypatch.setattr(fb, "_utc_now", lambda: when)


# ── what a plan promises ─────────────────────────────────────────────────────

def test_a_plan_is_a_target_and_a_ceiling():
    """A plan states what the user RECEIVES and what that is allowed to cost.
    There is no soft point, no burst, no pace — those described a budget of
    calls, and calls are not what anybody buys."""
    pro, free = PLAN_LIMITS[PlanTier.PRO], PLAN_LIMITS[PlanTier.FREE]
    assert (pro["shortlist_daily"], pro["finals_daily"]) == (35, 250)
    assert (free["shortlist_daily"], free["finals_daily"]) == (20, 120)
    for tier, limits in PLAN_LIMITS.items():
        assert limits["finals_daily"] > limits["shortlist_daily"], (
            f"{tier}: the ceiling must leave room to MISS — a final that scores "
            f"below the bar still cost money and still has to be affordable")


def test_the_pacing_machinery_is_gone_not_merely_disabled():
    """Pacing released ~1.77 finals/hour and produced a measured p50 of 685
    minutes from prescore to final. A disabled knob is a knob someone re-enables
    without reading why it went."""
    for gone in ("finals_pace_enabled", "finals_pace_head_start",
                 "finals_burst_multiplier", "finals_promise_floor"):
        assert not hasattr(settings, gone), gone
    for gone in ("day_fraction", "paced", "budgets", "burst_gate", "week_fraction"):
        assert not hasattr(fb, gone), gone


def test_there_is_exactly_one_tier_one_gate():
    """Two gates existed so the burst zone could demand a stronger candidate.
    With no burst zone, a second gate would only make a job's fate depend on
    what time of day it was picked up."""
    assert fb.normal_gate() == min(settings.prescore_advance_threshold,
                                   settings.shortlist_score_threshold)
    a = fb.allowance("u-gate", per_cycle_cap=10, ceiling=250, target=35)
    assert a.gate == fb.normal_gate()


def test_the_global_backstop_still_clears_the_user_base():
    """LLM_DAILY_FINAL_CAP is the PLATFORM runaway guard. It has to sit above
    what real users can now spend in a day, or it silently becomes the
    allocation again — which is what it was before the per-user budget existed."""
    ceiling = max(l["finals_daily"] for l in PLAN_LIMITS.values())
    assert settings.llm_daily_final_cap >= ceiling * 50


# ── the objective ────────────────────────────────────────────────────────────

def test_scoring_runs_flat_out_until_the_days_jobs_are_delivered(monkeypatch):
    """The whole point. No drip: while the board is short of its target, every
    cycle gets a full slice."""
    monkeypatch.setattr(fb, "delivered_today", lambda uid: 0)
    a = fb.allowance("u-flat", per_cycle_cap=40, ceiling=250, target=35)
    assert a.n == 40 and a.reason == "delivering 0/35"


def test_delivering_the_target_stops_the_spend(monkeypatch):
    """The cheapest stop, and the only one that means success. A user with
    their 35 jobs on the board buys nothing more today however rich the pool."""
    monkeypatch.setattr(fb, "delivered_today", lambda uid: 35)
    a = fb.allowance("u-done", per_cycle_cap=40, ceiling=250, target=35)
    assert a.n == 0 and "on the board" in a.reason


def test_the_cost_ceiling_bounds_a_day_that_never_reaches_the_target(monkeypatch):
    """A pool with nothing good in it must not spend forever looking. The
    target is never reached, so the ceiling is what stops it."""
    monkeypatch.setattr(fb, "delivered_today", lambda uid: 2)
    monkeypatch.setattr(fb, "day_counts", lambda uid: (250, 2))
    a = fb.allowance("u-poor", per_cycle_cap=40, ceiling=250, target=35)
    assert a.n == 0 and "cost ceiling" in a.reason and "2/35 delivered" in a.reason


def test_the_yield_stop_needs_a_real_sample_before_it_may_fire(monkeypatch):
    """The version of this guard that was nearly shipped was ABSORBING, and got
    there by coin-flip: a 10-final window at a 10% continue rate, read from an
    in-process ring that only a PURCHASED FINAL can append to. At the true hit
    rate (~10%), ten consecutive misses happens 35% of the time — and once it
    fired the user could buy no final, so nothing could ever refresh the
    evidence. Zero finals for that user until the next deploy, from the guard
    whose whole purpose is preventing all-day stalls.

    The sample gate is what makes a stop mean something rather than mean noise.
    """
    monkeypatch.setattr(fb, "delivered_today", lambda uid: 0)
    window = settings.finals_yield_window
    monkeypatch.setattr(fb, "day_counts", lambda uid: (window - 1, 0))
    assert fb.allowance("u-thin", 40, 250, 35).n > 0, (
        "a sample under the window is not a verdict — an unlucky opening run "
        "must not end the day")
    monkeypatch.setattr(fb, "day_counts", lambda uid: (window, 0))
    assert fb.allowance("u-dead", 40, 250, 35).n == 0, (
        "zero hits in a full sample IS the fault this guard exists for")


def test_the_yield_stop_cannot_outlive_the_day_that_earned_it(monkeypatch):
    """Within a day the stop is terminal by construction — no finals, no new
    evidence — and that is the intended meaning of "this user's Tier-1 is
    broken today". What must never happen again is it surviving the day: the
    ring version was absorbing until a DEPLOY. The ledger is keyed by UTC day,
    so tomorrow starts clean whatever today looked like."""
    uid = "u-yield-day"
    _at(monkeypatch, MONDAY + timedelta(hours=6))
    for _ in range(settings.finals_yield_window):
        fb.record_final(uid)                        # finals, and not one hit
    assert fb.allowance(uid, 40, 250, 35).n == 0, "a dead day must stop"
    _at(monkeypatch, MONDAY + timedelta(days=1, hours=6))
    fb.reset_state()
    assert fb.allowance(uid, 40, 250, 35).n == 40, "a new day must open clean"


def test_no_control_can_invalidate_spend_already_made(monkeypatch):
    """The 2026-09-03 outage, pinned.

    A weekly release curve was applied to a ledger that already held a
    front-loaded week. A user at 300 finals was past what the curve said the
    week had "released" (158 by Wednesday) and got ZERO for four days — while
    `allowance` reported reason "paced", which the lane files under "the budget
    working" rather than a stall, so nothing warned.

    Any budget window longer than a day can do this again. The day is now the
    only window: whatever a user spent before, a new UTC day opens with a full
    allowance, because yesterday's spend is written to yesterday's row.
    """
    uid = "u-yesterday"
    _at(monkeypatch, MONDAY + timedelta(hours=12))
    for _ in range(400):                     # a very heavy Monday
        fb.record_final(uid)
        fb.record_outcome(uid, 90.0)
    # Tuesday. The ledger is keyed by UTC day, so the new day reads its own row.
    _at(monkeypatch, MONDAY + timedelta(days=1, hours=12))
    fb.reset_state()
    a = fb.allowance(uid, per_cycle_cap=40, ceiling=250, target=35)
    assert a.n == 40, f"a fresh day must have a full allowance, got {a!r}"
    assert a.reason.startswith("delivering"), a.reason


def test_no_evidence_yet_is_not_evidence_against():
    """A user who has scored nothing today has no yield to judge. Defaulting
    pessimistic would stop them before their first final; the daily cost ceiling
    still bounds the risk of defaulting the other way."""
    assert fb.recent_hit_rate("u-fresh") == (1.0, False)


# ── durability: the reason this is not in-memory ─────────────────────────────

def test_spend_survives_a_process_restart():
    """In-memory counters reset on every deploy — which is exactly why the
    Aug 14-21 stall appeared to heal on restart. A cost ceiling cannot be
    honoured by state a deploy erases."""
    _spend("u-restart", 42)
    fb.reset_state()                    # simulate a fresh process
    finals, _hits = fb.day_counts("u-restart")
    assert finals == 42
    assert fb.allowance("u-restart", per_cycle_cap=40, ceiling=50, target=0).n == 8


def test_local_and_anonymous_users_are_never_ledgered():
    _spend("local", 5)
    _spend(None, 5)
    assert fb.day_counts("local") == (0, 0)
    assert fb.day_counts(None) == (0, 0)


# ── one definition of "delivered today" ──────────────────────────────────────

def _app(uid: str, track: str = "manual"):
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus, Job, JobSource
    with get_session() as session:
        j = Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=f"d-{track}-{uid}",
                company="Acme", title="Engineer", url="https://e.com/d", description="x")
        session.add(j)
        session.commit()
        session.refresh(j)
        # Dated on the budget's clock, which conftest pins: the day boundary
        # comes from fb._utc_now(), so a row stamped with the real utcnow()
        # would land on a different day and never be counted.
        session.add(Application(job_id=j.id, user_id=uid, apply_track=track,
                                status=ApplicationStatus.SHORTLISTED,
                                created_at=fb._utc_now()))
        session.commit()
        return j.id


def test_an_email_import_is_not_a_job_we_delivered():
    """Importing a Gmail history writes Application rows dated TODAY for
    applications made elsewhere months ago. Counting them would let one import
    fill the day's quota and switch the user's feed off — the budget would stop
    buying finals and all three shortlist caps would stop accepting jobs."""
    from app.db.init_db import get_session
    from app.db.models import Application, Job
    uid = "u-delivered"
    ids = [_app(uid, "manual"), _app(uid, "email_import")]
    try:
        assert fb.delivered_today(uid, cached=False) == 1
    finally:
        with get_session() as session:
            for jid in ids:
                for a in session.exec(
                        select(Application).where(Application.job_id == jid)).all():
                    session.delete(a)
                obj = session.get(Job, jid)
                if obj:
                    session.delete(obj)
            session.commit()


def test_every_shortlist_cap_uses_the_budgets_definition():
    """Four places decide "how many has this user had today?": the budget and
    the three lanes that shortlist. They were four copies of one query, and the
    budget's whole job is to buy finals for jobs the caps will ACCEPT — a
    definition that drifts makes it pay for jobs that are then refused.

    Source-level: running all three lanes needs FAISS and the ML stack."""
    import inspect
    from app.matching import pipeline
    from app.strategy import pulse_lane
    for mod in (pipeline, pulse_lane, sl):
        src = inspect.getsource(mod)
        assert "delivered_today(" in src, f"{mod.__name__} counts deliveries itself"
        assert "Application.created_at" not in src, (
            f"{mod.__name__} has its own day-count query again — put it in "
            f"finals_budget.delivered_today so the cap and the budget agree")


# ── the lane reads the policy ────────────────────────────────────────────────

def test_the_lane_slice_and_gate_come_from_the_budget(monkeypatch):
    """The lane asks the budget; it does not keep its own counter. 50 finals in
    is well inside Pro's 250 ceiling and short of the 35 target, so the slice is
    the full per-cycle cap at the one gate."""
    monkeypatch.setattr(sl, "_plan_budget", lambda uid: (250, 35))
    monkeypatch.setattr(settings, "prescore_budget_multiplier", 0)
    monkeypatch.setattr(fb, "delivered_today", lambda uid: 0)
    _spend("u-lane", 50, hit=True)
    allow = sl._finals_allowance("u-lane", 40)
    assert allow.n == 40 and allow.gate == fb.normal_gate()
    assert sl._remaining_finals_today("u-lane", 40) == 40


def test_no_plan_cap_fails_open(monkeypatch):
    monkeypatch.setattr(sl, "_plan_budget", lambda uid: (None, 0))
    allow = sl._finals_allowance("u-nocap", 40)
    assert allow.n == 40 and allow.gate == fb.normal_gate()


# ── promise-first ordering ───────────────────────────────────────────────────

def test_the_queue_spends_on_the_most_promising_not_the_most_recent():
    """Arrival order was the quiet half of the flat cap's problem: the day's
    finals went to whatever showed up first. Freshness is the filter; the
    Tier-1 prescore is the sort, applied across the WHOLE queue rather than
    inside a freshness window. A never-prescored job ranks AT the advance gate:
    worth investigating, never worth pre-empting a job Tier-1 has already
    called strong. The old COALESCE-to-100 put unknowns ahead of a genuine 90,
    which is the same defect the window had, one level down."""
    from datetime import datetime, timedelta
    from app.db.init_db import get_session
    from app.db.models import Job, JobSource

    uid = "u-order"
    now = datetime.utcnow()
    made = []
    with get_session() as session:
        for i, pre in enumerate([20.0, 80.0, None, 45.0]):
            j = Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=f"ord-{i}",
                    company="Acme", title="Engineer", url=f"https://e.com/{i}",
                    description="x", prescore=pre,
                    first_seen=now - timedelta(minutes=i))   # index 0 = freshest
            session.add(j)
            session.commit()
            session.refresh(j)
            made.append((j.id, pre))
    try:
        got = sl._user_queue(uid, 4)
        by_id = dict(made)
        # 80 > 45 > unknown(ranks at the 40 gate) > 20
        assert [by_id[i] for i in got] == [80.0, 45.0, None, 20.0]
        assert sl._user_queue(uid, 2) == got[:2], "the cap cuts the WEAKEST, not the oldest"
    finally:
        with get_session() as session:
            for jid, _ in made:
                obj = session.get(Job, jid)
                if obj:
                    session.delete(obj)
            session.commit()


# ── the lane's two very different zeros ──────────────────────────────────────

def test_a_delivered_user_is_not_reported_as_a_stall(monkeypatch, caplog):
    """The lane's warning exists for the Aug 14-21 class of stall. A user whose
    day's jobs are already ON THE BOARD is the opposite of that — success — so
    the cycle counts them apart from users who stopped short, and stays quiet.

    The 2026-09-03 outage was the same bug pointed the other way: a reason that
    meant "you get nothing" was filed under "the budget working", so 39 hours of
    zero finals produced no warning at all. Only "delivered" is quiet.
    """
    import logging
    monkeypatch.setattr(sl, "_expire_stale_unscored",
                        lambda: {"total": 0, "queue_stale": 0, "ancient_posting": 0})
    monkeypatch.setattr(sl, "_scorable_user_ids", lambda: ["user-a", "user-b"])
    monkeypatch.setattr(sl, "_finals_allowance", lambda uid, cap: fb.Allowance(
        0, 40, "delivered 35/35 — the day's jobs are on the board"))
    monkeypatch.setattr(settings, "scoring_drain_cap", 0)
    sl._last_capped_log[0] = float("-inf")
    with caplog.at_level(logging.WARNING, logger="app.strategy.scoring_lane"):
        stats = sl._run_scoring_cycle(None)
    assert stats.get("target_met_users") == 2
    assert "plan_capped_users" not in stats
    assert not any("stopped SHORT" in r.getMessage() for r in caplog.records)


def test_a_user_stopped_by_the_cost_ceiling_is_reported(monkeypatch, caplog):
    """The other side of the same taxonomy, and the one the outage needed: a
    user who did NOT get their day's jobs is a warning, every time."""
    import logging
    monkeypatch.setattr(sl, "_expire_stale_unscored",
                        lambda: {"total": 0, "queue_stale": 0, "ancient_posting": 0})
    monkeypatch.setattr(sl, "_scorable_user_ids", lambda: ["user-a"])
    monkeypatch.setattr(sl, "_finals_allowance", lambda uid, cap: fb.Allowance(
        0, 40, "daily cost ceiling (250/250 finals) at 9/35 delivered"))
    monkeypatch.setattr(settings, "scoring_drain_cap", 0)
    sl._last_capped_log[0] = float("-inf")
    with caplog.at_level(logging.WARNING, logger="app.strategy.scoring_lane"):
        stats = sl._run_scoring_cycle(None)
    assert stats.get("plan_capped_users") == 1
    assert "target_met_users" not in stats
    assert any("stopped SHORT" in r.getMessage() for r in caplog.records)
