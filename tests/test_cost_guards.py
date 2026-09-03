"""Cost guards: provider circuit breaker, daily spend cap, cache-minimum
padding, per-job attempt ceiling, round-robin fairness, adoption extras cap.

These are the levers that prevent a repeat of the Jul-15 overnight spend:
every test exercises a guard with fake clients — no API keys, no spend.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest
from sqlmodel import delete

import app.matching.reranker as rr
import app.strategy.scoring_lane as sl
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, FunnelEvent, Job, JobSource, UserNotification, UserProfile


@pytest.fixture(autouse=True)
def _reset_guard_state():
    """Module-level guard state must not leak between tests."""
    rr._provider_down_until.clear()
    rr._daily_finals["day"] = ""
    rr._daily_finals["count"] = 0
    rr._hourly_finals["hour"] = ""
    rr._hourly_finals["count"] = 0
    rr._user_finals.clear()
    sl._fail_counts.clear()
    sl._deferred_until.clear()
    yield
    rr._provider_down_until.clear()
    rr._daily_finals["day"] = ""
    rr._daily_finals["count"] = 0
    rr._hourly_finals["hour"] = ""
    rr._hourly_finals["count"] = 0
    rr._user_finals.clear()
    sl._fail_counts.clear()
    sl._deferred_until.clear()


def _job(title="Senior ML Engineer", jid=None):
    j = Job(title=title, company="Acme", location="Remote", remote=True,
            description="Build LLM systems in Python: FastAPI services, PyTorch training pipelines, and PostgreSQL data layers on AWS.", source=JobSource.GREENHOUSE,
            external_id="x1", url="https://x/1")
    if jid is not None:
        j.id = jid
    return j


def _reranker_with(anthropic=True, openai=True):
    rk = rr.Reranker.__new__(rr.Reranker)
    rk._profile = None
    rk._feedback = ""
    rk._anthropic_client = object() if anthropic else None
    rk._openai_client = object() if openai else None
    rk._active_backend = "anthropic" if anthropic else ("openai" if openai else None)
    return rk


# ── Circuit breaker ───────────────────────────────────────────────────────────
def test_credit_error_marks_provider_down_and_falls_back(monkeypatch):
    rk = _reranker_with(True, True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_score_anthropic", lambda rb, jb: (_ for _ in ()).throw(
        RuntimeError("Your credit balance is too low")))
    monkeypatch.setattr(rk, "_score_openai", lambda rb, jb:
                        '{"score": 70, "reason": "ok", "concerns": [], "breakdown": {}}')

    score, *_ = rk.score("resume", _job(), provider="anthropic")
    assert score == 70.0                                  # fallback served it
    assert not rr.provider_available("anthropic")         # breaker tripped
    assert rr.provider_available("openai")


def test_down_provider_is_skipped_without_api_call(monkeypatch):
    rk = _reranker_with(True, True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    calls = {"anthropic": 0}

    def _anthropic(rb, jb):
        calls["anthropic"] += 1
        raise AssertionError("should not be called while down")
    monkeypatch.setattr(rk, "_score_anthropic", _anthropic)
    monkeypatch.setattr(rk, "_score_openai", lambda rb, jb:
                        '{"score": 61, "reason": "ok", "concerns": [], "breakdown": {}}')

    rr._mark_provider_down("anthropic")
    score, *_ = rk.score("resume", _job(), provider="anthropic")
    assert score == 61.0 and calls["anthropic"] == 0


def test_all_providers_down_raises_before_any_call(monkeypatch):
    # Old wait-for-a-provider behavior — explicit opt-out of the local fallback.
    monkeypatch.setattr(settings, "local_score_fallback", False)
    rk = _reranker_with(True, True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    rr._mark_provider_down("anthropic")
    rr._mark_provider_down("openai")
    assert not rr.any_provider_available()
    with pytest.raises(RuntimeError, match="cooling down"):
        rk.score("resume", _job())


def test_all_providers_down_falls_back_to_local(monkeypatch):
    # Default behavior: with every provider cooling down, score() returns a
    # labeled local estimate instead of stranding the job (LOCAL_SCORE_FALLBACK).
    monkeypatch.setattr(settings, "local_score_fallback", True)
    rk = _reranker_with(True, True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_ce_relevance", lambda resume, job: 0.30)
    rr._mark_provider_down("anthropic")
    rr._mark_provider_down("openai")
    score, reason, concerns, breakdown = rk.score("resume", _job())
    assert reason.startswith(rr.LOCAL_REASON_PREFIX)
    assert 0.0 <= score <= 100.0
    assert set(breakdown) == {"skills", "experience", "location", "work_auth"}


def test_breaker_expires_after_cooldown():
    rr._mark_provider_down("anthropic")
    assert not rr.provider_available("anthropic")
    rr._provider_down_until["anthropic"] = time.time() - 1  # cooldown elapsed
    assert rr.provider_available("anthropic")


def test_breaker_disabled_when_cooldown_zero(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider_cooldown_minutes", 0)
    rr._mark_provider_down("anthropic")
    assert rr.provider_available("anthropic")


def test_prescore_credit_error_trips_breaker(monkeypatch):
    rk = _reranker_with(anthropic=False, openai=True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_prescore_openai", lambda prompt: (_ for _ in ()).throw(
        RuntimeError("insufficient_quota: You exceeded your current quota")))
    assert rk.prescore("resume", _job()) is None          # fail-open
    assert not rr.provider_available("openai")


# ── Daily spend cap ───────────────────────────────────────────────────────────
def test_daily_budget_blocks_finals_past_cap(monkeypatch):
    monkeypatch.setattr(settings, "llm_daily_final_cap", 2)
    rk = _reranker_with(True, False)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_score_anthropic", lambda rb, jb:
                        '{"score": 80, "reason": "ok", "concerns": [], "breakdown": {}}')

    assert rk.score("resume", _job())[0] == 80.0
    assert rk.score("resume", _job())[0] == 80.0
    assert rr.llm_budget_exhausted()
    with pytest.raises(RuntimeError, match="budget"):
        rk.score("resume", _job())                        # third call: no spend


def test_daily_budget_resets_on_new_day(monkeypatch):
    monkeypatch.setattr(settings, "llm_daily_final_cap", 1)
    rr._daily_finals["day"] = "1999-01-01"
    rr._daily_finals["count"] = 999
    assert not rr.llm_budget_exhausted()                  # stale day ≠ today


def test_daily_budget_unlimited_when_zero(monkeypatch):
    monkeypatch.setattr(settings, "llm_daily_final_cap", 0)
    rr._daily_finals["day"] = datetime.utcnow().strftime("%Y-%m-%d")
    rr._daily_finals["count"] = 10**9
    assert not rr.llm_budget_exhausted()


# ── Cache-minimum padding ─────────────────────────────────────────────────────
def test_resume_block_padded_past_cache_minimum():
    resume = "PYTHON ML ENGINEER RESUME LINE\n" * 150   # ~4.6K chars — typical résumé
    block = rr._resume_context_block(resume)
    assert len(block) >= rr._CACHE_MIN_BLOCK_CHARS       # crosses Haiku's 4096-token floor
    assert "<resume_repeat>" in block                    # padded with the résumé itself
    assert "no new information" in block                 # pad is labeled inert


def test_long_resume_needs_no_padding():
    resume = "x" * 20000
    block = rr._resume_context_block(resume)
    assert "<resume_repeat>" not in block
    assert resume[: rr._RESUME_SLICE_CHARS] in block     # slice raised from 6000


@pytest.mark.parametrize("length", [1, 50, 200, 500, 1000, 2579, 2580, 5000, 15000])
def test_every_resume_length_clears_the_cache_minimum(length):
    """Replaces `test_tiny_resume_is_not_padded`, which asserted the bug.

    That test accepted "stays uncached (status quo)" for résumés under ~2,580
    chars, on the reasoning that padding them "would need 100s of repeats". The
    repeat COUNT is irrelevant — the padded block is a constant
    ~_CACHE_MIN_BLOCK_CHARS whether it took one repetition or two hundred, so a
    short résumé costs exactly what a long one does to cache. What the guard
    actually bought was a permanent 2.3x markup ($0.0075 vs $0.0033 per final)
    on precisely the newest users, who have the shortest résumés.

    Padding is one cache WRITE at 1.25x that pays for itself on the second job
    of a batch, and users are scored in batches of dozens to hundreds.
    """
    block = rr._resume_context_block("x" * length)
    assert len(block) >= rr._CACHE_MIN_BLOCK_CHARS, (
        f"{length}-char résumé -> {len(block)}-char block: under the cache "
        "minimum, so cache_control is silently ignored and every final re-bills"
    )


def test_empty_resume_is_not_padded():
    """Nothing to repeat — must not spin or emit a pad of empty strings."""
    block = rr._resume_context_block("")
    assert "<resume_repeat>" not in block
    assert len(block) < 100


# ── Shared LLM clients ────────────────────────────────────────────────────────
# A Reranker is built per user per 90s cycle. Constructing fresh Anthropic and
# OpenAI clients each time leaked an httpx connection pool + SSL context per
# construction — ~960 cycles/day x N users — which is the slow climb from an
# ~850MB baseline to the container ceiling over a day or two.

def test_llm_clients_are_built_once_per_process():
    rr._CLIENTS = None                      # force a cold build
    assert rr._shared_llm_clients() is rr._shared_llm_clients()


def test_rerankers_share_the_same_client_objects():
    rr._CLIENTS = None
    a, b = rr.Reranker(), rr.Reranker()
    assert a._anthropic_client is b._anthropic_client
    assert a._openai_client is b._openai_client


# ── Tailoring is real spend and must be counted ───────────────────────────────

def test_tailor_spend_registers_against_the_budget_counter():
    """Tailoring is Sonnet spend that never touched the counter, so the number
    an operator read was not the number being billed."""
    from app.tailoring.tailor import _register_llm_spend
    before = rr._daily_finals.get("count", 0)
    _register_llm_spend("tailor_resume")
    assert rr._daily_finals["count"] == before + 1


def test_tailor_spend_never_raises(monkeypatch):
    """Accounting must not be able to fail a paying user's tailor."""
    from app.tailoring import tailor

    def boom():
        raise RuntimeError("counter exploded")
    monkeypatch.setattr(rr, "_register_final_call", boom)
    tailor._register_llm_spend("tailor_resume")   # must not raise


