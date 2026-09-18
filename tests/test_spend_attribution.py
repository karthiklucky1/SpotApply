"""Provider attribution + token-metered LLM spend.

Production, 2026-09-14..16: Anthropic answered every call with 400 "Your credit
balance is too low" for three days and billed $0; OpenAI served every final
and its spend rose to $3.20/day. The app's own books said otherwise — the
scoring cycle logged by_claude=1,335/1,313/988 with by_gpt=0, and the ledger
booked 1,534/1,534/1,136 `score_final` rows a day at the Haiku flat rate. Over
seven days the estimate was $68.60 against $17.77 actually billed (3.9x):
prescore was 83% of it at a never-metered $0.001/call, and the lanes recorded
67,280 prescore+final calls while OpenAI saw 41,378 requests, because ghost
stamps, rule pre-filters and the empty-JD guard were each booked as a Tier-1
call that never happened.

Three rules these tests pin:
  1. Attribution names the backend that ANSWERED, never the one requested.
  2. A call that did not happen records nothing; one that did records its
     provider, model and token usage, priced from the per-model table.
  3. There is ONE writer (the Reranker, buffered; the lanes only flush).

Every row this file writes carries the `spendtest-` prefix and is deleted by
that prefix — never a wholesale delete.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime

import pytest
from sqlmodel import delete, select

import app.analytics.spend as spend
import app.matching.reranker as rr
import app.strategy.pulse_lane as pl
import app.strategy.scoring_lane as sl
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, Job, JobSource, LlmSpend, UserNotification

PREFIX = "spendtest-"


@pytest.fixture(autouse=True)
def _isolate():
    """Breaker state + the spend buffer are process-global; my ledger rows are
    mine alone (prefix-scoped)."""
    def _reset():
        rr._provider_down_until.clear()
        rr._provider_down_since.clear()
        rr._provider_last_error.clear()
        spend._BUFFER.clear()
        with get_session() as session:
            session.exec(delete(LlmSpend).where(LlmSpend.user_id.like(f"{PREFIX}%")))  # type: ignore[attr-defined]
            session.exec(delete(Application).where(Application.user_id.like(f"{PREFIX}%")))  # type: ignore[attr-defined]
            session.exec(delete(UserNotification).where(UserNotification.user_id.like(f"{PREFIX}%")))  # type: ignore[attr-defined]
            session.exec(delete(Job).where(Job.user_id.like(f"{PREFIX}%")))  # type: ignore[attr-defined]
            session.commit()
    _reset()
    yield
    _reset()


def _job(jid=None, description="Build LLM systems in Python: FastAPI services, PyTorch "
                               "training pipelines, and PostgreSQL data layers on AWS."):
    j = Job(title="Senior ML Engineer", company="Acme", location="Remote", remote=True,
            description=description, source=JobSource.GREENHOUSE,
            external_id="spend-x1", url="https://x/spend-1")
    if jid is not None:
        j.id = jid
    return j


def _reranker(user_id, anthropic=None, openai=None):
    """A Reranker with the given fake CLIENTS (not monkeypatched methods) so
    the real backend methods — the ones that record spend — run."""
    rk = rr.Reranker.__new__(rr.Reranker)
    rk._profile = None
    rk._feedback = ""
    rk._user_id = user_id
    rk._anthropic_client = anthropic
    rk._openai_client = openai
    rk._active_backend = "anthropic" if anthropic is not None else ("openai" if openai is not None else None)
    rk._pre_filter_job = lambda job: None
    return rk


class _CreditError(Exception):
    pass


class _AnthropicBroke:
    """messages.create raises the exact production error."""
    def __init__(self):
        self.messages = self
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        raise _CreditError(
            "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
            "'message': 'Your credit balance is too low to access the Anthropic API.'}}")


class _OpenAIOk:
    """chat.completions.create answers with a JSON verdict AND real-looking usage."""
    def __init__(self, text, *, model="gpt-4o-mini-2024-07-18", prompt=3000, cached=2000, out=100):
        self.text, self.model_id = text, model
        self.prompt, self.cached, self.out = prompt, cached, out
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.calls += 1
        details = type("D", (), {"cached_tokens": self.cached})()
        usage = type("U", (), {"prompt_tokens": self.prompt, "completion_tokens": self.out,
                               "prompt_tokens_details": details})()
        msg = type("M", (), {"content": self.text})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()],
                              "usage": usage, "model": self.model_id})()


def _rows(uid):
    with get_session() as session:
        return session.exec(select(LlmSpend).where(LlmSpend.user_id == uid)).all()


# ── 1. The fallback final is attributed to the backend that answered ─────────

def test_openai_served_final_is_booked_to_openai_at_mini_prices(monkeypatch, caplog):
    monkeypatch.setattr(settings, "dual_score_enabled", False)
    uid = f"{PREFIX}u1"
    anth, oai = _AnthropicBroke(), _OpenAIOk('{"score": 70, "reason": "ok", "concerns": [], "breakdown": {}}')
    rk = _reranker(uid, anthropic=anth, openai=oai)

    with caplog.at_level(logging.WARNING, logger="app.matching.reranker"):
        score, reason, concerns, breakdown, meta = rk.score_with_meta("resume", _job())

    assert score == 70.0 and anth.calls == 1 and oai.calls == 1
    assert meta["provider"] == "openai", "attribution must name the backend that ANSWERED"
    assert meta["model"] == "gpt-4o-mini-2024-07-18"
    # OpenAI's prompt_tokens INCLUDES the cached ones: 3000 prompt / 2000 cached
    # -> 1000 uncached input + 2000 cache_read.
    assert meta["usage"] == {"input": 1000, "output": 100, "cache_read": 2000, "cache_write": 0}
    # score() stays the 4-tuple every other caller relies on.
    assert len(rk.score("resume", _job())) == 4

    # The breaker tripped for anthropic and the ledger knows who served it.
    assert not rr.provider_available("anthropic") and rr.provider_available("openai")
    assert spend.flush_llm_spend() >= 1
    rows = [r for r in _rows(uid) if r.kind == "score_final"]
    assert len(rows) == 1, [(r.kind, r.provider, r.model) for r in _rows(uid)]
    row = rows[0]
    assert row.provider == "openai" and row.model == "gpt-4o-mini-2024-07-18"
    assert row.calls == 2 and row.metered_calls == 2
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (2000, 200, 4000)
    # Priced at MINI rates from real tokens, per call:
    #   1000 x 0.15 + 2000 x 0.075 + 100 x 0.60 = 150 + 150 + 60 = 360 uUSD
    per_call = (1000 * 0.15 + 2000 * 0.075 + 100 * 0.60) / 1e6
    assert abs(row.est_cost_usd - 2 * per_call) < 1e-12
    assert abs(row.est_cost_usd / 2 - spend.EST_COST_PER_CALL["score_final"]) > 1e-4, \
        "must not be the Haiku flat rate the audit found"
    # And nothing was booked to anthropic — it never answered.
    assert not any(r.provider == "anthropic" for r in _rows(uid))


def test_anthropic_served_final_records_cache_tiers(monkeypatch):
    uid = f"{PREFIX}u2"

    class _Anth:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            usage = type("U", (), {"input_tokens": 400, "output_tokens": 120,
                                   "cache_read_input_tokens": 5000,
                                   "cache_creation_input_tokens": 0})()
            content = [type("C", (), {"text": '{"score": 81, "reason": "fit", "concerns": [], "breakdown": {}}'})()]
            return type("R", (), {"content": content, "usage": usage,
                                  "model": "claude-haiku-4-5-20251001"})()

    rk = _reranker(uid, anthropic=_Anth(), openai=None)
    *_, meta = rk.score_with_meta("resume", _job())
    assert meta["provider"] == "anthropic"
    assert meta["usage"] == {"input": 400, "output": 120, "cache_read": 5000, "cache_write": 0}
    spend.flush_llm_spend()
    (row,) = [r for r in _rows(uid) if r.kind == "score_final"]
    assert row.provider == "anthropic" and row.model.startswith("claude-haiku-4-5")
    expected = (400 * 1.00 + 120 * 5.00 + 5000 * 0.10) / 1e6
    assert abs(row.est_cost_usd - expected) < 1e-12
    assert row.metered_calls == 1


# ── 2. Short-circuits that make no API call record nothing ───────────────────

def _never_called():
    class _Boom:
        def __init__(self):
            self.chat = self
            self.completions = self
            self.messages = self

        def create(self, **kw):
            raise AssertionError("no API call should happen on a short-circuit")
    return _Boom()

def test_prescore_rule_prefilter_records_no_spend():
    rk = _reranker(f"{PREFIX}u3", openai=_never_called())
    rk._pre_filter_job = lambda job: (10.0, "Rule filtered: wrong role", [], {})
    assert rk.prescore("resume", _job())[0] == 10.0
    assert spend.pending_spend() == {}, "a rule rejection is not a Tier-1 call"


def test_prescore_empty_description_guard_records_no_spend():
    rk = _reranker(f"{PREFIX}u4", openai=_never_called())
    assert rk.prescore("resume", _job(description="")) == (60.0, "no description")
    assert rk.prescore("resume", _job(description="Great role. Apply now!")) == (60.0, "no description")
    assert spend.pending_spend() == {}


def test_prescore_that_really_calls_is_metered_to_its_provider():
    uid = f"{PREFIX}u5"
    oai = _OpenAIOk('{"score": 25, "reason": "off-role"}', prompt=1200, cached=1024, out=20)
    rk = _reranker(uid, openai=oai)
    assert rk.prescore("resume", _job()) == (25.0, "off-role")
    snap = spend.pending_spend()
    assert list(snap) == [(uid, spend._utc_day(), "score_prescore", "openai", "gpt-4o-mini-2024-07-18")]
    b = snap[(uid, spend._utc_day(), "score_prescore", "openai", "gpt-4o-mini-2024-07-18")]
    assert b["metered_calls"] == 1 and b["flat_calls"] == 0
    assert b["usage"] == {"input": 176, "output": 20, "cache_read": 1024, "cache_write": 0}


def test_rule_final_and_ghost_drain_record_no_spend_in_the_lane(monkeypatch):
    """The 26K-call gap: the lane booked a prescore for every 'drained' item,
    ghost stamps included, and a final for every 'scored' item, rule stamps
    included. Neither made an API call; neither may reach the ledger."""
    uid = f"{PREFIX}lane-ghost"
    ids = []
    with get_session() as session:
        for ext in ("spend-ghost", "spend-rule-t1", "spend-rule-t2"):
            j = _job()
            j.user_id, j.external_id = uid, ext
            session.add(j)
            session.commit()
            session.refresh(j)
            ids.append(j.id)
    gid, rid_t1, rid_t2 = ids

    class _Ghost:
        is_ghost, ghost_score, flags_json, flags = True, 0.9, "[]", ["stale_repost"]

    class _Clean:
        is_ghost, ghost_score, flags_json, flags = False, 0.0, None, []

    monkeypatch.setattr("app.matching.filters.score_ghost",
                        lambda job, session: _Ghost() if job.id == gid else _Clean())
    rk = _reranker(uid, openai=_never_called())
    rk._pre_filter_job = lambda job: (10.0, "Rule filtered: wrong role", [], {})

    # Tier-1 on: the ghost stamp and the rule rejection both drain before any
    # API call — the two "drained" items the lane used to bill as prescores.
    ctx_t1 = sl._Ctx("resume", rk, use_prescore=True, gate=40, spend_gate=40)
    assert sl._score_job_owned(gid, ctx_t1) == ("drained", gid, None, None)
    assert sl._score_job_owned(rid_t1, ctx_t1) == ("drained", rid_t1, None, None)
    # Tier-1 off: the rule verdict comes out of score_with_meta as a "rule"
    # final — the "scored" item the lane used to bill as a Claude final.
    ctx_t2 = sl._Ctx("resume", rk, use_prescore=False, gate=40, spend_gate=40)
    out = sl._score_job_owned(rid_t2, ctx_t2)
    assert out is not None and out[0] == "scored" and out[3] == "rule"

    assert spend.pending_spend() == {}
    assert spend.flush_llm_spend() == 0
    assert _rows(uid) == []


# ── 3. Price arithmetic ──────────────────────────────────────────────────────

def test_estimate_cost_arithmetic_including_cache_tiers():
    # Haiku: every tier priced separately, per million tokens.
    u = {"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000, "cache_write": 1_000_000}
    assert abs(spend.estimate_cost("claude-haiku-4-5-20251001", u) - (1.00 + 5.00 + 0.10 + 1.25)) < 1e-9
    assert abs(spend.estimate_cost("claude-sonnet-4-5", u) - (3.00 + 15.00 + 0.30 + 3.75)) < 1e-9
    # OpenAI: cached input at half price, no write charge.
    assert abs(spend.estimate_cost("gpt-4o-mini", u) - (0.15 + 0.60 + 0.075 + 0.0)) < 1e-9
    assert abs(spend.estimate_cost("gpt-4o-2024-08-06", u) - (2.50 + 10.00 + 1.25 + 0.0)) < 1e-9
    # Longest prefix wins: a dated mini snapshot is mini, not gpt-4o.
    assert spend.price_for_model("gpt-4o-mini-2024-07-18") is spend.PRICES_PER_MTOK["gpt-4o-mini"]
    assert spend.price_for_model("gpt-4o-2024-08-06") is spend.PRICES_PER_MTOK["gpt-4o"]
    # Partial usage is zero-filled; an unpriced model is None, not a guess.
    assert spend.estimate_cost("gpt-4o-mini", {"input": 1000}) == pytest.approx(0.00015)
    assert spend.estimate_cost("some-model-nobody-priced", u) is None
    assert spend.estimate_cost(None, u) is None
    assert spend.normalize_usage(None) == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert spend.normalize_usage({"input": "12", "output": None}) == \
        {"input": 12, "output": 0, "cache_read": 0, "cache_write": 0}


def test_unpriced_model_falls_back_to_flat_and_is_not_counted_as_metered():
    uid = f"{PREFIX}u6"
    spend.record_llm_spend(uid, "score_final", provider="openai", model="o9-preview",
                           usage={"input": 10, "output": 10})
    (row,) = _rows(uid)
    assert row.est_cost_usd == pytest.approx(spend.EST_COST_PER_CALL["score_final"])
    assert row.metered_calls == 0 and row.calls == 1
    assert row.input_tokens == 10, "tokens are still kept for when a price arrives"


# ── 4. Scoring-lane stats attribute by the SERVED provider ───────────────────

def _seed_lane_jobs(uid, n):
    ids = []
    with get_session() as session:
        for i in range(n):
            j = Job(title="Senior ML Engineer", company=f"Co{i}", location="Remote", remote=True,
                    description="LLMs in Python on AWS", source=JobSource.GREENHOUSE,
                    external_id=f"spend-lane-{i}", url=f"https://x/spend-lane-{i}",
                    user_id=uid, first_seen=datetime.utcnow(), posted_at=datetime.utcnow())
            session.add(j)
            session.commit()
            session.refresh(j)
            ids.append(j.id)
    return ids


def _drive_cycle(monkeypatch, uid, fake_cls):
    from app.matching.finals_budget import Allowance
    monkeypatch.setattr("app.matching.reranker.Reranker", fake_cls)
    monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: "resume")

    class _G:
        is_ghost, ghost_score, flags_json, flags = False, 0.0, None, []
    monkeypatch.setattr("app.matching.filters.score_ghost", lambda job, session: _G())
    monkeypatch.setattr(sl, "_scorable_user_ids", lambda: [uid])
    monkeypatch.setattr(sl, "_finals_allowance", lambda u, cap: Allowance(10, 40, "fill"))
    monkeypatch.setattr(sl, "_expire_stale_unscored",
                        lambda **kw: {"total": 0, "queue_stale": 0, "ancient_posting": 0, "stopped": ""})
    return sl._run_scoring_cycle(None)


def test_cycle_stats_count_an_openai_served_final_as_by_gpt_not_by_claude(monkeypatch):
    uid = f"{PREFIX}lane-gpt"
    _seed_lane_jobs(uid, 3)

    class _Fake:
        """Dual mode is off, so the lane asks for provider=None — exactly the
        case the old `in (None, "anthropic")` test miscounted."""
        def __init__(self, profile=None, feedback=""):
            pass
        def has_prescore_backend(self):
            return False
        def has_dual(self):
            return False
        def score(self, resume, job, provider=None):
            raise AssertionError("the lane must prefer score_with_meta when it exists")
        def score_with_meta(self, resume, job, provider=None):
            assert provider is None
            return 50.0, "served by the fallback", [], {}, {
                "provider": "openai", "model": "gpt-4o-mini",
                "usage": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0}}

    stats = _drive_cycle(monkeypatch, uid, _Fake)
    assert stats["scored"] == 3
    assert stats["by_gpt"] == 3
    assert stats["by_claude"] == 0
    assert stats["by_local"] == 0 and stats["by_rule"] == 0


def test_cycle_stats_count_rule_and_local_finals_separately(monkeypatch):
    uid = f"{PREFIX}lane-mix"
    ids = _seed_lane_jobs(uid, 2)

    class _Fake:
        def __init__(self, profile=None, feedback=""):
            pass
        def has_prescore_backend(self):
            return False
        def has_dual(self):
            return False
        def score_with_meta(self, resume, job, provider=None):
            if job.id == ids[0]:
                return 10.0, "Rule filtered: wrong role", [], {}, {"provider": "rule", "model": None, "usage": {}}
            return 44.0, f"{rr.LOCAL_REASON_PREFIX} (no LLM provider active)", [], {}, \
                {"provider": "local", "model": "cross-encoder", "usage": {}}

    stats = _drive_cycle(monkeypatch, uid, _Fake)
    assert stats["scored"] == 2
    assert (stats["by_rule"], stats["by_local"], stats["by_claude"], stats["by_gpt"]) == (1, 1, 0, 0)


def test_legacy_score_only_fake_is_not_folded_into_by_claude(monkeypatch):
    """A scorer that reports no meta must not default to Claude — that default
    IS the miscount. It is counted, just not as a provider."""
    uid = f"{PREFIX}lane-legacy"
    _seed_lane_jobs(uid, 1)

    class _Fake:
        def __init__(self, profile=None, feedback=""):
            pass
        def has_prescore_backend(self):
            return False
        def has_dual(self):
            return False
        def score(self, resume, job, provider=None):
            return 50.0, "fit", [], {}

    stats = _drive_cycle(monkeypatch, uid, _Fake)
    assert stats["scored"] == 1 and stats["by_claude"] == 0
    assert stats.get("by_unknown") == 1


def test_lane_has_no_spend_writer_of_its_own():
    """ONE writer. The lanes' own per-kind accounting is gone."""
    import inspect
    src = inspect.getsource(sl._run_scoring_cycle)
    assert "spend_by_user" not in src and "record_llm_spend" not in src
    assert "flush_llm_spend" in src
    assert not hasattr(pl, "_record_spend")
    psrc = inspect.getsource(pl._fast_path_user)
    assert "record_llm_spend" not in psrc and "_flush_spend()" in psrc


