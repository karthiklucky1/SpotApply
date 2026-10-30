"""Tier-1 has its own per-user daily cap: 2,000, or 5,000 on a setup day.

Production 2026-10-30: the all-calls backstop (USER_DAILY_PAID_CALL_CAP, then
2,000) stopped a first-day user at 12:36 UTC with 2,229 fresh jobs never looked
at, and the busiest established user the evening before. Tier-1 now has a
number of its own and the backstop sits above it.

The invariant with money attached: a capped Tier-1 call is TIER1_DEFERRED,
never None. None means "advance to Tier-2" (fail-open), and a capped job must
stay queued — not be bought a Claude final nobody judged.

Rows are prefixed ``t1_`` and cleaned by that prefix.
"""
from __future__ import annotations

import inspect
from datetime import datetime

import pytest
from sqlmodel import delete

from app.common import daily_counter
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Job, JobSource, PlatformCounter
from app.matching import tier1_budget as t1
from app.matching.reranker import Reranker
from app.matching.tier1_budget import TIER1_DEFERRED
from app.strategy import scoring_lane as sl

_P = "t1_"


@pytest.fixture(autouse=True)
def _clean():
    t1.reset_state()
    _wipe()
    yield
    _wipe()
    t1.reset_state()
    sl._prescore_memo.clear()


def _wipe():
    with get_session() as s:
        s.exec(delete(PlatformCounter).where(PlatformCounter.name.like(f"%:user:{_P}%")))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.commit()


def _seed(uid, ext, prescore=None) -> int:
    with get_session() as s:
        j = Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=_P + ext,
                company=f"Co-{ext}", title="Backend Engineer", url=f"https://x/{_P}{ext}",
                description="Python APIs " * 10, prescore=prescore,
                first_seen=datetime.utcnow(), posted_at=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


# ── the numbers and the counter ──────────────────────────────────────────────

def test_the_documented_numbers():
    assert settings.tier1_daily_cap == 2000
    assert settings.tier1_setup_day_cap == 5000


def test_a_setup_day_raises_the_cap_until_midnight():
    uid = _P + "setup"
    assert t1.daily_cap(uid) == 2000
    t1.mark_setup_day(uid)
    assert t1.daily_cap(uid) == 5000
    t1.reset_state()                    # a restart re-reads the persisted day
    assert t1.daily_cap(uid) == 5000
    assert t1.daily_cap(None) == 0 and t1.daily_cap("local") == 0


def test_the_cap_refuses_and_is_remembered(monkeypatch):
    monkeypatch.setattr(settings, "tier1_daily_cap", 3)
    monkeypatch.setattr(settings, "tier1_setup_day_cap", 5)
    uid = _P + "cap"
    assert [t1.take(uid) for _ in range(4)] == [True, True, True, False]
    assert t1.exhausted(uid)
    assert t1.used_today(uid) == 3

    # Saving roles mid-day reopens it under the setup-day cap.
    t1.mark_setup_day(uid)
    assert not t1.exhausted(uid)
    assert [t1.take(uid) for _ in range(3)] == [True, True, False]


def test_a_database_blip_is_not_remembered_as_a_spent_day(monkeypatch):
    """The reservation refuses on a DB error. Remembering THAT as "cap reached"
    would stop the user's Tier-1 for the rest of the day over one blip."""
    uid = _P + "blip"
    monkeypatch.setattr(daily_counter, "reserve", lambda name, cap, day=None: False)
    monkeypatch.setattr(daily_counter, "count", lambda name, day=None, session=None: None)
    assert t1.take(uid) is False
    assert t1.exhausted(uid) is False


def test_unlimited_never_refuses(monkeypatch):
    monkeypatch.setattr(settings, "tier1_daily_cap", 0)
    monkeypatch.setattr(daily_counter, "reserve", lambda name, cap, day=None: False)
    assert t1.take(_P + "unl") is True


def test_the_counters_are_purged_with_the_account():
    """account_purge deletes platform_counter rows named '…:user:<uid>'."""
    assert t1._name("abc").endswith(":user:abc")
    assert t1._setup_name("abc").endswith(":user:abc")


def test_the_paid_call_backstop_sits_above_tier1_and_the_staged_ceiling():
    """The backstop counts EVERY paid call. At 2,000 it was the real limit."""
    from app.db.models import PLAN_LIMITS
    widest = max(p["finals_daily"] + p.get("finals_challenge_extra", 0)
                 + p.get("finals_engaged_extra", 0) for p in PLAN_LIMITS.values())
    assert settings.user_daily_paid_call_cap >= settings.tier1_setup_day_cap + widest + 500


# ── prescore() ───────────────────────────────────────────────────────────────