def test_tailor_abuse_cap_bounds_a_single_account():
    """At ~$0.045-0.17/tailor the old default of 150/day allowed $6.75-25.50
    from ONE account — multiples of the whole platform scoring budget."""
    assert settings.tailor_abuse_daily_cap > 0, "an unlimited cap is an unlimited bill"
    worst_case = settings.tailor_abuse_daily_cap * 0.17
    assert worst_case <= 5.0, f"cap allows ${worst_case:.2f}/user/day"


def test_resume_block_is_deterministic():
    resume = "PYTHON ML ENGINEER RESUME LINE\n" * 150
    assert rr._resume_context_block(resume) == rr._resume_context_block(resume)


# ── Per-job attempt ceiling ───────────────────────────────────────────────────
def test_repeated_failures_defer_job(monkeypatch):
    monkeypatch.setattr(settings, "scoring_fail_max_attempts", 3)
    for _ in range(2):
        sl._note_score_failure(42)
    assert sl._drop_deferred([42]) == [42]               # under the ceiling: retried
    sl._note_score_failure(42)                            # third strike
    assert sl._drop_deferred([42]) == []                  # deferred
    assert 42 not in sl._fail_counts


def test_success_clears_failure_state():
    sl._note_score_failure(7)
    sl._note_score_success(7)
    assert 7 not in sl._fail_counts and sl._drop_deferred([7]) == [7]


