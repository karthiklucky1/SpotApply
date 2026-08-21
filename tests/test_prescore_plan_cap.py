"""Tier-1 prescores must not eat the user's per-plan FINALS allowance.

Production, 2026-08-14 → 08-21: the OpenAI key ran out of credits, so every
Tier-1 prescore fell back to Anthropic (Haiku). `_prescore_anthropic` charged
each of those to the USER's finals counter (`_register_final_call(user_id)`),
so a PRO user's 50 finals/day were consumed by ~45 cheap prescores + ~5 real
finals within 15 minutes of 00:00 UTC, and the scoring lane + pulse fast path
then silently skipped that user for the rest of the day (`plan_capped`) —
800+ on-role jobs sat unscored while the board got 5–6 shortlists a day.

The GLOBAL hourly/daily backstop must still see Anthropic prescores (the
Jul-15 incident: Tier-1 on Haiku, uncapped, outspent the finals). Only the
per-user PLAN allowance is finals-only.
"""
from __future__ import annotations

from app.config import settings
from app.matching import reranker as rr
from app.matching.reranker import Reranker
from app.strategy import scoring_lane as sl


def _job():
    from app.db.models import Job, JobSource
    return Job(id=1, source=JobSource.GREENHOUSE, external_id="x", company="Acme",
               title="Software Engineer", location="Remote", remote=True,
               url="https://example.com/j", description="Python backend " * 20)


def _haiku_reranker(user_id="user-a"):
    profile = type("P", (), {"user_id": user_id})()
    rk = Reranker(profile=profile)
    rk._openai_client = None            # OpenAI key out of credits / not configured
    rk._anthropic_client = object()     # Haiku is what Tier-1 falls back to

    class _Msgs:
        @staticmethod
        def create(**kw):
            r = type("R", (), {})()
            r.content = [type("C", (), {"text": '{"score": 20, "reason": "off-role"}'})()]
            return r
    rk._anthropic_client = type("F", (), {"messages": _Msgs()})()
    rk._pre_filter_job = lambda job: None
    return rk


def test_anthropic_prescore_does_not_consume_the_users_plan_finals(monkeypatch):
    monkeypatch.setattr(settings, "llm_daily_final_cap", 0)
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 0)
    monkeypatch.setattr(sl, "_plan_finals_cap", lambda uid: 50)   # PRO
    rk = _haiku_reranker("user-a")

    assert sl._remaining_finals_today("user-a", 40) == 40
    for _ in range(50):
        assert rk.prescore("resume", _job())[0] == 20.0   # 50 Tier-1 drains, $0.0005 each

    # No FINAL has been scored yet — the plan allowance must be untouched.
    assert rr.user_finals_today("user-a") == 0
    assert sl._remaining_finals_today("user-a", 40) == 40


def test_anthropic_prescore_still_counts_toward_the_global_backstop(monkeypatch):
    """The platform-wide runaway guard keeps seeing Haiku prescores (Jul-15 rule)."""
    monkeypatch.setattr(settings, "llm_daily_final_cap", 2)
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 0)
    rk = _haiku_reranker("user-a")
    assert rk.prescore("resume", _job())[0] == 20.0
    assert rk.prescore("resume", _job())[0] == 20.0
    assert rr.llm_budget_exhausted()
    assert rk.prescore("resume", _job()) is None           # fail-open, no call


def test_prescores_have_their_own_bounded_per_user_allowance(monkeypatch):
    """Not charging prescores as finals must not make Tier-1 unbounded per user:
    they get their own allowance (finals_daily × prescore_budget_multiplier)."""
    monkeypatch.setattr(settings, "llm_daily_final_cap", 0)
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 0)
    monkeypatch.setattr(settings, "prescore_budget_multiplier", 2)   # 50 × 2 = 100
    monkeypatch.setattr(sl, "_plan_finals_cap", lambda uid: 50)
    rk = _haiku_reranker("user-a")
    for _ in range(100):
        rk.prescore("resume", _job())
    assert rr.user_prescores_today("user-a") == 100
    assert sl._remaining_finals_today("user-a", 40) == 0      # prescore allowance spent
    assert sl._remaining_finals_today("user-b", 40) == 40     # someone else: untouched


def test_openai_prescores_are_not_capped_per_user(monkeypatch):
    """The allowance bounds the ANTHROPIC fallback, not the cheap path.

    A gpt-4o-mini prescore is ~$0.0002 — one user's entire daily Tier-1 volume
    is well under $1/month — so capping it buys nothing and would let an OpenAI
    outage (or any burst) throttle the feed for the rest of the UTC day.
    """
    monkeypatch.setattr(settings, "llm_daily_final_cap", 0)
    monkeypatch.setattr(settings, "llm_hourly_final_cap", 0)
    monkeypatch.setattr(settings, "prescore_budget_multiplier", 2)   # 50 × 2 = 100
    monkeypatch.setattr(sl, "_plan_finals_cap", lambda uid: 50)

    profile = type("P", (), {"user_id": "user-a"})()
    rk = Reranker(profile=profile)
    rk._anthropic_client = None

    class _Completions:
        @staticmethod
        def create(**kw):
            msg = type("M", (), {"content": '{"score": 20, "reason": "off-role"}'})()
            return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()
    rk._openai_client = type("F", (), {
        "chat": type("Ch", (), {"completions": _Completions()})()})()
    rk._pre_filter_job = lambda job: None

    for _ in range(200):                       # 2x the allowance the Haiku path has
        assert rk.prescore("resume", _job())[0] == 20.0
    assert rr.user_prescores_today("user-a") == 0
    assert sl._remaining_finals_today("user-a", 40) == 40   # feed untouched


def test_plan_capped_cycle_is_logged_not_silent(monkeypatch, caplog):
    """A cycle where every scorable user is plan-capped used to `return stats`
    before the log line, so a day-long stall left no trace in the logs."""
    import logging
    from app.matching.finals_budget import Allowance
    monkeypatch.setattr(sl, "_expire_stale_unscored", lambda: 0)
    monkeypatch.setattr(sl, "_scorable_user_ids", lambda: ["user-a", "user-b"])
    monkeypatch.setattr(sl, "_finals_allowance",
                        lambda uid, cap: Allowance(0, 40, "weekly budget spent"))
    sl._last_capped_log[0] = float("-inf")
    with caplog.at_level(logging.WARNING, logger="app.strategy.scoring_lane"):
        stats = sl._run_scoring_cycle(None)
    assert stats["plan_capped_users"] == 2
    assert any("no finals allowance" in r.getMessage() for r in caplog.records)