def test_pulse_fast_path_uses_score_with_meta_and_flushes(monkeypatch):
    uid = f"{PREFIX}pulse"
    with get_session() as session:
        j = Job(title="Senior ML Engineer", company="Acme", location="Remote", remote=True,
                description="LLMs in Python", source=JobSource.GREENHOUSE,
                external_id="spend-pulse-1", url="https://x/spend-pulse-1", user_id=uid,
                first_seen=datetime.utcnow(), posted_at=datetime.utcnow())
        session.add(j)
        session.commit()
        jid = j.id

    flushed = {"n": 0}
    real_flush = spend.flush_llm_spend

    def _counting_flush():
        flushed["n"] += 1
        return real_flush()
    monkeypatch.setattr(spend, "flush_llm_spend", _counting_flush)

    class _Fake:
        def __init__(self, profile=None, feedback=""):
            pass
        def has_prescore_backend(self):
            return False
        def score(self, resume, job):
            raise AssertionError("the fast path must prefer score_with_meta when it exists")
        def score_with_meta(self, resume, job):
            spend.buffer_llm_spend(uid, "score_final", provider="openai", model="gpt-4o-mini",
                                   usage={"input": 100, "output": 10})
            return 50.0, "fit", [], {}, {"provider": "openai", "model": "gpt-4o-mini", "usage": {}}

    from app.matching.finals_budget import Allowance
    monkeypatch.setattr("app.matching.reranker.Reranker", _Fake)
    monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: "resume")
    monkeypatch.setattr(sl, "_finals_allowance", lambda u, cap: Allowance(10, 40, "fill"))

    class _G:
        is_ghost, ghost_score, flags_json, flags = False, 0.0, None, []
    monkeypatch.setattr("app.matching.filters.score_ghost", lambda job, session: _G())

    scored, short, alerts = pl._fast_path_user(uid, score_budget=5)
    assert (scored, short) == (1, 0)
    assert flushed["n"] == 1, "one flush per fast path, at the end"
    with get_session() as session:
        assert session.get(Job, jid).rerank_score == 50.0
    (row,) = _rows(uid)
    assert (row.kind, row.provider, row.model, row.metered_calls) == ("score_final", "openai", "gpt-4o-mini", 1)


