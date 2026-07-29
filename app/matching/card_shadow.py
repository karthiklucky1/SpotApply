"""CardRace v2 shadow harness — Phase 3 of docs/CARDRACE_DESIGN.md §5.

Runs BESIDE every real Claude final (never instead of one): mints/loads the two
cards, runs the deterministic matcher, assigns the would-be band, and records
one CardMatchShadow row. Zero effect on any user-visible decision — the live
cascade stays authoritative until the recorded agreement clears the §3.4 gates.

Spend: card mints only happen for jobs Claude is scoring anyway, so shadow's
extra cost tracks finals volume (~$0.005/scored job), bounded further by
``card_mint_daily_cap`` and the global LLM budget backstop.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

_stats_lock = threading.Lock()
_stats = {"n": 0, "abs_err_sum": 0.0, "within10": 0, "decision_agree": 0,
          "auto_in": 0, "auto_out": 0, "band": 0}
_LOG_EVERY = 20


def shadow_card_match(jid: int, resume_text: str, llm_score: float,
                      llm_breakdown: Optional[dict]) -> None:
    """Best-effort; swallows every failure — shadow must never break scoring."""
    if not settings.card_match_shadow:
        return
    try:
        _run(jid, resume_text, llm_score, llm_breakdown)
    except Exception as e:
        log.debug("card shadow failed for %s: %s", jid, e)


def _run(jid: int, resume_text: str, llm_score: float,
         llm_breakdown: Optional[dict]) -> None:
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import CardMatchShadow, Job, UserProfile
    from app.matching.card_match import match_cards
    from app.matching.cards import (get_or_compile_user_card, get_or_mint_job_card,
                                    job_card_key)
    from app.matching.conformal import assign_band
    from app.matching.skill_graph import load_graph

    with get_session() as session:
        job = session.get(Job, jid)
        if job is None:
            return
        uid = job.user_id
        profile = session.exec(
            select(UserProfile).where(UserProfile.user_id == uid)).first()
        session.expunge(job)

    job_card = get_or_mint_job_card(job)
    if job_card is None:
        return
    user_card = get_or_compile_user_card(uid, profile, resume_text)
    if user_card is None:
        return

    graph = load_graph() if settings.card_graph_enabled else None
    res = match_cards(user_card, job_card, graph=graph)
    decision = assign_band(res.direct, res.expanded, res.spread, res.low_confidence)

    with get_session() as session:
        session.add(CardMatchShadow(
            job_id=jid,
            user_id=uid,
            llm_score=float(llm_score),
            llm_breakdown=json.dumps(llm_breakdown) if llm_breakdown else None,
            direct_score=res.direct,
            expanded_score=res.expanded,
            spread=res.spread,
            calibrated=decision.calibrated,
            band=decision.band,
            breakdown=json.dumps(res.breakdown),
            card_key=job_card_key(job),
        ))
        session.commit()

    _record(llm_score, res.expanded, decision.band)


def _record(llm_score: float, g_score: float, band: str) -> None:
    """Aggregate telemetry so prod logs answer "is the card engine agreeing?"."""
    bar = settings.shortlist_score_threshold
    err = abs(float(llm_score) - float(g_score))
    agree = (llm_score >= bar) == (g_score >= bar)
    with _stats_lock:
        _stats["n"] += 1
        _stats["abs_err_sum"] += err
        _stats["within10"] += 1 if err <= 10 else 0
        _stats["decision_agree"] += 1 if agree else 0
        _stats[band] = _stats.get(band, 0) + 1
        if _stats["n"] % _LOG_EVERY:
            return
        s = dict(_stats)
    log.info("CardRace shadow (n=%d): MAE=%.1f within10=%.0f%% decision-agree@%.0f=%.0f%% "
             "bands in/band/out=%d/%d/%d",
             s["n"], s["abs_err_sum"] / max(1, s["n"]),
             100.0 * s["within10"] / max(1, s["n"]), bar,
             100.0 * s["decision_agree"] / max(1, s["n"]),
             s.get("auto_in", 0), s.get("band", 0), s.get("auto_out", 0))