def _reranker(uid, calls):
    profile = type("P", (), {"user_id": uid})()
    rk = Reranker(profile=profile)

    def _openai(prompt):
        calls.append(prompt)
        return '{"score": 70, "reason": "fit"}'
    rk._prescore_backends = lambda: iter([("openai", _openai)])
    rk._pre_filter_job = lambda job: None
    return rk


def _job_obj(desc="Python backend " * 20):
    return Job(id=1, source=JobSource.GREENHOUSE, external_id="x", company="Acme",
               title="Software Engineer", location="Remote", remote=True,
               url="https://example.com/j", description=desc)


def test_a_capped_prescore_is_deferred_not_fail_open(monkeypatch):
    monkeypatch.setattr(settings, "tier1_daily_cap", 1)
    monkeypatch.setattr(settings, "llm_daily_final_cap", 0)
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 0)
    calls: list = []
    rk = _reranker(_P + "pre", calls)
    assert rk.prescore("resume", _job_obj())[0] == 70.0
    out = rk.prescore("resume", _job_obj())
    assert out is TIER1_DEFERRED and out is not None
    assert len(calls) == 1, "a refused Tier-1 must not reach a provider"


def test_no_unit_is_spent_when_no_provider_can_be_asked(monkeypatch):
    """A provider cooldown, the platform budget or an idle search must not
    spend the user's Tier-1 day on calls that never happen — the outage would
    outlive itself as a feed outage until 00:00 UTC."""
    uid = _P + "cool"
    calls: list = []
    rk = _reranker(uid, calls)
    rk._prescore_backends = lambda: iter([])                 # every provider cooling down
    assert rk.prescore("resume", _job_obj()) is None          # fail-open, as before
    rk = _reranker(uid, calls)
    rk._paid_call_allowed = lambda: False                     # platform cap / idle search
    assert rk.prescore("resume", _job_obj()) is None
    assert calls == [] and t1.used_today(uid) is None


def test_a_fallback_provider_does_not_count_twice(monkeypatch):
    uid = _P + "fallback"
    rk = _reranker(uid, [])

    def _fail(prompt):
        raise RuntimeError("timeout")
    rk._prescore_backends = lambda: iter([
        ("openai", _fail), ("anthropic", lambda prompt: '{"score": 66, "reason": "ok"}')])
    monkeypatch.setattr(settings, "llm_daily_final_cap", 0)
    assert rk.prescore("resume", _job_obj())[0] == 66.0
    assert t1.used_today(uid) == 1


def test_free_verdicts_are_not_counted(monkeypatch):
    """The rule pre-filter and the empty-JD guard ask no provider."""
    calls: list = []
    uid = _P + "free"
    rk = _reranker(uid, calls)
    assert rk.prescore("resume", _job_obj(desc="short"))[1] == "no description"
    rk._pre_filter_job = lambda job: (10.0, "Location filtered: x", [], {})
    assert rk.prescore("resume", _job_obj())[0] == 10.0
    assert calls == [] and t1.used_today(uid) is None


# ── the three lanes leave a deferred job queued ──────────────────────────────

class _DeferringReranker:
    def __init__(self):
        self.final_calls = 0
        self.prescore_calls = 0

    def prescore(self, resume, job):
        self.prescore_calls += 1
        return TIER1_DEFERRED

    def score(self, *a, **kw):
        self.final_calls += 1
        raise AssertionError("a Tier-1-deferred job was bought a final")

    score_with_meta = score

    def has_prescore_backend(self):
        return True


def test_scoring_lane_leaves_a_deferred_job_untouched():
    uid = _P + "lane"
    jid = _seed(uid, "lane")
    rr = _DeferringReranker()
    ctx = sl._Ctx("resume", rr, use_prescore=True, gate=40, spend_gate=40)
    assert sl._score_job_owned(jid, ctx) is None
    with get_session() as s:
        job = s.get(Job, jid)
    assert job.rerank_score is None and job.prescore is None
    assert rr.final_calls == 0


def test_a_capped_user_is_offered_only_jobs_tier1_already_judged():
    uid = _P + "queue"
    kept = _seed(uid, "kept", prescore=81.0)
    _seed(uid, "unknown")
    assert set(sl._user_queue(uid, 10)) >= {kept}
    assert sl._user_queue(uid, 10, only_prescored=True) == [kept]


