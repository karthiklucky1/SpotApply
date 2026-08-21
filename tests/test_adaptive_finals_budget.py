"""The adaptive finals budget: spend on evidence, never on a counter.

The flat per-plan cap was wrong in both directions on the same day — it stopped
a strong user at 50 while finals were still landing 70-fits, and spent 50 finals
on a Saturday proving 20 weak jobs are weak. These tests pin the replacement:

  soft   = PLAN_LIMITS["finals_daily"]      spent freely
  burst  = soft x FINALS_BURST_MULTIPLIER   hard daily ceiling
  weekly = soft x FINALS_WEEKLY_MULTIPLIER  the real economic control

and the two tests that gate the burst zone (promise floor, marginal yield).

The invariant that matters most is the LAST one: weekly = 7 x soft means a
bursting user costs exactly what the flat cap already cost. If that relation
ever breaks, the design has quietly become a price increase.
"""
from __future__ import annotations

import pytest

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


# ── the three budgets ────────────────────────────────────────────────────────

def test_the_three_budgets_come_from_one_plan_dial():
    soft, burst, weekly = fb.budgets(PLAN_LIMITS[PlanTier.PRO]["finals_daily"])
    assert (soft, burst, weekly) == (50, 100, 350)
    soft, burst, weekly = fb.budgets(PLAN_LIMITS[PlanTier.FREE]["finals_daily"])
    assert (soft, burst, weekly) == (15, 30, 105)


def test_bursting_costs_the_same_money_as_the_flat_cap():
    """weekly == 7 x soft, i.e. a 50/day average. Burst reallocates spend across
    the week; it must never ADD any. Raising finals_weekly_multiplier is the one
    change here that increases what a user costs."""
    assert settings.finals_weekly_multiplier == 7.0
    for tier in (PlanTier.FREE, PlanTier.PRO, PlanTier.AGENCY):
        soft, _burst, weekly = fb.budgets(PLAN_LIMITS[tier]["finals_daily"])
        assert weekly == soft * 7


def test_the_promise_floor_sits_between_the_gate_and_the_shortlist_bar():
    """At or below the everyday advance gate it does nothing; at or above the
    shortlist bar it demands Tier-1 already know the answer Tier-2 is paid for."""
    assert settings.prescore_advance_threshold < settings.finals_promise_floor
    assert settings.finals_promise_floor < settings.shortlist_score_threshold
    assert fb.burst_gate() == settings.finals_promise_floor
    assert fb.normal_gate() == 40


def test_the_global_backstop_still_clears_a_bursting_user_base():
    """LLM_DAILY_FINAL_CAP has to sit above what real users can now spend in a
    day, or the platform backstop silently becomes the allocation again."""
    _s, burst, _w = fb.budgets(PLAN_LIMITS[PlanTier.PRO]["finals_daily"])
    assert settings.llm_daily_final_cap >= burst * 50


# ── inside the soft budget ───────────────────────────────────────────────────

def test_inside_the_soft_budget_we_spend_freely_at_the_everyday_gate():
    a = fb.allowance("u-soft", per_cycle_cap=40, soft_cap=50)
    assert a.n == 40 and a.gate == fb.normal_gate()

    _spend("u-soft", 30)                       # 30 spent, all misses
    a = fb.allowance("u-soft", per_cycle_cap=40, soft_cap=50)
    assert a.n == 20, "the slice must stop AT the soft boundary, not straddle it"
    assert a.gate == fb.normal_gate(), "everyday rules still apply below soft"


# ── the burst zone ───────────────────────────────────────────────────────────

def test_a_strong_day_earns_burst_at_the_strict_gate():
    """Monday: finals keep landing matches, so spending continues past soft —
    but only on candidates that clear the promise floor."""
    _spend("u-strong", 50, hit=True)
    a = fb.allowance("u-strong", per_cycle_cap=40, soft_cap=50)
    assert a.n == 40
    assert a.gate == fb.burst_gate() > fb.normal_gate()


def test_a_weak_day_stops_at_soft_even_with_jobs_left():
    """Saturday: the queue is not empty, but recent finals are not producing
    matches. Stop — never keep paying to reach a target number."""
    _spend("u-weak", 50, hit=False)
    a = fb.allowance("u-weak", per_cycle_cap=40, soft_cap=50)
    assert a.n == 0
    assert "yield" in a.reason


def test_burst_is_a_hard_ceiling_however_strong_the_day_is():
    _spend("u-hot", 100, hit=True)
    a = fb.allowance("u-hot", per_cycle_cap=40, soft_cap=50)
    assert a.n == 0 and a.reason == "daily burst ceiling"


