"""Option A — dual-provider final scoring: 60/40 routing, shared rubric, calibration.

The prescore→final cascade is a relay (GPT drains, Claude finalises), so the two
can't split one job. Instead we split the FINAL score across providers by job id.
These tests exercise the router + reranker plumbing with fake LLM clients — no keys.
"""
from __future__ import annotations

import types

import pytest

import app.matching.reranker as rr
import app.strategy.scoring_lane as sl
from app.config import settings
from app.db.models import Job, JobSource


def _job(title="Senior ML Engineer"):
    return Job(title=title, company="Acme", location="Remote", remote=True,
               description="Build LLM systems in Python.", source=JobSource.GREENHOUSE,
               external_id="x1", url="https://x/1")


def _reranker_with(anthropic=True, openai=True):
    """A Reranker with fake clients, bypassing real key/init."""
    rk = rr.Reranker.__new__(rr.Reranker)
    rk._profile = None
    rk._feedback = ""
    rk._anthropic_client = object() if anthropic else None
    rk._openai_client = object() if openai else None
    rk._active_backend = "anthropic" if anthropic else ("openai" if openai else None)
    return rk


# ── has_dual ──────────────────────────────────────────────────────────────────
def test_has_dual_requires_both_providers():
    assert _reranker_with(True, True).has_dual() is True
    assert _reranker_with(True, False).has_dual() is False
    assert _reranker_with(False, True).has_dual() is False


# ── backend ordering honours the requested provider ────────────────────────────
def test_score_backends_routes_to_requested_provider():
    rk = _reranker_with(True, True)
    names = lambda prov: [n for n, _ in rk._score_backends(prov)]
    assert names("openai")[0] == "openai"      # GPT first, Claude fallback
    assert names("openai")[1] == "anthropic"
    assert names("anthropic")[0] == "anthropic"
    assert names(None)[0] == "anthropic"        # default: active backend first


def test_score_backends_single_provider_has_no_fallback():
    rk = _reranker_with(anthropic=False, openai=True)
    assert [n for n, _ in rk._score_backends("anthropic")] == ["openai"]  # only what exists


# ── the OpenAI final scorer uses the full model in dual mode ───────────────────
def test_dual_mode_uses_full_openai_model(monkeypatch):
    monkeypatch.setattr(settings, "dual_score_enabled", True)
    rk = _reranker_with(True, True)
    fn = dict(rk._score_backends("openai"))["openai"]
    assert fn == rk._score_openai_final          # full model, not the mini fallback
    monkeypatch.setattr(settings, "dual_score_enabled", False)
    fn2 = dict(rk._score_backends("openai"))["openai"]
    assert fn2 == rk._score_openai               # cheap fallback when dual off


# ── calibration offset only shifts GPT, only in dual mode ──────────────────────
def test_calibration_offset_applied_to_gpt_only(monkeypatch):
    rk = _reranker_with(True, True)
    monkeypatch.setattr(settings, "dual_score_enabled", True)
    monkeypatch.setattr(settings, "dual_score_openai_offset", 5.0)
    base = (70.0, "ok", [], {})
    assert rk._calibrate("openai", base)[0] == 75.0     # GPT nudged up
    assert rk._calibrate("anthropic", base)[0] == 70.0  # Claude untouched
    # Clamped to 0-100.
    assert rk._calibrate("openai", (98.0, "", [], {}))[0] == 100.0
    monkeypatch.setattr(settings, "dual_score_openai_offset", 0.0)
    assert rk._calibrate("openai", base)[0] == 70.0     # no offset → no change


def test_score_routes_and_calibrates(monkeypatch):
    """Full path: score(provider='openai') calls the GPT backend and calibrates."""
    monkeypatch.setattr(settings, "dual_score_enabled", True)
    monkeypatch.setattr(settings, "dual_score_openai_offset", 4.0)
    rk = _reranker_with(True, True)
    monkeypatch.setattr(rk, "_pre_filter_job", lambda job: None)  # skip rule filter
    monkeypatch.setattr(rk, "_score_openai_final",
                        lambda prompt: '{"score": 80, "reason": "fit", "concerns": [], "breakdown": {}}')
    monkeypatch.setattr(rk, "_score_anthropic",
                        lambda prompt: (_ for _ in ()).throw(AssertionError("Claude should not be called")))
    score, reason, concerns, bd = rk.score("resume", _job(), provider="openai")
    assert score == 84.0   # 80 + 4 calibration
    assert reason == "fit"


# ── the 60/40 router ────────────────────────────────────────────────────────────
class _DualCtx:
    class _RK:
        def has_dual(self):
            return True
    reranker = _RK()


def test_pick_provider_splits_by_share(monkeypatch):
    monkeypatch.setattr(settings, "dual_score_enabled", True)
    monkeypatch.setattr(settings, "dual_score_claude_share", 0.6)
    ctx = _DualCtx()
    picks = [sl._pick_provider(jid, ctx) for jid in range(100)]
    claude = picks.count("anthropic")
    gpt = picks.count("openai")
    assert claude == 60 and gpt == 40          # exact 60/40 over 100 ids
    # Deterministic: same id → same provider.
    assert sl._pick_provider(7, ctx) == sl._pick_provider(7, ctx)


def test_pick_provider_noop_when_single_provider(monkeypatch):
    monkeypatch.setattr(settings, "dual_score_enabled", True)

    class _SingleCtx:
        class _RK:
            def has_dual(self):
                return False
        reranker = _RK()

    assert sl._pick_provider(3, _SingleCtx()) is None


def test_pick_provider_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "dual_score_enabled", False)
    assert sl._pick_provider(3, _DualCtx()) is None