def test_deferral_expires():
    sl._deferred_until[9] = time.time() - 1
    assert sl._drop_deferred([9]) == [9]
    assert 9 not in sl._deferred_until                    # expired entries purged


# ── Scoring lane guards ───────────────────────────────────────────────────────
def _clean(session):
    for model in (Application, UserNotification, FunnelEvent, Job, UserProfile):
        session.exec(delete(model))
    session.commit()


def test_shared_pool_is_not_a_scorable_user():
    from app.discovery.pipeline import SHARED_POOL_USER
    with get_session() as session:
        _clean(session)
        session.add(Job(title="ML Engineer", company="C", location="R", remote=True,
                        description="d", source=JobSource.GREENHOUSE, external_id="s1",
                        url="https://x/s1", user_id=SHARED_POOL_USER,
                        first_seen=datetime.utcnow()))
        session.add(Job(title="ML Engineer", company="C", location="R", remote=True,
                        description="d", source=JobSource.GREENHOUSE, external_id="u1",
                        url="https://x/u1", user_id="ua", first_seen=datetime.utcnow()))
        session.commit()
    users = sl._scorable_user_ids()
    assert "ua" in users and SHARED_POOL_USER not in users


def test_cycle_skips_when_all_providers_down(monkeypatch):
    # Old fast-exit behavior applies only when the local fallback is opted out.
    monkeypatch.setattr(settings, "local_score_fallback", False)
    with get_session() as session:
        _clean(session)
        session.add(Job(title="ML Engineer", company="C", location="R", remote=True,
                        description="d", source=JobSource.GREENHOUSE, external_id="u1",
                        url="https://x/u1", user_id="ua", first_seen=datetime.utcnow()))
        session.commit()
    rr._mark_provider_down("anthropic")
    rr._mark_provider_down("openai")
    stats = sl.run_scoring_lane()
    assert stats.get("skipped") == "all LLM providers cooling down"
    assert stats["scored"] == 0


def test_cycle_proceeds_when_providers_down_with_local_fallback(monkeypatch):
    # Default behavior: providers cooling down is not a stall — the cycle runs
    # and Reranker.score() stamps local estimates instead.
    monkeypatch.setattr(settings, "local_score_fallback", True)
    with get_session() as session:
        _clean(session)
        session.commit()
    rr._mark_provider_down("anthropic")
    rr._mark_provider_down("openai")
    stats = sl.run_scoring_lane()
    assert stats.get("skipped") != "all LLM providers cooling down"


def test_cycle_skips_when_budget_exhausted(monkeypatch):
    monkeypatch.setattr(settings, "llm_daily_final_cap", 1)
    rr._daily_finals["day"] = datetime.utcnow().strftime("%Y-%m-%d")
    rr._daily_finals["count"] = 1
    stats = sl.run_scoring_lane()
    assert stats.get("skipped") == "LLM budget reached (hourly/daily cap)"


