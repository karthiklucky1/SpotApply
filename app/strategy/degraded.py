"""Degraded mode: what happens to the shortlist when the LLM is unavailable.

When credits run out (or every provider is cooling down after a billing/quota
error), `Reranker.score()` falls back to the local cross-encoder so the funnel
keeps moving. That kept the pipeline alive but filled the board with weak
matches, because a local score cleared the same 60 bar a Claude final does.

Two rules fix that, and this module owns both:

1. **A higher bar while degraded.** A local-only score must clear
   ``degraded_shortlist_threshold`` (75) instead of 60. Fewer jobs, but ones
   worth looking at. Everything shortlisted this way is flagged
   ``Application.provisional`` — shortlisted, but never actually AI-reviewed.

2. **One recheck when credits return.** The provisional applications are
   re-scored with the real model, ONCE, and the ones that no longer clear the
   normal bar are removed from the board. Scope is deliberately tight:

   * only applications created during the outage — the window is the outage's
     OWN duration (2 hours out → 2 hours of jobs), hard-capped at
     ``degraded_recheck_max_hours`` (48) because older postings are stale;
   * at most ``degraded_recheck_max_jobs`` (20) per user, best-first;
   * exactly once per outage — the flag is cleared whether the job survives or
     not, so a job can never be re-billed on a later pass.

The outage window is persisted (not just held in memory) so a container restart
mid-outage doesn't lose it and re-check the wrong span.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job

log = logging.getLogger(__name__)

# Small persisted marker. A table would need a migration for two timestamps;
# this file lives beside the SQLite db / on the container's data volume and is
# rewritten at most twice per outage.
_STATE_PATH = Path("./data/degraded_state.json")


def _read_state() -> dict:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text()) or {}
    except Exception as e:
        log.debug("degraded state unreadable: %s", e)
    return {}


def _write_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state))
    except Exception as e:
        log.debug("degraded state unwritable: %s", e)


def shortlist_threshold(is_local_score: bool) -> int:
    """The bar a score must clear to be shortlisted.

    Local scores are held to the degraded bar even if the provider recovered a
    second ago: the score in hand was still produced without AI review.
    """
    if not is_local_score:
        return settings.shortlist_score_threshold
    return max(settings.shortlist_score_threshold, settings.degraded_shortlist_threshold)


def note_degraded(now: Optional[datetime] = None) -> None:
    """Record that we are scoring without a real LLM right now."""
    now = now or datetime.utcnow()
    state = _read_state()
    if not state.get("started_at"):
        state["started_at"] = now.isoformat()
        log.warning("Degraded mode STARTED — local scoring only, shortlist bar raised to %d",
                    settings.degraded_shortlist_threshold)
    state["last_seen_at"] = now.isoformat()
    _write_state(state)


def note_healthy(now: Optional[datetime] = None) -> Optional[tuple[datetime, datetime]]:
    """Record that a real LLM answered. Returns the closed outage window, once.

    Returns ``(started_at, ended_at)`` on the FIRST healthy call after an
    outage and ``None`` every other time — the caller uses that to trigger the
    recheck exactly once.
    """
    now = now or datetime.utcnow()
    state = _read_state()
    started = state.get("started_at")
    if not started:
        return None
    try:
        start_dt = datetime.fromisoformat(started)
    except ValueError:
        _write_state({})
        return None
    # Closed: clear it before returning so a concurrent lane can't run twice.
    _write_state({})
    log.warning("Degraded mode ENDED after %s — rechecking provisional shortlist",
                str(now - start_dt).split(".")[0])
    return start_dt, now


def recheck_provisional(window: tuple[datetime, datetime], user_ids: list[str | None]) -> dict:
    """Re-score the provisional shortlist from an outage window, once.

    Jobs that no longer clear the normal bar are moved to SKIPPED (never
    deleted — the user can still see what happened and why).
    """
    start_dt, end_dt = window
    max_span = timedelta(hours=max(1, settings.degraded_recheck_max_hours))
    # The window is the outage's own length, capped. A 6-hour gap rechecks 6
    # hours of jobs, not two days of them.
    cutoff = max(start_dt, end_dt - max_span)
    stats = {"checked": 0, "kept": 0, "removed": 0, "errors": 0}

    from app.matching.reranker import Reranker

    for uid in user_ids:
        uid_arg = None if (not uid or uid == "local") else uid
        with get_session() as session:
            q = (select(Application, Job)
                 .join(Job, Job.id == Application.job_id)
                 .where(Application.provisional == True)  # noqa: E712
                 .where(Application.status == ApplicationStatus.SHORTLISTED)
                 .where(Application.created_at >= cutoff))
            q = q.where(Application.user_id == uid_arg) if uid_arg \
                else q.where(Application.user_id.is_(None))
            rows = session.exec(q).all()
            # Best-first, so a capped run keeps the most promising ones.
            rows = sorted(rows, key=lambda r: -(r[1].rerank_score or 0))
            pending = [(a.id, j.id) for a, j in rows][: settings.degraded_recheck_max_jobs]

        if not pending:
            continue

        # Build the scorer exactly the way the scoring lane does. This block
        # used to call Reranker(user_id=...) and .resume_text(), neither of
        # which exists — every recheck raised here, was swallowed as "reranker
        # unavailable", and no provisional shortlist was ever re-reviewed.
        try:
            from app.matching.pipeline import _load_resume
            resume_text = _load_resume(user_id=uid_arg)
            profile = None
            try:
                from app.autofill.answer_pack import _get_or_create_profile
                profile = _get_or_create_profile(user_id=uid_arg)
            except Exception:
                pass
            reranker = Reranker(profile=profile)
        except Exception as e:
            log.warning("Recheck skipped for %s — reranker unavailable: %s", uid, e)
            stats["errors"] += 1
            continue

        for app_id, job_id in pending:
            with get_session() as session:
                job = session.get(Job, job_id)
                if not job:
                    continue
            try:
                # score() returns (score, reason, concerns, breakdown) — the old
                # float(getattr(result, "score", result)) coerced the 4-tuple and
                # raised TypeError on every job.
                score, _reason, _concerns, _breakdown = reranker.score(resume_text, job)
                score = float(score or 0)
            except Exception as e:
                # Still no usable LLM — leave it provisional for the next
                # recovery rather than dropping a job on a transient failure.
                log.debug("Recheck score failed for job %s: %s", job_id, e)
                stats["errors"] += 1
                continue

            stats["checked"] += 1
            keeps = score >= settings.shortlist_score_threshold
            with get_session() as session:
                app_row = session.get(Application, app_id)
                job_row = session.get(Job, job_id)
                if not app_row or not job_row:
                    continue
                job_row.rerank_score = score
                app_row.provisional = False          # reviewed either way — never re-billed
                if keeps:
                    stats["kept"] += 1
                else:
                    app_row.status = ApplicationStatus.SKIPPED
                    app_row.notes = ((app_row.notes or "") +
                                     f"\nRemoved on AI review after an LLM outage "
                                     f"(scored {score:.0f}, needs "
                                     f"{settings.shortlist_score_threshold}).").strip()
                    stats["removed"] += 1
                app_row.updated_at = datetime.utcnow()
                session.add(app_row)
                session.add(job_row)
                session.commit()

    if stats["checked"]:
        log.warning("Provisional recheck done: %d checked, %d kept, %d removed (window from %s)",
                    stats["checked"], stats["kept"], stats["removed"], cutoff.isoformat())
    return stats