# ── 5. provider_status + down_since semantics ────────────────────────────────

def test_provider_status_shape_when_healthy():
    st = rr.provider_status()
    assert set(st) == {"anthropic", "openai"}
    for name in ("anthropic", "openai"):
        assert set(st[name]) == {"configured", "available", "down_until", "down_since", "last_error"}
        assert isinstance(st[name]["configured"], bool)
        assert st[name]["available"] is True
        assert st[name]["down_until"] is None and st[name]["down_since"] is None
        assert st[name]["last_error"] is None


def test_down_since_marks_the_start_of_the_current_outage_and_clears_on_success(monkeypatch, caplog):
    monkeypatch.setattr(settings, "llm_provider_cooldown_minutes", 30)
    monkeypatch.setattr(settings, "local_score_fallback", True)
    t0 = 1_800_000_000.0
    now = {"t": t0}
    monkeypatch.setattr(rr.time, "time", lambda: now["t"])

    with caplog.at_level(logging.WARNING, logger="app.matching.reranker"):
        rr._mark_provider_down("anthropic", error="400 Your credit balance is too low sk-ant-api03-SECRETSECRET")
    st = rr.provider_status()["anthropic"]
    assert st["available"] is False
    assert st["down_since"] == rr._iso(t0)
    assert st["down_until"] == rr._iso(t0 + 1800)
    assert "credit balance is too low" in st["last_error"]
    assert "SECRETSECRET" not in st["last_error"], "keys never reach the status surface"
    assert "[redacted]" in st["last_error"]

    # 2 days 3 hours later the breaker re-trips: down_since is UNCHANGED and the
    # warning says how long it has been and who is serving finals meanwhile.
    now["t"] = t0 + 2 * 86400 + 3 * 3600 + 5
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.matching.reranker"):
        rr._mark_provider_down("anthropic", error="still 400")
    st = rr.provider_status()["anthropic"]
    assert st["down_since"] == rr._iso(t0), "the start of the CURRENT outage, not the last trip"
    assert st["last_error"] == "still 400"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("marked DOWN" in m for m in msgs), msgs
    assert any("has failed on credit/quota for 2d 3h" in m for m in msgs), msgs
    assert any("finals are being served by" in m for m in msgs), msgs
    assert any("console.anthropic.com" in m for m in msgs), msgs

    # The cooldown expiring proves nothing: available again, outage still open.
    now["t"] += 1801
    st = rr.provider_status()["anthropic"]
    assert st["available"] is True and st["down_until"] is None
    assert st["down_since"] == rr._iso(t0)

    # A SUCCESSFUL call is what closes it.
    rr._note_provider_ok("anthropic")
    st = rr.provider_status()["anthropic"]
    assert st["down_since"] is None and st["last_error"] is None