def test_global_cap_is_shared_fairly_round_robin(monkeypatch):
    """With the cap at 2 and two users queued, each user gets one slot — the old
    user-by-user fill gave both slots to whichever user came first."""
    with get_session() as session:
        _clean(session)
        for uid, ext in (("ua", 1), ("ua", 2), ("ub", 3), ("ub", 4)):
            session.add(Job(title="Senior ML Engineer", company=f"Co{ext}", location="R",
                            remote=True, description="LLMs", source=JobSource.GREENHOUSE,
                            external_id=str(ext), url=f"https://x/{ext}", user_id=uid,
                            first_seen=datetime.utcnow()))
        session.commit()

    class _FakeReranker:
        def __init__(self, profile=None, feedback=""):
            pass
        def has_prescore_backend(self):
            return False
        def has_dual(self):
            return False
        def score(self, resume, job, provider=None):
            return 78.0, "fit", [], {}

    class _G:
        is_ghost = False
        ghost_score = 0.0
        flags_json = None
        flags = []

    monkeypatch.setattr("app.matching.reranker.Reranker", _FakeReranker)
    monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: "resume")
    monkeypatch.setattr("app.matching.filters.score_ghost", lambda job, session: _G())
    monkeypatch.setattr(settings, "scoring_global_cap", 2)

    stats = sl.run_scoring_lane()
    assert stats["scored"] == 2
    assert stats["users"] == 2                            # one slot each, not 2+0


# ── Adoption extras cap ───────────────────────────────────────────────────────
def test_semantic_extras_bounded_by_budget(monkeypatch):
    from app.strategy import adoption

    captured = {}

    def _fake_extras(others, roles, user_id, need):
        captured["need"] = need
        return others[:need]

    monkeypatch.setattr(adoption, "_semantic_extras", _fake_extras)
    monkeypatch.setattr(settings, "adoption_semantic_enabled", True)
    monkeypatch.setattr(settings, "adoption_semantic_max_extras", 2)
    monkeypatch.setattr("app.discovery.title_filter.role_title_match",
                        lambda title, roles: False)      # nothing matches by title

    jobs = [_job(title=f"Applied Scientist {i}") for i in range(10)]
    picked = adoption._select_adoptable(jobs, ["ml engineer"], "ua", limit=400)
    assert captured["need"] == 2                          # budget, not 400 open slots
    assert len(picked) == 2


def test_semantic_extras_zero_budget_means_title_only(monkeypatch):
    from app.strategy import adoption
    monkeypatch.setattr(settings, "adoption_semantic_enabled", True)
    monkeypatch.setattr(settings, "adoption_semantic_max_extras", 0)
    monkeypatch.setattr(adoption, "_semantic_extras",
                        lambda others, roles, user_id, need: (_ for _ in ()).throw(
                            AssertionError("must not embed with zero budget")))
    monkeypatch.setattr("app.discovery.title_filter.role_title_match",
                        lambda title, roles: False)
    picked = adoption._select_adoptable([_job()], ["ml engineer"], "ua", limit=400)
    assert picked == []


# ── Cross-lane in-flight claim ────────────────────────────────────────────────
def test_inflight_claim_blocks_second_lane():
    from app.common import inflight
    inflight._inflight.clear()
    assert inflight.try_claim(101) is True
    assert inflight.try_claim(101) is False          # second lane blocked
    inflight.release(101)
    assert inflight.try_claim(101) is True           # free again after release
    inflight.release(101)


def test_inflight_context_manager_releases_on_exception():
    from app.common import inflight
    inflight._inflight.clear()
    with pytest.raises(ValueError):
        with inflight.claim(7) as ok:
            assert ok is True
            raise ValueError("boom")
    assert inflight.try_claim(7) is True             # released despite the error
    inflight.release(7)


def test_score_job_skips_job_claimed_by_another_lane(monkeypatch):
    from app.common import inflight
    inflight._inflight.clear()

    class _NeverCalled:
        def prescore(self, *a, **k):
            raise AssertionError("must not score a claimed job")
        def score(self, *a, **k):
            raise AssertionError("must not score a claimed job")
        def has_dual(self):
            return False

    ctx = sl._Ctx("resume", _NeverCalled(), True, 35)
    inflight.try_claim(999)                          # another lane owns it
    try:
        assert sl._score_job(999, ctx) is None       # skipped, zero LLM calls
    finally:
        inflight.release(999)