def test_the_cycle_asks_for_kept_prescores_and_no_drain_for_a_capped_user(monkeypatch):
    from app.matching import finals_budget as fb
    uid = _P + "cycle"
    monkeypatch.setattr(sl, "_expire_stale_unscored",
                        lambda **kw: {"total": 0, "queue_stale": 0,
                                      "ancient_posting": 0, "stopped": ""})
    monkeypatch.setattr(sl, "_scorable_user_ids", lambda: [uid])
    monkeypatch.setattr(t1, "exhausted", lambda u: u == uid)
    asked: list = []
    monkeypatch.setattr(sl, "_user_queue", lambda u, cap, **kw: asked.append(kw) or [])

    monkeypatch.setattr(sl, "_finals_allowance",
                        lambda u, cap: fb.Allowance(40, 40, "delivering 0/35"))
    stats = sl._run_scoring_cycle(None)
    assert [kw["only_prescored"] for kw in asked] == [True]
    assert asked[0]["min_prescore"] is None, "a filling board takes any promise"
    assert stats.get("tier1_capped_users") == 1

    # Board full: only kept prescores that can beat the challenger gate.
    asked.clear()
    monkeypatch.setattr(sl, "_finals_allowance", lambda u, cap: fb.Allowance(
        40, 82, "slate full at 35/35", target_met=True))
    sl._run_scoring_cycle(None)
    assert asked and asked[0]["min_prescore"] == 82

    asked.clear()
    monkeypatch.setattr(settings, "scoring_drain_cap", 20)
    monkeypatch.setattr(sl, "_finals_allowance", lambda u, cap: fb.Allowance(
        0, 40, "daily cost ceiling (400/400 finals, engaged stage) at 35/35 delivered",
        target_met=True))
    sl._run_scoring_cycle(None)
    assert asked == [], "a drain slice is Tier-1-only work; a capped user gets none"


def test_pulse_fast_path_skips_a_capped_user_before_reading_rows(monkeypatch):
    import app.strategy.pulse_lane as pl
    uid = _P + "pulse"
    _seed(uid, "pulse")
    monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: "resume")

    asked: list = []
    real_allowance = sl._finals_allowance

    def _allow(u, cap):
        asked.append(u)
        return real_allowance(u, cap)
    monkeypatch.setattr(sl, "_finals_allowance", _allow)

    class _Fake(_DeferringReranker):
        def __init__(self, profile=None, feedback=""):
            super().__init__()
    monkeypatch.setattr("app.matching.reranker.Reranker", _Fake)
    monkeypatch.setattr(t1, "exhausted", lambda u: u == uid)
    assert pl._fast_path_user(uid, score_budget=10) == (0, 0, 0)
    assert asked == [], "the allowance (a board read) was computed for a Tier-1-capped user"

    # Not capped: the same call does reach the allowance, so the check above
    # is what returned — not some earlier gate.
    monkeypatch.setattr(t1, "exhausted", lambda u: False)
    pl._fast_path_user(uid, score_budget=10)
    assert asked == [uid]


def test_pulse_fast_path_stops_at_a_deferred_prescore(monkeypatch):
    import app.strategy.pulse_lane as pl
    uid = _P + "pulse2"
    jid = _seed(uid, "pulse2")
    monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: "resume")
    made: list = []

    class _Fake(_DeferringReranker):
        def __init__(self, profile=None, feedback=""):
            super().__init__()
            made.append(self)
    monkeypatch.setattr("app.matching.reranker.Reranker", _Fake)

    class _G:
        is_ghost = False
        ghost_score = 0.0
        flags_json = None
        flags = []
    monkeypatch.setattr("app.matching.filters.score_ghost", lambda job, session: _G())
    assert pl._fast_path_user(uid, score_budget=10) == (0, 0, 0)
    assert made and made[0].prescore_calls == 1, "the job must reach Tier-1 for this to test anything"
    assert made[0].final_calls == 0
    with get_session() as s:
        job = s.get(Job, jid)
    assert job.rerank_score is None and job.prescore is None


def test_matching_lane_neither_advances_nor_stamps_a_deferred_job():
    """run_matching is too large to drive here; pin the shape instead: the
    deferred set is taken out BEFORE the `pr is None` fail-open branch, and out
    of the promise map."""
    from app.matching import pipeline
    src = inspect.getsource(pipeline.run_matching)
    assert "TIER1_DEFERRED" in src
    i_def = src.index("if jid in _deferred_t1:")
    i_open = src.index("if pr is None:              # cheap-model failure")
    assert i_def < i_open, "a deferred job must be skipped before fail-open can advance it"
    assert "jid not in _deferred_t1" in src, "a deferred job must not enter the promise map"
    i_cap = src.index("_t1.exhausted(user_id)")
    i_pool = src.index("ex.map(_prescore_one, prescore_pool)")
    assert i_cap < i_pool, ("a capped user's pool must be cut to kept prescores BEFORE "
                            "each candidate is loaded for a Tier-1 call")


def test_a_setup_day_is_marked_by_seed_new_user():
    from app.strategy import adoption
    assert "mark_setup_day(user_id)" in inspect.getsource(adoption.seed_new_user)