def test_the_weekly_budget_binds_across_days():
    """Four strong days at burst would be 400 finals; the week allows 350."""
    uid = "u-week"
    for _ in range(340):
        fb.record_final(uid)
        fb.record_outcome(uid, 90.0)
    # Same UTC day here, so the day ceiling would bite first — check the weekly
    # arithmetic directly against the ledger the day counters share.
    assert fb.week_finals(uid) == 340
    a = fb.allowance(uid, per_cycle_cap=40, soft_cap=50)
    assert a.n == 0
    for _ in range(20):
        fb.record_final(uid)
    assert fb.week_finals(uid) == 360
    assert fb.allowance(uid, per_cycle_cap=40, soft_cap=50).reason == "weekly budget spent"


def test_no_evidence_yet_is_not_evidence_against():
    """A fresh process mid-day has an empty outcome ring. Defaulting pessimistic
    would cap every restart at soft; burst and weekly still bound the risk."""
    assert fb.recent_hit_rate("u-fresh") == 1.0


# ── durability: the reason this is not in-memory ─────────────────────────────

def test_spend_survives_a_process_restart():
    """In-memory counters reset on every deploy — which is exactly why the
    Aug 14-21 stall appeared to heal on restart. A weekly budget cannot be
    honoured by state a deploy erases."""
    _spend("u-restart", 42)
    fb.reset_state()                    # simulate a fresh process
    finals, _hits = fb.day_counts("u-restart")
    assert finals == 42
    assert fb.allowance("u-restart", per_cycle_cap=40, soft_cap=50).n == 8


def test_local_and_anonymous_users_are_never_ledgered():
    _spend("local", 5)
    _spend(None, 5)
    assert fb.day_counts("local") == (0, 0)
    assert fb.day_counts(None) == (0, 0)


# ── the lane reads the policy ────────────────────────────────────────────────

def test_the_lane_slice_and_gate_come_from_the_budget(monkeypatch):
    monkeypatch.setattr(sl, "_plan_finals_cap", lambda uid: 50)
    monkeypatch.setattr(settings, "prescore_budget_multiplier", 0)
    _spend("u-lane", 50, hit=True)
    allow = sl._finals_allowance("u-lane", 40)
    assert allow.n == 40 and allow.gate == fb.burst_gate()
    assert sl._remaining_finals_today("u-lane", 40) == 40


def test_burst_never_drains_the_middle_band():
    """A job whose prescore sits between the drain gate and the burst gate is
    NOT a misfit. Stamping it there would make an identical job's fate depend on
    what time of day it was picked up — it must stay Queued for the soft budget.
    """
    ctx = sl._Ctx("resume", None, True, fb.normal_gate(), fb.burst_gate())
    assert ctx.gate == 40 and ctx.spend_gate == 55

    seen = {}
    stamped = []

    class _RK:
        @staticmethod
        def prescore(resume, job):
            return (45.0, "adjacent role")     # above drain gate, below burst gate
    ctx.reranker = _RK()

    import app.strategy.scoring_lane as _sl
    orig_stamp = _sl._stamp_job
    _sl._stamp_job = lambda *a, **k: stamped.append(a) or True
    orig_session = _sl.get_session
    try:
        from app.db.models import Job, JobSource
        job = Job(id=99123, user_id="u", source=JobSource.GREENHOUSE, external_id="mid",
                  company="Acme", title="Engineer", url="https://e.com/x", description="x")

        class _S:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a): return job
            def expunge(self, *a): pass
        _sl.get_session = lambda: _S()
        _sl._prescore_memo.pop(99123, None)
        out = _sl._score_job_owned(99123, ctx)
    finally:
        _sl._stamp_job = orig_stamp
        _sl.get_session = orig_session
        seen.clear()

    assert out is None, "must not be scored — burst money is for strong candidates"
    assert not stamped, "and must NOT be drained: it is not a misfit"
    assert _sl._prescore_memo.get(99123) == (45.0, "adjacent role"), \
        "the prescore is memoized so waiting for the soft budget costs nothing"
    _sl._prescore_memo.pop(99123, None)


def test_no_plan_cap_fails_open(monkeypatch):
    monkeypatch.setattr(sl, "_plan_finals_cap", lambda uid: None)
    allow = sl._finals_allowance("u-nocap", 40)
    assert allow.n == 40 and allow.gate == fb.normal_gate()


# ── promise-first ordering ───────────────────────────────────────────────────

def test_the_queue_spends_on_the_most_promising_not_the_most_recent():
    """Arrival order was the quiet half of the flat cap's problem: the day's
    finals went to whatever showed up first. Freshness is the filter; the
    Tier-1 prescore is the sort. Never-prescored jobs sort first — 'unknown' is
    worth $0.0002 to resolve, and the cascade drains it free if it is weak."""
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
        assert [by_id[i] for i in got] == [None, 80.0, 45.0, 20.0]
        assert sl._user_queue(uid, 2) == got[:2], "the cap cuts the WEAKEST, not the oldest"
    finally:
        with get_session() as session:
            for jid, _ in made:
                obj = session.get(Job, jid)
                if obj:
                    session.delete(obj)
            session.commit()