# ── Prescore memo (retry pays only for the step that failed) ─────────────────
def test_failed_final_reuses_memoized_prescore(monkeypatch):
    from app.common import inflight
    inflight._inflight.clear()
    sl._prescore_memo.clear()
    with get_session() as session:
        _clean(session)
        session.add(Job(title="ML Engineer", company="C", location="R", remote=True,
                        description="d", source=JobSource.GREENHOUSE, external_id="m1",
                        url="https://x/m1", user_id="ua", first_seen=datetime.utcnow()))
        session.commit()
        jid = session.exec(__import__("sqlmodel").select(Job.id)).first()
        jid = jid[0] if isinstance(jid, tuple) else jid

    calls = {"pre": 0, "final": 0}

    class _RK:
        def prescore(self, resume, job):
            calls["pre"] += 1
            return (60.0, "promising")               # advances past the gate
        def score(self, resume, job, provider=None):
            calls["final"] += 1
            if calls["final"] == 1:
                raise RuntimeError("overloaded")     # first final fails
            return 70.0, "fit", [], {}
        def has_dual(self):
            return False

    class _G:
        is_ghost = False
        ghost_score = 0.0
        flags_json = None
        flags = []

    monkeypatch.setattr("app.matching.filters.score_ghost", lambda job, session: _G())
    ctx = sl._Ctx("resume", _RK(), True, 35)

    assert sl._score_job(jid, ctx) is None           # attempt 1: final fails
    assert jid in sl._prescore_memo                  # Tier-1 result kept
    out = sl._score_job(jid, ctx)                    # attempt 2: retry
    assert out is not None and out[0] == "scored"
    assert calls["pre"] == 1                         # prescore paid ONCE, not twice
    assert calls["final"] == 2
    assert jid not in sl._prescore_memo              # cleaned up after success


# ── OpenAI prescore prefix size ───────────────────────────────────────────────
def test_prescore_prompt_resume_slice_crosses_openai_cache_minimum():
    resume = "x" * 10000
    prompt = rr._build_prescore_prompt(resume, _job())
    # résumé slice must be ≥4000 chars (~1000 tokens): with the ~200-token system
    # prompt that puts the static prefix past OpenAI's 1,024-token minimum.
    assert "x" * 4000 in prompt
    assert "x" * 4001 not in prompt                  # still bounded (cheap tier)
    assert prompt.index("<resume>") == 0             # résumé leads → static prefix


# ── Hourly smoothing cap ──────────────────────────────────────────────────────
def test_hourly_cap_blocks_burst(monkeypatch):
    monkeypatch.setattr(settings, "llm_daily_final_cap", 1000)
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 2)
    rk = _reranker_with(True, False)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_score_anthropic", lambda rb, jb:
                        '{"score": 80, "reason": "ok", "concerns": [], "breakdown": {}}')
    assert rk.score("resume", _job())[0] == 80.0
    assert rk.score("resume", _job())[0] == 80.0
    assert rr.llm_budget_exhausted()                     # hourly cap hit, daily far away
    with pytest.raises(RuntimeError, match="budget"):
        rk.score("resume", _job())


def test_hourly_cap_resets_next_hour(monkeypatch):
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 1)
    rr._hourly_finals["hour"] = "1999-01-01 00"          # stale hour
    rr._hourly_finals["count"] = 999
    assert not rr.llm_budget_exhausted()


def test_hourly_cap_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 0)
    monkeypatch.setattr(settings, "llm_daily_final_cap", 0)
    rr._hourly_finals["hour"] = datetime.utcnow().strftime("%Y-%m-%d %H")
    rr._hourly_finals["count"] = 10**9
    assert not rr.llm_budget_exhausted()


# ── Daily-quota 429 trips the breaker ─────────────────────────────────────────
_OPENAI_RPD_MSG = ("Error code: 429 - {'error': {'message': 'Rate limit reached for "
                   "gpt-4o-mini in organization org-X on requests per day (RPD): "
                   "Limit 10000, Used 10000, Requested 1.', 'type': 'requests', "
                   "'code': 'rate_limit_exceeded'}}")


def test_daily_quota_429_trips_breaker_in_score(monkeypatch):
    rk = _reranker_with(True, True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_score_openai", lambda rb, jb: (_ for _ in ()).throw(
        RuntimeError(_OPENAI_RPD_MSG)))
    monkeypatch.setattr(settings, "dual_score_enabled", False)
    monkeypatch.setattr(rk, "_score_anthropic", lambda rb, jb:
                        '{"score": 66, "reason": "ok", "concerns": [], "breakdown": {}}')
    score, *_ = rk.score("resume", _job(), provider="openai")
    assert score == 66.0                                 # Claude picked it up
    assert not rr.provider_available("openai")           # RPD exhaustion = breaker trip


def test_daily_quota_429_trips_breaker_in_prescore(monkeypatch):
    rk = _reranker_with(anthropic=False, openai=True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_prescore_openai", lambda prompt: (_ for _ in ()).throw(
        RuntimeError(_OPENAI_RPD_MSG)))
    assert rk.prescore("resume", _job()) is None
    assert not rr.provider_available("openai")


def test_transient_429_does_not_trip_breaker(monkeypatch):
    rk = _reranker_with(True, True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_score_anthropic", lambda rb, jb: (_ for _ in ()).throw(
        RuntimeError("Error code: 429 - rate_limit_error: too many requests, retry shortly")))
    monkeypatch.setattr(rk, "_score_openai", lambda rb, jb:
                        '{"score": 55, "reason": "ok", "concerns": [], "breakdown": {}}')
    score, *_ = rk.score("resume", _job(), provider="anthropic")
    assert score == 55.0
    assert rr.provider_available("anthropic")            # per-minute 429 = transient, no trip


