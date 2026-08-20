"""Hard-delete dead job rows so the table (and every scan's egress) stays bounded.

The scrape-once pool + per-user adoption copies grow the ``job`` table forever;
the shared-pool retention only ever set ``is_closed=True``, so closed rows kept
accumulating and every lane scan streamed more bytes out of Postgres (100% of
the egress overage was Shared-Pooler / DB reads).

``purge_old_closed_jobs`` deletes rows that are provably dead — CLOSED, older
than a cutoff, and NOT referenced by any Application — in bounded batches so no
single statement can hit Supabase's statement timeout. A job with an Application
is never touched, so nothing a user has shortlisted/tailored/applied to is lost.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlmodel import select, update

log = logging.getLogger(__name__)


def close_stale_user_jobs(days: int = 45, batch: int = 2000, max_batches: int = 100) -> int:
    """Age-close OPEN per-user rows older than ``days`` that have no Application.

    The missing half of retention (CAPACITY.md §5.1): shared-pool rows are
    closed at 45d, but per-user adopted copies were never age-closed, so they
    accumulated forever and could never reach the purge. A 45-day-old posting
    is filled or ghost; anything a user acted on has an Application and is
    never touched. Batched id-select → targeted UPDATE, mirroring the purge,
    so no single statement can hit the statement timeout.
    """
    from app.db.init_db import get_session
    from app.db.models import Application, Job
    from app.discovery.pipeline import SHARED_POOL_USER
    from sqlalchemy import update

    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    closed = 0
    for _ in range(max_batches):
        with get_session() as session:
            ids = [r[0] if isinstance(r, tuple) else r for r in session.exec(
                select(Job.id)
                .where(
                    Job.is_closed == False,          # noqa: E712
                    Job.user_id != SHARED_POOL_USER,  # shared pool has its own close
                    Job.user_id.is_not(None),
                    Job.first_seen < cutoff,
                    Job.id.not_in(select(Application.job_id)),  # user-touched rows keep their lifecycle
                )
                .limit(batch)
            ).all()]
            if not ids:
                break
            session.exec(
                update(Job)
                .where(Job.id.in_(ids))
                .values(is_closed=True,
                        closed_reason=f"per-user retention ({days}d, no application)")
            )
            session.commit()
            closed += len(ids)
        if len(ids) < batch:
            break
    if closed:
        log.info("Job retention: age-closed %d stale per-user job(s) older than %dd", closed, days)
    return closed


def purge_old_closed_jobs(days: int = 60, batch: int = 2000, max_batches: int = 100) -> int:
    """Delete CLOSED jobs older than ``days`` that have no Application attached.

    Returns the number of rows deleted. Batched + committed per batch so a large
    backlog drains across several small statements instead of one giant DELETE.
    """
    from app.db.init_db import get_session
    from app.db.models import Application, FunnelEvent, Job

    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    deleted = 0
    for _ in range(max_batches):
        with get_session() as session:
            ids = [r[0] if isinstance(r, tuple) else r for r in session.exec(
                select(Job.id)
                .where(
                    Job.is_closed == True,          # noqa: E712
                    Job.first_seen < cutoff,
                    Job.id.not_in(select(Application.job_id)),  # never delete an applied job
                )
                .limit(batch)
            ).all()]
            if not ids:
                break
            # Release the funnel's FK first. funnel_events.job_id references
            # job.id, so deleting a job it still points at raises
            # funnel_events_job_id_fkey and the whole purge batch rolls back —
            # which is why the retention job had stopped reclaiming anything.
            # The column is nullable and the events are counted by STAGE, so
            # detaching keeps the analytics and frees the row.
            session.exec(
                update(FunnelEvent)
                .where(FunnelEvent.job_id.in_(ids))
                .values(job_id=None)
            )
            session.exec(delete(Job).where(Job.id.in_(ids)))
            session.commit()
            deleted += len(ids)
        if len(ids) < batch:
            break
    if deleted:
        log.info("Job retention: purged %d closed job(s) older than %dd with no application", deleted, days)
    return deleted


def strip_dead_descriptions(days: int = 14, batch: int = 2000, max_batches: int = 100) -> int:
    """Blank the JD text on old rows that will never be read again.

    ``description`` averages ~5.8 KB per row and is by far the largest thing in
    the database — 3.3 GB of TOAST across 1.1M rows, i.e. more than half the
    disk. It is also, for most of those rows, dead weight: the whole funnel is
    fresh-only (``scoring_max_job_age_days`` / ``shortlist_max_age_days`` = 5),
    so a two-week-old posting is never scored, never retrieved and never shown.

    Deliberately narrower than the purge, because the row itself is worth
    keeping — the dedupe key stops discovery re-inserting the posting and
    paying to score it all over again. Only the text goes.

    THREE carve-outs, all load-bearing:

    * a job with an Application is never touched — the user acted on it, and
      skill-gap, autopsy and re-tailoring all read the JD back;
    * a job with a REAL score keeps its text: (description, rerank_score) pairs
      are the distillation training set (docs/DISTILLATION.md), which is what
      keeps scoring alive when LLM credits run out;
    * jobs stamped "Expired unscored" by the scoring lane are fair game — that
      score is a placeholder, not a judgement, so there is nothing to learn
      from them.
    """
    from app.db.init_db import get_session
    from app.db.models import Application, Job
    from sqlalchemy import func as _func, or_, update

    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    age = _func.coalesce(Job.posted_at, Job.first_seen, Job.discovered_at)
    stripped = 0
    for _ in range(max_batches):
        with get_session() as session:
            ids = [r[0] if isinstance(r, tuple) else r for r in session.exec(
                select(Job.id)
                .where(
                    age < cutoff,
                    Job.description != "",
                    Job.description.is_not(None),
                    # Never a job the user engaged with.
                    Job.id.not_in(select(Application.job_id)),
                    # Unscored, or scored only by the staleness stamp.
                    or_(Job.rerank_score.is_(None),
                        Job.rerank_reasoning.like("Expired unscored%")),
                )
                .limit(batch)
            ).all()]
            if not ids:
                break
            session.exec(update(Job).where(Job.id.in_(ids)).values(description=""))
            session.commit()
            stripped += len(ids)
        if len(ids) < batch:
            break
    if stripped:
        log.info("Job retention: blanked description on %d stale unscored job(s) older than %dd",
                 stripped, days)
    return stripped
