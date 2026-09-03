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

from datetime import datetime, timedelta

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


# ── pacing: WHEN the money is available ──────────────────────────────────────
#
# Production, 2026-09-01 -> 09-03 (Railway "Scoring cycle" stats, per UTC hour):
# both users' finals for the day were spent in the 00:xx hour — 176 on 09-02,
# 87 on 09-03 — and the other 23 hours scored 0 (drain-only). Everything posted
# during the US working day waited for the next midnight, and the founder
# account then hit the weekly ceiling on Wednesday. These tests pin the release
# curves that replace that. The budgets are unchanged in SIZE; they arrive
# across the day (and the week) instead of at 00:00.

MONDAY = datetime(2026, 9, 7)                  # 2026-09-07 is a Monday
SUNDAY_NIGHT = datetime(2026, 9, 13, 23, 59, 59)


def _at(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(fb, "_utc_now", lambda: when)


def _drain(uid: str, hit: bool = False) -> int:
    """Spend everything the budget allows right now — what successive lane
    cycles do within one accrual step. Returns the total bought."""
    total = 0
    for _ in range(20):
        a = fb.allowance(uid, per_cycle_cap=40, soft_cap=50)
        if a.n <= 0:
            return total
        _spend(uid, a.n, hit=hit)
        total += a.n
    raise AssertionError("allowance never closed")


def test_the_curves_release_the_full_budget_by_the_end_of_the_day_and_week(monkeypatch):
    """Pacing changes WHEN, never HOW MUCH: at Sunday 23:59:59 every curve reads
    its whole budget — which is why every other test in this file (pinned
    there by conftest) still sees soft/burst/weekly = 50/100/350."""
    _at(monkeypatch, SUNDAY_NIGHT)
    assert fb.paced(50, fb.day_fraction()) == 50
    assert fb.paced(100, fb.day_fraction()) == 100
    assert fb.paced(350, fb.week_fraction()) == 350
    _at(monkeypatch, MONDAY)
    assert fb.paced(50, fb.day_fraction()) == 8, "the 15% head start of 50"
    assert fb.paced(350, fb.week_fraction()) == 50, "one day's worth at Monday 00:00"


def test_just_after_midnight_only_the_head_start_is_available(monkeypatch):
    """00:10 UTC, PRO, bottomless backlog: 8 at the everyday gate (15% of 50),
    then at most the released share of burst (16 = 15% of 100) — not 100."""
    uid = "u-pace-0010"
    _at(monkeypatch, MONDAY.replace(minute=10))
    a = fb.allowance(uid, per_cycle_cap=40, soft_cap=50)
    assert a.n == 8 and a.gate == fb.normal_gate(), "8 = 15% of 50, not the cycle cap of 40"
    assert _drain(uid) == 16
    a = fb.allowance(uid, per_cycle_cap=40, soft_cap=50)
    assert a.n == 0 and a.reason.startswith("paced"), a.reason


def test_the_budget_keeps_arriving_through_the_us_posting_day(monkeypatch):
    """Lane cycles every 30 minutes against a bottomless backlog of weak
    candidates (every final a miss): the 00:xx hour can no longer take the
    day, 13:00-22:00 UTC gets its share, and the day totals the soft budget."""
    uid = "u-pace-day"
    by_hour: dict = {}
    for tick in range(48):
        now = MONDAY + timedelta(minutes=30 * tick)
        _at(monkeypatch, now)
        by_hour[now.hour] = by_hour.get(now.hour, 0) + _drain(uid)
    assert sum(by_hour.values()) == 50, by_hour
    assert by_hour[0] <= 16, f"00:xx spent {by_hour[0]} — the midnight drain is back"
    us_day = sum(v for h, v in by_hour.items() if 13 <= h <= 22)
    assert us_day >= 16, f"13:00-22:00 UTC got only {us_day} finals: {by_hour}"
    assert max(v for h, v in by_hour.items() if h > 0) <= 3, by_hour


def test_unspent_morning_money_is_still_there_in_the_afternoon(monkeypatch):
    """The curve is a ceiling on cumulative spend, not an hourly quota: a user
    whose queue was empty all morning has the whole released share at 18:00."""
    _at(monkeypatch, MONDAY.replace(hour=18))
    assert fb.paced(50, fb.day_fraction()) == 40        # 0.15 + 0.85 x 18/24 -> 39.4 -> 40
    a = fb.allowance("u-pace-1800", per_cycle_cap=40, soft_cap=50)
    assert a.n == 40 and a.gate == fb.normal_gate()


def test_strong_candidates_can_burst_at_any_hour_within_the_released_share(monkeypatch):
    """Burst is paced too: at noon a hot user (every final a hit) may spend up
    to the released burst share — twice the released soft — not the whole 100."""
    uid = "u-pace-burst"
    _at(monkeypatch, MONDAY.replace(hour=12))
    soft_now, burst_now = fb.paced(50, fb.day_fraction()), fb.paced(100, fb.day_fraction())
    assert (soft_now, burst_now) == (29, 58)
    _spend(uid, soft_now, hit=True)
    a = fb.allowance(uid, per_cycle_cap=40, soft_cap=50)
    assert a.gate == fb.burst_gate() and a.n == burst_now - soft_now
    _spend(uid, a.n, hit=True)
    a = fb.allowance(uid, per_cycle_cap=40, soft_cap=50)
    assert a.n == 0 and a.reason.startswith("paced: day"), a.reason


def test_a_hot_start_cannot_exhaust_the_week_by_wednesday(monkeypatch):
    """Founder account, week of 2026-08-31: burst-level days Mon-Wed hit the
    350 and Thu-Sun scored nothing. With the weekly curve, seven hot days
    (every final a hit, so burst is always earned) spend the same 350 — and
    every day, Sunday included, still has a working budget."""
    uid = "u-pace-week"
    per_day = [0] * 7
    for day in range(7):
        for hour in range(0, 24, 2):
            _at(monkeypatch, MONDAY + timedelta(days=day, hours=hour))
            per_day[day] += _drain(uid, hit=True)
    assert 340 <= sum(per_day) <= 350, per_day         # the same money, still spent
    assert max(per_day) <= 100, per_day                # the burst ceiling holds
    assert min(per_day) >= 30, per_day                 # no starved day
    assert per_day[0] > per_day[1], per_day            # Monday's head start is the burst


def test_pacing_off_restores_everything_at_midnight(monkeypatch):
    monkeypatch.setattr(settings, "finals_pace_enabled", False)
    _at(monkeypatch, MONDAY.replace(minute=10))
    assert fb.allowance("u-pace-off", per_cycle_cap=40, soft_cap=50).n == 40


def test_paced_users_are_not_reported_as_a_stall(monkeypatch, caplog):
    """The 'no finals allowance' warning exists for the Aug 14-21 class of
    stall. A user waiting on the release curve is the budget working: the
    cycle counts them apart from hard stops and stays quiet."""
    import logging
    monkeypatch.setattr(sl, "_expire_stale_unscored",
                        lambda: {"total": 0, "queue_stale": 0, "ancient_posting": 0})
    monkeypatch.setattr(sl, "_scorable_user_ids", lambda: ["user-a", "user-b"])
    monkeypatch.setattr(sl, "_finals_allowance",
                        lambda uid, cap: fb.Allowance(0, 40, "paced: day 8/8 released (15% of day)"))
    monkeypatch.setattr(settings, "scoring_drain_cap", 0)
    sl._last_capped_log[0] = float("-inf")
    with caplog.at_level(logging.WARNING, logger="app.strategy.scoring_lane"):
        stats = sl._run_scoring_cycle(None)
    assert stats.get("budget_paced_users") == 2
    assert "plan_capped_users" not in stats
    assert not any("no finals allowance" in r.getMessage() for r in caplog.records)