# ── Anthropic prescores draw from the same budget as finals ───────────────────
def test_anthropic_prescore_counts_against_budget(monkeypatch):
    monkeypatch.setattr(settings, "llm_daily_final_cap", 2)
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 0)
    rk = _reranker_with(anthropic=True, openai=False)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)

    class _Msgs:
        @staticmethod
        def create(**kw):
            r = type("R", (), {})()
            r.content = [type("C", (), {"text": '{"score": 20, "reason": "off-role"}'})()]
            return r
    rk._anthropic_client = type("F", (), {"messages": _Msgs()})()

    assert rk.prescore("resume", _job())[0] == 20.0      # 1st Haiku prescore
    assert rk.prescore("resume", _job())[0] == 20.0      # 2nd — budget now full
    assert rr.llm_budget_exhausted()                     # prescores consumed it
    assert rk.prescore("resume", _job()) is None         # 3rd: skipped, no API call


def test_openai_prescore_does_not_touch_budget(monkeypatch):
    monkeypatch.setattr(settings, "llm_daily_final_cap", 1)
    rk = _reranker_with(anthropic=False, openai=True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_prescore_openai",
                        lambda prompt: '{"score": 25, "reason": "off-role"}')
    for _ in range(5):                                    # mini is pennies — never budgeted
        assert rk.prescore("resume", _job())[0] == 25.0
    assert not rr.llm_budget_exhausted()


# ── Per-plan daily final caps ────────────────────────────────────────────────
# The global LLM_DAILY_FINAL_CAP is a platform backstop; the ALLOCATION is
# per user and plan-tied (PLAN_LIMITS["finals_daily"]). A single global pool
# divided by N users means every signup thins every existing user's feed.

def test_finals_are_attributed_per_user():
    rr._register_final_call("user-a")
    rr._register_final_call("user-a")
    rr._register_final_call("user-b")
    assert rr.user_finals_today("user-a") == 2
    assert rr.user_finals_today("user-b") == 1
    # ...and still roll up into the global backstop counter.
    assert rr._daily_finals["count"] == 3


def test_unattributed_finals_still_count_globally():
    """A lane with no profile (no user_id) must not silently escape the backstop."""
    rr._register_final_call(None)
    assert rr._daily_finals["count"] == 1
    assert rr.user_finals_today(None) == 0


def test_user_counters_reset_on_day_roll():
    rr._register_final_call("user-a")
    assert rr.user_finals_today("user-a") == 1
    rr._daily_finals["day"] = "1999-01-01"      # simulate yesterday
    assert rr.user_finals_today("user-a") == 0  # rolled, not carried over


def _prewarm_reranker(exc: Exception):
    rk = _reranker_with(anthropic=True, openai=False)

    class _Msgs:
        @staticmethod
        def create(**kw):
            raise exc

    rk._anthropic_client = type("F", (), {"messages": _Msgs()})()
    return rk


