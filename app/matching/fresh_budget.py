"""Fresh-first allocation of the scarce LLM reranker budget.

The reranker only scores the top ``settings.llm_rerank_cap`` candidates per run.
Allocate those slots by freshness tier FIRST (a day's new postings before
older-but-similar jobs), breaking ties within a tier by weighted fit priority.
Without this, high-similarity stale jobs kept winning the budget and brand-new
postings sat unscored — the pool grew but the shortlist didn't.

Kept dependency-light (only ``datetime`` + the ``Job`` model) so it imports
without the heavy ML stack and stays unit-testable in isolation.
"""
from __future__ import annotations

from datetime import datetime

from app.db.models import Job

_FRESHNESS_TIER_HOURS = (24, 72, 168)  # tiers: <1d, <3d, <7d, then older


def freshness_tier(job: Job) -> int:
    """0 = freshest (<24h) … ``len(_FRESHNESS_TIER_HOURS)`` = oldest/undated.
    Undated rows sort to the oldest tier so a missing timestamp never outranks a
    real fresh posting for the LLM budget."""
    posted = job.posted_at or job.first_seen
    if not posted:
        return len(_FRESHNESS_TIER_HOURS)
    if posted.tzinfo is not None:
        posted = posted.replace(tzinfo=None)
    age_h = (datetime.utcnow() - posted).total_seconds() / 3600.0
    for i, bound in enumerate(_FRESHNESS_TIER_HOURS):
        if age_h < bound:
            return i
    return len(_FRESHNESS_TIER_HOURS)


def order_fresh_first(to_rerank, tier_of, priority_of):
    """Order ``(jid, sim)`` candidates for the LLM budget: freshest tier first,
    then highest weighted fit priority within a tier. Pure/testable — the
    freshness tiers and priorities are precomputed by the caller. A jid absent
    from ``tier_of`` defaults to the oldest tier so it never floats to the top."""
    return sorted(
        to_rerank,
        key=lambda t: (
            tier_of.get(t[0], len(_FRESHNESS_TIER_HOURS)),
            -priority_of.get(t[0], t[1]),
        ),
    )


def order_fit_first(to_rerank, score_of):
    """Order the freshly-scored cohort by LLM fit score DESC for SHORTLIST
    creation, so the strongest matches claim the scarce daily-limit and
    per-company-cap slots first. This is deliberately NOT fresh-first: iterating
    the shortlist step fresh-first would let a marginal newer role take a
    company's last cap slot and block a stronger same-company role for the
    cooldown window. Jobs missing from ``score_of`` (unscored) sort last."""
    return sorted(to_rerank, key=lambda t: score_of.get(t[0], 0.0), reverse=True)
