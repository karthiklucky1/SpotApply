"""Per-user LLM spend ledger — METERED where token counts exist, estimated elsewhere.

Every paid LLM call site records here so the owner can answer "which user /
day / feature / PROVIDER is the money going to?". Rows are keyed by
(user, day, kind, provider, model); a row carries the token counts the API
returned and a cost priced from PRICES_PER_MTOK, and `metered_calls` says how
many of its calls were priced from real usage rather than a flat guess.

Why metered, and why provider is part of the key (audit, 2026-09-14..16):
Anthropic rejected every call with 400 "credit balance is too low" for three
days and billed $0, while OpenAI served every final and its spend rose to
$3.20/day. This ledger recorded 1,534/1,534/1,136 finals a day as
`score_final` at the Haiku flat rate, and the cycle stats said by_claude=1,335
by_gpt=0 — attribution keyed off the provider the lane ASKED for, not the one
that answered. Over seven days the estimate was $68.60 against $17.77 of
actual provider cost (3.9x): prescore alone was 83% of it at a never-metered
$0.001/call, and the lanes recorded 67,280 prescore+final calls while OpenAI
saw 41,378 requests, because a ghost stamp, a rule pre-filter and the empty-JD
guard were each booked as a Tier-1 call that never happened.

ONE writer: the Reranker records each call at the call site (it knows the
user, the backend that answered, the model id and `resp.usage`) into the
in-process SpendBuffer; the lanes only FLUSH it at the end of a cycle/tick.
A call that never happened therefore cannot be recorded. Flat per-call
estimates survive only as the fallback for legacy callers (tailoring) and for
responses that carried no usage. Recording must never break the caller —
everything is wrapped."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import LlmSpend

log = logging.getLogger(__name__)


def _utc_day() -> date:
    return datetime.now(timezone.utc).date()


def _lock_ledger_key(session, key) -> None:
    """Serialize the read/insert/increment even when two lanes flush together.

    A Python buffer lock ends before DB writes and cannot protect another
    process. The transaction lock covers absent rows too, without a destructive
    deduplication/index migration of the historical ledger.
    """
    if session.get_bind().dialect.name == "postgresql":
        digest = hashlib.sha256(json.dumps(key, default=str).encode()).digest()
        lock_id = int.from_bytes(digest[:8], "big", signed=True)
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
    else:
        session.execute(text("BEGIN IMMEDIATE"))

# Flat per-call estimates (USD) — the FALLBACK when a call carried no token
# usage (legacy callers, fakes, SDK responses without `.usage`). Every scoring
# call now records real usage, so these price only what metering cannot.
EST_COST_PER_CALL = {
    # MEASURED 2026-09-05 from the reranker's own token telemetry ("Claude
    # usage (last N finals cumulative)") over 250 production calls, priced at
    # Haiku 4.5 list ($1.00/$5.00 per MTok, cache write 1.25x in, read 0.10x):
    #   uncached_in 102,163  cache_read 1,167,451  cache_write 116,735
    #   out 49,942           =>  $0.6145 / 250 = $0.00246
    # The old 0.010 came from nowhere and was 4.1x high. It is quoted in
    # docs/CAPACITY.md and in the ceiling arithmetic behind
    # PLAN_LIMITS["finals_daily"], so a wrong number here mis-sizes real limits.
    "score_final": 0.0025,    # Claude final — the cached résumé prefix is why
    # UNMEASURED, unlike score_final: the Tier-1 path logs no token counts, so
    # nothing has ever checked this. Back-of-envelope on gpt-4o-mini
    # ($0.15/$0.60 per MTok, ~2.5k in + ~60 out) says ~$0.0004, i.e. this may
    # be ~2.5x high too — but an estimate is not a measurement. To settle it,
    # add the same cumulative-usage log to Reranker.prescore.
    "score_prescore": 0.001,  # Tier-1 mini-model bulk score
    # One cache-prefix write (~4-5k tokens at the 1.25x write rate, 0 output
    # tokens) per user per cycle. Real calls always carry usage, so this flat
    # figure prices only a response without one.
    "score_prewarm": 0.006,
    "score_local": 0.0,       # local fallback — free
    "tailor": 0.05,           # résumé + cover letter generation pass
    # One shared location-extraction call per unresolved NEW posting
    # (app/discovery/geo_verify.py): ~1.2k excerpt tokens in, ~120 out on
    # gpt-4o-mini list price. Real calls carry usage and are metered; this
    # prices only a response without one.
    "geo_verify": 0.0003,
}

# USD per MILLION tokens, keyed by model-id PREFIX. The longest matching prefix
# wins, so "gpt-4o-mini-2024-07-18" prices as mini and not as gpt-4o, and a
# dated Haiku snapshot prices as Haiku. Usage is normalised to four buckets:
#   input       — uncached prompt tokens (Anthropic `input_tokens`; OpenAI
#                 `prompt_tokens - cached_tokens`)
#   output      — completion tokens
#   cache_read  — prompt tokens served from cache (Anthropic
#                 `cache_read_input_tokens`; OpenAI `cached_tokens`)
#   cache_write — Anthropic `cache_creation_input_tokens`; OpenAI has no
#                 explicit write charge, so 0.0
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075, "cache_write": 0.0},
    "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25, "cache_write": 0.0},
}
USAGE_KEYS = ("input", "output", "cache_read", "cache_write")


def price_for_model(model: Optional[str]) -> Optional[dict[str, float]]:
    """The per-MTok price row for ``model`` (longest prefix match), or None
    when the model is not in the table — callers then fall back to the flat
    estimate rather than pricing at a guessed rate."""
    if not model:
        return None
    m = str(model).lower()
    best = None
    for prefix, prices in PRICES_PER_MTOK.items():
        if m.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICES_PER_MTOK[best] if best else None


def normalize_usage(usage) -> dict[str, int]:
    """Zero-filled int usage dict over USAGE_KEYS. Tolerates None, partial
    dicts and non-int values (an SDK object's counters can be None)."""
    out = {k: 0 for k in USAGE_KEYS}
    if not usage:
        return out
    for k in USAGE_KEYS:
        try:
            v = usage.get(k, 0) if isinstance(usage, dict) else getattr(usage, k, 0)
            out[k] = max(0, int(v or 0))
        except (TypeError, ValueError):
            out[k] = 0
    return out


def estimate_cost(model: Optional[str], usage) -> Optional[float]:
    """Metered USD cost of one or more calls from their summed token usage.
    Returns None when ``model`` has no price row, so the caller can fall back
    to the flat estimate AND leave `metered_calls` at 0 — an unpriced model
    must never look measured."""
    prices = price_for_model(model)
    if prices is None:
        return None
    u = normalize_usage(usage)
    return sum(u[k] * prices.get(k, 0.0) for k in USAGE_KEYS) / 1_000_000.0


def record_llm_spend(user_id: str | None, kind: str, calls: int = 1, *,
                     provider: Optional[str] = None, model: Optional[str] = None,
                     usage=None, cost_usd: Optional[float] = None,
                     ledger_day: Optional[date] = None) -> None:
    """Upsert today's (user, kind, provider, model) row. Safe from any thread.

    Backward compatible: ``record_llm_spend(uid, "tailor")`` still works and
    prices at the flat rate with provider/model NULL. With ``usage`` (and a
    priced ``model``) the cost is metered and the calls count as metered;
    ``cost_usd`` overrides both when the caller already knows the money."""
    if calls <= 0:
        return
    uid = user_id or "local"
    today = ledger_day if ledger_day is not None else _utc_day()
    tokens = normalize_usage(usage) if usage is not None else None
    metered = 0
    if cost_usd is not None:
        est = float(cost_usd)
        metered = calls
    else:
        est = None
        if tokens is not None:
            est = estimate_cost(model, tokens)
            if est is not None:
                metered = calls
        if est is None:
            est = EST_COST_PER_CALL.get(kind, 0.0) * calls
    try:
        with get_session() as session:
            _lock_ledger_key(session, (uid, today, kind, provider, model))
            row = session.exec(
                select(LlmSpend).where(
                    LlmSpend.user_id == uid,
                    LlmSpend.day == today,
                    LlmSpend.kind == kind,
                    LlmSpend.provider == provider,   # `== None` renders IS NULL
                    LlmSpend.model == model,
                )
            ).first()
            if row is None:
                row = LlmSpend(user_id=uid, day=today, kind=kind,
                               provider=provider, model=model)
            row.calls = (row.calls or 0) + calls
            row.est_cost_usd = (row.est_cost_usd or 0.0) + est
            row.metered_calls = (row.metered_calls or 0) + metered
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            if tokens is not None:
                row.input_tokens = (row.input_tokens or 0) + tokens["input"]
                row.output_tokens = (row.output_tokens or 0) + tokens["output"]
                row.cache_read_tokens = (row.cache_read_tokens or 0) + tokens["cache_read"]
                row.cache_write_tokens = (row.cache_write_tokens or 0) + tokens["cache_write"]
            session.add(row)
            session.commit()
    except Exception as e:  # never let bookkeeping break the money path
        log.debug("llm spend record failed (%s/%s/%s): %s", uid, kind, provider, e)


# ── In-process buffer: the ONE writer's staging area ─────────────────────────
# The Reranker is shared by 20 worker threads per user and makes one API call
# per job; writing a ledger row per call would be a DB round-trip inside the
# money path. Calls are coalesced here per (user, UTC day, kind, provider, model) and
# the lanes flush once per cycle/tick — one upsert per key, not per call.
# Best-effort by design: a crash loses at most one cycle's attribution.

class SpendBuffer:
    """Thread-safe accumulator of LLM calls, coalesced per ledger key.

    Per key three buckets are kept apart because they price differently:
    metered (calls + summed usage), flat (calls with no usage) and priced
    (calls whose cost the caller supplied). ``flush`` upserts each non-empty
    bucket, so a mixed key costs at most three round-trips instead of one per
    call."""

    # Past this many distinct keys the buffer flushes itself on the next add.
    # Production sees a few dozen keys per cycle; the bound exists so a lane
    # that stops flushing cannot grow the process without limit.
    MAX_KEYS = 5000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict = {}

    @staticmethod
    def _empty() -> dict:
        return {"metered_calls": 0, "usage": {k: 0 for k in USAGE_KEYS},
                "flat_calls": 0, "priced_calls": 0, "priced_cost": 0.0}

    def add(self, user_id: str | None, kind: str, *, provider: Optional[str] = None,
            model: Optional[str] = None, usage=None, calls: int = 1,
            cost_usd: Optional[float] = None) -> None:
        if calls <= 0:
            return
        key = (user_id or "local", _utc_day(), kind, provider, model)
        overflow = False
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                row = self._rows[key] = self._empty()
            if cost_usd is not None:
                row["priced_calls"] += calls
                row["priced_cost"] += float(cost_usd)
            elif usage is not None and price_for_model(model) is not None:
                row["metered_calls"] += calls
                t = normalize_usage(usage)
                for k in USAGE_KEYS:
                    row["usage"][k] += t[k]
            else:
                row["flat_calls"] += calls
            overflow = len(self._rows) > self.MAX_KEYS
        if overflow:
            self.flush()

    def pending(self) -> int:
        """Number of distinct ledger keys waiting to be flushed."""
        with self._lock:
            return len(self._rows)

    def snapshot(self) -> dict:
        """Copy of the buffered rows (tests / debug): {key: buckets}."""
        with self._lock:
            return {k: {**v, "usage": dict(v["usage"])} for k, v in self._rows.items()}

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()

    def flush(self) -> int:
        """Upsert everything buffered so far. Returns the number of ledger
        upserts issued. Never raises; a failed upsert is logged at DEBUG by
        record_llm_spend and its calls are dropped (not re-buffered — a
        poisoned key must not retry forever)."""
        with self._lock:
            rows, self._rows = self._rows, {}
        n = 0
        for (uid, day, kind, provider, model), b in rows.items():
            try:
                if b["metered_calls"]:
                    record_llm_spend(uid, kind, b["metered_calls"], provider=provider,
                                     model=model, usage=b["usage"], ledger_day=day)
                    n += 1
                if b["flat_calls"]:
                    record_llm_spend(uid, kind, b["flat_calls"], provider=provider,
                                     model=model, ledger_day=day)
                    n += 1
                if b["priced_calls"]:
                    record_llm_spend(uid, kind, b["priced_calls"], provider=provider,
                                     model=model, cost_usd=b["priced_cost"], ledger_day=day)
                    n += 1
            except Exception as e:
                log.debug("llm spend flush failed (%s/%s/%s): %s", uid, kind, provider, e)
        return n


_BUFFER = SpendBuffer()


def buffer_llm_spend(user_id: str | None, kind: str, *, provider: Optional[str] = None,
                     model: Optional[str] = None, usage=None, calls: int = 1,
                     cost_usd: Optional[float] = None) -> None:
    """Record one (or ``calls``) LLM call(s) into the process buffer. Called by
    the Reranker at the API call site — the only place that knows which backend
    answered and what it billed. Never raises."""
    try:
        _BUFFER.add(user_id, kind, provider=provider, model=model, usage=usage,
                    calls=calls, cost_usd=cost_usd)
    except Exception as e:
        log.debug("llm spend buffer failed (%s/%s): %s", user_id, kind, e)


def flush_llm_spend() -> int:
    """Write the buffered calls to the ledger; the lanes call this once at the
    end of a scoring cycle / pulse tick. Returns upserts issued; never raises."""
    try:
        return _BUFFER.flush()
    except Exception as e:
        log.debug("llm spend flush failed: %s", e)
        return 0


def pending_spend() -> dict:
    """Snapshot of the unflushed buffer (tests / debug)."""
    return _BUFFER.snapshot()


def spend_summary(days: int = 14) -> dict:
    """Owner overview over the last N days: per-day totals, per-provider
    totals, top users (kinds broken down by provider), and ``metered_share``
    — the fraction of recorded calls priced from real token counts, so the
    owner can see how much of the number is measured rather than guessed."""
    from datetime import timedelta
    since = _utc_day() - timedelta(days=days - 1)
    with get_session() as session:
        rows = session.exec(select(LlmSpend).where(LlmSpend.day >= since)).all()
    by_day: dict = {}
    by_user: dict = {}
    by_provider: dict = {}
    total_calls = 0
    metered_calls = 0

    def _tokens(r) -> dict:
        return {"input_tokens": r.input_tokens or 0, "output_tokens": r.output_tokens or 0,
                "cache_read_tokens": r.cache_read_tokens or 0,
                "cache_write_tokens": r.cache_write_tokens or 0}

    def _acc(bucket: dict, r) -> None:
        bucket["calls"] += r.calls
        bucket["est_cost_usd"] += r.est_cost_usd
        bucket["metered_calls"] += r.metered_calls or 0
        for k, v in _tokens(r).items():
            bucket[k] = bucket.get(k, 0) + v

    def _bucket() -> dict:
        return {"calls": 0, "est_cost_usd": 0.0, "metered_calls": 0}

    for r in rows:
        total_calls += r.calls
        metered_calls += r.metered_calls or 0
        d = r.day.isoformat()
        by_day.setdefault(d, {"calls": 0, "est_cost_usd": 0.0, "metered_calls": 0})
        by_day[d]["calls"] += r.calls
        by_day[d]["est_cost_usd"] += r.est_cost_usd
        by_day[d]["metered_calls"] += r.metered_calls or 0

        prov = r.provider or "unattributed"
        p = by_provider.setdefault(prov, {**_bucket(), "models": {}})
        _acc(p, r)
        m = p["models"].setdefault(r.model or "unknown", _bucket())
        _acc(m, r)

        u = by_user.setdefault(r.user_id, {**_bucket(), "kinds": {}})
        _acc(u, r)
        k = u["kinds"].setdefault(r.kind, {**_bucket(), "by_provider": {}})
        _acc(k, r)
        kp = k["by_provider"].setdefault(prov, {**_bucket(), "model": r.model})
        _acc(kp, r)
        if kp.get("model") != r.model:
            kp["model"] = "mixed"

    top_users = sorted(
        ({"user_id": u, **v} for u, v in by_user.items()),
        key=lambda x: -x["est_cost_usd"],
    )[:25]
    total = round(sum(v["est_cost_usd"] for v in by_day.values()), 4)
    return {
        "days": days,
        "total_est_cost_usd": total,
        "metered_share": round(metered_calls / total_calls, 4) if total_calls else 0.0,
        "by_day": dict(sorted(by_day.items())),
        "by_provider": by_provider,
        "top_users": top_users,
        "note": "Metered from token usage where the API returned it (PRICES_PER_MTOK); "
                "flat per-call estimates only where it did not (metered_share says how "
                "much is measured). Covers scoring lane, pulse fast path, and tailoring; "
                "rows with provider=null are legacy/tailor entries.",
    }