def test_a_credit_error_on_the_free_prewarm_trips_the_breaker(caplog):
    """Production 2026-09-03: every scoring cycle's cache prewarm answered 400
    ('credit balance is too low') at DEBUG, so the log showed nothing and the
    lane kept paying Tier-1 prescores for finals that could not happen. The
    prewarm is the same outage score() trips the breaker for — trip it here,
    at the free call, and say so."""
    import logging
    rr._prewarm_warned[0] = 0.0
    rk = _prewarm_reranker(Exception(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API.'}}"))
    with caplog.at_level(logging.WARNING, logger="app.matching.reranker"):
        assert rk.prewarm_cache("resume text " * 50) is False
    assert not rr.provider_available("anthropic"), "the breaker must trip on a credit error"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("credit balance" in m for m in msgs), msgs
    assert any("marked DOWN" in m for m in msgs), msgs


def test_a_transient_prewarm_failure_warns_once_and_keeps_the_provider(caplog):
    import logging
    rr._prewarm_warned[0] = 0.0
    rk = _prewarm_reranker(Exception("Error code: 529 - overloaded_error"))
    with caplog.at_level(logging.WARNING, logger="app.matching.reranker"):
        assert rk.prewarm_cache("resume text " * 50) is False
        assert rk.prewarm_cache("resume text " * 50) is False
    assert rr.provider_available("anthropic"), "an overload is not a credit outage"
    warned = [r for r in caplog.records if "prewarm failed" in r.getMessage()]
    assert len(warned) == 1, "one warning per half hour, not one per cycle"


def test_remaining_finals_is_bounded_by_plan_allowance(monkeypatch):
    from app.db.models import PlanTier
    monkeypatch.setattr(sl, "_plan_finals_cap", lambda uid: 50)
    assert sl._remaining_finals_today("u", 40) == 40      # per-cycle cap binds
    for _ in range(30):
        rr._register_final_call("u")
    assert sl._remaining_finals_today("u", 40) == 20      # plan allowance binds
    for _ in range(20):
        rr._register_final_call("u")
    assert sl._remaining_finals_today("u", 40) == 0       # spent for the day
    assert PlanTier.PRO  # plan enum still resolvable


def test_plan_lookup_failure_fails_open(monkeypatch):
    """A billing hiccup must never stall scoring — no cap beats no feed."""
    def _boom(uid):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(sl, "_plan_finals_cap", _boom)
    with pytest.raises(RuntimeError):
        sl._plan_finals_cap("u")
    # The real helper swallows it and returns None → no per-user cap.
    monkeypatch.undo()
    monkeypatch.setattr("app.api.server._get_user_plan", _boom, raising=False)
    assert sl._plan_finals_cap("u") is None
    assert sl._remaining_finals_today("u", 40) == 40


def test_local_dev_user_has_no_plan_cap():
    assert sl._plan_finals_cap("local") is None
    assert sl._plan_finals_cap(None) is None


def test_every_plan_declares_a_finals_allowance():
    """A plan without finals_daily would silently fall through to uncapped."""
    from app.db.models import PLAN_LIMITS
    for tier, limits in PLAN_LIMITS.items():
        assert "finals_daily" in limits, tier
        assert limits["finals_daily"] > 0, tier


# ── Cache prewarm ────────────────────────────────────────────────────────────
# A cache entry is only readable once the response writing it starts streaming,
# so N concurrent calls sharing a prefix all miss and all pay the 1.25x write.

class _FakeAnthropic:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        class _R:
            content = []
            usage = None
        return _R()


def test_prewarm_uses_prefill_only_and_the_same_cached_prefix():
    r = rr.Reranker.__new__(rr.Reranker)
    r._profile, r._feedback, r._user_id = None, "", None
    fake = _FakeAnthropic()
    r._anthropic_client = fake

    assert r.prewarm_cache("RESUME TEXT") is True
    kw = fake.calls[0]
    # Prefill only: zero output tokens billed.
    assert kw["max_tokens"] == 0
    # Both prefix blocks are marked cacheable, exactly as _score_anthropic does —
    # a byte difference here and the workers miss the entry this just wrote.
    assert [b["cache_control"] for b in kw["system"]] == \
           [{"type": "ephemeral"}, {"type": "ephemeral"}]
    assert kw["system"][1]["text"] == rr._resume_context_block("RESUME TEXT", "")
    assert kw["model"] == settings.scoring_model


def test_prewarm_never_breaks_scoring():
    """Best-effort: a failed warm just means the first real call writes the cache."""
    class _Boom:
        messages = None
        def create(self, **kw):
            raise RuntimeError("overloaded")
    r = rr.Reranker.__new__(rr.Reranker)
    r._profile, r._feedback, r._user_id = None, "", None
    boom = _Boom()
    boom.messages = boom
    r._anthropic_client = boom
    assert r.prewarm_cache("RESUME") is False


def test_prewarm_noop_without_anthropic_client():
    r = rr.Reranker.__new__(rr.Reranker)
    r._profile, r._feedback, r._user_id = None, "", None
    r._anthropic_client = None
    assert r.prewarm_cache("RESUME") is False


# ── Tier-1 is not free: don't prescore once the budget is gone ───────────────

def test_prescore_gate_sits_at_the_adjacent_band_floor():
    """The effective gate is min(advance, shortlist), and 40 is the floor of the
    banded Tier-1 prompt's adjacent band (40-59): adjacent fits advance to
    Claude, stated-blocker jobs (<40) drain. See test_settings_defaults for the
    full rationale; this guard just keeps the pair coherent from the cost side."""
    assert settings.prescore_advance_threshold == 40
    assert settings.prescore_advance_threshold < settings.shortlist_score_threshold


def test_pulse_fast_path_checks_budget_before_spending(monkeypatch):
    import app.strategy.pulse_lane as pl
    monkeypatch.setattr(rr, "llm_budget_exhausted", lambda: True)
    # Exhausted budget → no resume load, no prescore, no Claude call.
    assert pl._fast_path_user("u", 10) == (0, 0, 0)


# ── Tier-1 score persistence ─────────────────────────────────────────────────
# The prescore used to be discarded for jobs that ADVANCE (kept only in a RAM
# memo for retry), so a row never carried both scores: drained jobs had only a
# prescore, advanced jobs only a final. That makes "is a stricter Tier-1 gate
# safe?" unanswerable — the jobs a higher gate would kill never get a final to
# compare against. Persist it on every path.

def test_stamp_job_persists_the_prescore():
    with get_session() as session:
        _clean(session)
        job = _job()
        job.user_id = "pre-user"
        session.add(job)
        session.commit()
        jid = job.id

    assert sl._stamp_job(jid, None, 82.0, "strong fit", None, prescore=71.0) is True

    with get_session() as session:
        row = session.get(Job, jid)
        assert row.rerank_score == 82.0
        assert row.prescore == 71.0      # BOTH scores on one row
        session.exec(delete(Job).where(Job.user_id == "pre-user"))
        session.commit()


def test_stamp_job_without_a_prescore_leaves_it_null():
    """Ghost-filtered and rule-filtered jobs never reach Tier-1 — they must not
    get a fabricated prescore."""
    with get_session() as session:
        _clean(session)
        job = _job()
        job.user_id = "pre-user"
        session.add(job)
        session.commit()
        jid = job.id

    assert sl._stamp_job(jid, None, 5.0, "Ghost filtered", None) is True

    with get_session() as session:
        row = session.get(Job, jid)
        assert row.rerank_score == 5.0
        assert row.prescore is None
        session.exec(delete(Job).where(Job.user_id == "pre-user"))
        session.commit()


def test_prescore_column_is_migrated_onto_existing_databases():
    """A new column is useless if init_db doesn't add it to live databases."""
    import inspect
    from app.db import init_db
    src = inspect.getsource(init_db)
    assert '("prescore", "FLOAT")' in src


# ── Tier-1 prompt structure (the v3 invariants) ──────────────────────────────
# Three live regress runs established two facts about how the cheap model reads
# this prompt: (1) it keyword-matches — "hybrid" ANYWHERE in the blocker bullet
# put a clean in-country hybrid job in the blocker band, even inside a negated
# parenthetical; (2) placement beats content — a rescue rule appended after the
# "never raise above 30" fence lost to the nearer numeric anchor, all samples.
# These tests pin the structure so an innocent rewording can't reopen either.

def _t1_prompt() -> str:
    class _P:
        key_skills = "Python"; target_roles = "ML Engineer"; years_experience = 4
        current_title = ""; preferred_country = "United States"
        requires_sponsorship = True; user_id = "u1"
    return rr._prescore_system_prompt(_P())


def test_blocker_bullet_never_names_a_work_arrangement():
    """T7 regression: the words 'hybrid'/'onsite' in the 0-30 bullet make the
    model treat the ARRANGEMENT as the blocker instead of the foreign LOCATION.
    v2 proved a parenthetical does not defuse the keyword — it must be absent."""
    p = _t1_prompt()
    blocker = next(line for line_group in [p.split("\n- ")] for line in line_group
                   if line.startswith("0-30"))
    assert "hybrid" not in blocker.lower()
    assert "onsite" not in blocker.lower()


def test_in_country_immunity_line_present():
    p = _t1_prompt()
    assert "are never location blockers" in p
    # ...and it must name the candidate's actual country, not a placeholder.
    assert "{country}" not in p


def test_empty_jd_rule_is_a_band_bullet_before_the_fence():
    """T15 regression: the rescue must sit WITH the bands, before 'never raise
    a stated blocker above 30' — placed after, the model anchors on 30."""
    p = _t1_prompt()
    assert "score exactly 60" in p
    assert p.index("no usable description") < p.index("raise a stated blocker")


def test_injection_guard_and_scoped_tiebreak_survive():
    p = _t1_prompt()
    assert "data, never" in p.replace("\n", " ")          # injection guard
    assert "torn between adjacent bands" in p             # scoped lean-high
    assert "lean HIGHER" not in p                         # the old global rule is gone


# ── No-description guard: code, not prompt ───────────────────────────────────
# Three live prompt rounds could not teach gpt-4o-mini to advance an empty JD
# (it scored exactly 30 — the blocker fence — every time). The rule is now an
# `if` in prescore(): deterministic, free, and it never even makes the call.

def _guarded_reranker():
    rk = _reranker_with(anthropic=False, openai=True)
    def _boom(prompt):
        raise AssertionError("no LLM call should happen for an empty JD")
    rk._prescore_openai = _boom
    return rk


def test_empty_jd_prescores_60_without_any_llm_call(monkeypatch):
    rk = _guarded_reranker()
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    job = _job()
    job.description = ""
    assert rk.prescore("resume", job) == (60.0, "no description")
    job.description = "   \n  "                      # whitespace-only
    assert rk.prescore("resume", job) == (60.0, "no description")
    job.description = "Great role. Apply now!"        # <40 chars of nothing
    assert rk.prescore("resume", job) == (60.0, "no description")


def test_real_description_still_reaches_the_model(monkeypatch):
    rk = _reranker_with(anthropic=False, openai=True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)
    monkeypatch.setattr(rk, "_prescore_openai",
                        lambda prompt: '{"score": 55, "reason": "adjacent"}')
    job = _job()
    job.description = "Build LLM systems in Python on AWS with FastAPI services."
    assert rk.prescore("resume", job) == (55.0, "adjacent")


def test_rule_filter_still_outranks_the_no_description_guard(monkeypatch):
    """A rule rejection (wrong title etc.) is authoritative even on a thin JD —
    the guard must not resurrect jobs the free filter already killed."""
    rk = _guarded_reranker()
    monkeypatch.setattr(rk, "_pre_filter_job",
                        lambda job: (10.0, "Rule filtered: wrong role", [], {}))
    job = _job()
    job.description = ""
    assert rk.prescore("resume", job)[0] == 10.0