def test_outage_warning_names_the_backend_that_will_serve_finals(monkeypatch, caplog):
    monkeypatch.setattr(settings, "dual_score_enabled", False)
    monkeypatch.setattr(settings, "local_score_fallback", True)
    # Both providers configured (settings-level, no clients built).
    monkeypatch.setattr(rr, "_CLIENTS", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "k1")
    monkeypatch.setattr(settings, "openai_api_key", "k2")
    with caplog.at_level(logging.WARNING, logger="app.matching.reranker"):
        rr._mark_provider_down("anthropic", error="credit")
    assert any("served by openai/gpt-4o-mini" in r.getMessage() for r in caplog.records)
    caplog.clear()
    # With openai down too, the local scorer is what serves.
    with caplog.at_level(logging.WARNING, logger="app.matching.reranker"):
        rr._mark_provider_down("openai", error="quota")
    assert any("local scorer" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setattr(settings, "local_score_fallback", False)
    with caplog.at_level(logging.WARNING, logger="app.matching.reranker"):
        rr._mark_provider_down("openai", error="quota")
    assert any("NOBODY" in r.getMessage() for r in caplog.records)


def test_a_successful_score_clears_the_outage_and_a_credit_error_records_it():
    uid = f"{PREFIX}u7"
    rk = _reranker(uid, anthropic=_AnthropicBroke(),
                   openai=_OpenAIOk('{"score": 70, "reason": "ok", "concerns": [], "breakdown": {}}'))
    rr._provider_down_since["openai"] = time.time() - 60      # pretend openai was out earlier
    rr._provider_last_error["openai"] = "old"
    rk.score_with_meta("resume", _job())
    st = rr.provider_status()
    assert st["anthropic"]["down_since"] is not None
    assert "credit balance" in st["anthropic"]["last_error"]
    assert st["openai"]["down_since"] is None and st["openai"]["last_error"] is None


def test_fmt_duration():
    assert rr._fmt_duration(0) == "0m"
    assert rr._fmt_duration(5 * 60 + 30) == "5m"
    assert rr._fmt_duration(3 * 3600 + 12 * 60) == "3h 12m"
    assert rr._fmt_duration(2 * 86400 + 3 * 3600 + 59 * 60) == "2d 3h"


# ── 6. Legacy signature ──────────────────────────────────────────────────────

def test_record_llm_spend_legacy_signature_still_works():
    """server.py's two tailor calls: record_llm_spend(uid, "tailor")."""
    uid = f"{PREFIX}legacy"
    spend.record_llm_spend(uid, "tailor")
    spend.record_llm_spend(uid, "tailor")
    spend.record_llm_spend(uid, "score_prescore", 3)
    rows = {r.kind: r for r in _rows(uid)}
    t = rows["tailor"]
    assert (t.calls, t.provider, t.model, t.metered_calls) == (2, None, None, 0)
    assert t.est_cost_usd == pytest.approx(2 * spend.EST_COST_PER_CALL["tailor"])
    assert t.day == date.today()
    p = rows["score_prescore"]
    assert p.calls == 3 and p.est_cost_usd == pytest.approx(3 * spend.EST_COST_PER_CALL["score_prescore"])
    # Legacy and attributed rows for the same kind are DIFFERENT rows.
    spend.record_llm_spend(uid, "score_prescore", provider="openai", model="gpt-4o-mini",
                           usage={"input": 100, "output": 10})
    assert len([r for r in _rows(uid) if r.kind == "score_prescore"]) == 2


def test_record_llm_spend_ignores_non_positive_calls():
    uid = f"{PREFIX}zero"
    spend.record_llm_spend(uid, "tailor", 0)
    spend.record_llm_spend(uid, "tailor", -1)
    assert _rows(uid) == []


# ── The buffer: thread-safe, coalescing, flush-once ──────────────────────────

def test_spend_buffer_coalesces_per_key_and_is_thread_safe():
    uid = f"{PREFIX}threads"
    buf = spend.SpendBuffer()

    def _worker():
        for _ in range(50):
            buf.add(uid, "score_final", provider="anthropic", model="claude-haiku-4-5",
                    usage={"input": 10, "output": 2, "cache_read": 100, "cache_write": 0})
            buf.add(uid, "score_prescore", provider="openai", model="gpt-4o-mini",
                    usage={"input": 5, "output": 1})
    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert buf.pending() == 2
    snap = buf.snapshot()
    fin = snap[(uid, spend._utc_day(), "score_final", "anthropic", "claude-haiku-4-5")]
    assert fin["metered_calls"] == 1000
    assert fin["usage"] == {"input": 10_000, "output": 2_000, "cache_read": 100_000, "cache_write": 0}
    assert buf.flush() == 2
    assert buf.pending() == 0 and buf.flush() == 0
    rows = {(r.kind, r.provider): r for r in _rows(uid)}
    f = rows[("score_final", "anthropic")]
    assert f.calls == 1000 and f.metered_calls == 1000 and f.cache_read_tokens == 100_000
    assert f.est_cost_usd == pytest.approx((10_000 * 1.00 + 2_000 * 5.00 + 100_000 * 0.10) / 1e6)
    assert rows[("score_prescore", "openai")].calls == 1000


def test_spend_buffer_keeps_metered_and_flat_calls_apart():
    uid = f"{PREFIX}mixed"
    buf = spend.SpendBuffer()
    buf.add(uid, "score_final", provider="openai", model="gpt-4o-mini", usage={"input": 1000})
    buf.add(uid, "score_final", provider="openai", model="gpt-4o-mini")          # no usage
    buf.add(uid, "score_final", provider="openai", model="gpt-4o-mini", usage={"input": 1000})
    b = buf.snapshot()[(uid, spend._utc_day(), "score_final", "openai", "gpt-4o-mini")]
    assert (b["metered_calls"], b["flat_calls"]) == (2, 1)
    buf.flush()
    (row,) = _rows(uid)
    assert row.calls == 3 and row.metered_calls == 2
    assert row.est_cost_usd == pytest.approx(2 * 1000 * 0.15 / 1e6 + spend.EST_COST_PER_CALL["score_final"])


def test_buffer_never_raises_into_the_money_path(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("buffer exploded")
    monkeypatch.setattr(spend._BUFFER, "add", _boom)
    spend.buffer_llm_spend("u", "score_final", provider="openai", model="gpt-4o-mini")   # must not raise
    monkeypatch.setattr(spend._BUFFER, "flush", _boom)
    assert spend.flush_llm_spend() == 0


# ── spend_summary surfaces provider + metered share ──────────────────────────

def test_spend_summary_reports_by_provider_and_metered_share():
    uid = f"{PREFIX}summary"
    spend.record_llm_spend(uid, "score_final", provider="openai", model="gpt-4o-mini",
                           usage={"input": 1000, "output": 100, "cache_read": 2000})
    spend.record_llm_spend(uid, "tailor")
    s = spend.spend_summary(1)
    assert set(s) >= {"days", "total_est_cost_usd", "metered_share", "by_day", "by_provider", "top_users", "note"}
    assert 0.0 <= s["metered_share"] <= 1.0
    oai = s["by_provider"]["openai"]
    assert oai["metered_calls"] >= 1 and oai["cache_read_tokens"] >= 2000
    assert "gpt-4o-mini" in oai["models"]
    assert "unattributed" in s["by_provider"]          # the legacy tailor row
    mine = next((u for u in s["top_users"] if u["user_id"] == uid), None)
    if mine is not None:                              # top-25 may not include it under a noisy DB
        assert mine["kinds"]["score_final"]["by_provider"]["openai"]["model"] == "gpt-4o-mini"
        assert mine["kinds"]["tailor"]["by_provider"]["unattributed"]["metered_calls"] == 0
    assert "metered_share" in s["note"]


# ── Location is never a silent blank in the scorer's prompt ──────────────────

def test_job_context_block_never_leaves_location_blank():
    j = _job()
    j.location, j.remote = "", False
    block = rr._job_context_block(j)
    assert "Location: not stated in the posting" in block
    assert "Remote: no" in block
    assert "Remote: False" not in block
    j.location, j.remote = "  ", True
    assert "Location: not stated in the posting" in rr._job_context_block(j)
    assert "Remote: yes" in rr._job_context_block(j)
    j.location = "Austin, TX"
    assert "Location: Austin, TX" in rr._job_context_block(j)
    # The Tier-1 prompt says the same thing — Tier-1 gates what Tier-2 sees.
    j.location = ""
    assert "Location: not stated in the posting" in rr._build_prescore_prompt("r", j)


# ── The ledger columns reach a live database ─────────────────────────────────

def test_llm_spend_columns_are_migrated_onto_existing_databases():
    import inspect as _inspect
    from app.db import init_db
    src = _inspect.getsource(init_db)
    for col in ("provider", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens", "metered_calls"):
        assert f'("{col}",' in src, col
    assert 'add_column_if_missing("llm_spend"' in src
