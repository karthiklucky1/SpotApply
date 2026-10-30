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


def close_stale_user_jobs(days: int = 7, batch: int = 2000, max_batches: int = 100,
                          pause_seconds: float = 0.5) -> int:
    """Age-close OPEN per-user rows older than ``days`` that have no Application.

    The missing half of retention (CAPACITY.md §5.1): shared-pool rows are
    closed at 45d, but per-user adopted copies were never age-closed, so they
    accumulated forever and could never reach the purge. A 45-day-old posting
    is filled or ghost; anything a user acted on has an Application and is
    never touched. Batched id-select → targeted UPDATE, mirroring the purge,
    so no single statement can hit the statement timeout.

    The window is USER_JOB_CLOSE_AGE_DAYS (7 since 2026-09-28; was 45, which
    kept one account at 68k open rows). ``pause_seconds`` spaces the batches:
    the first run after that change closes tens of thousands of rows, and a
    database that is already disk-bound must keep serving sign-ins meanwhile.
    """
    import time as _time
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
        if pause_seconds > 0:
            _time.sleep(pause_seconds)
    if closed:
        log.info("Job retention: age-closed %d stale per-user job(s) older than %dd", closed, days)
    return closed


def close_stale_shared_jobs(days: int = 45, batch: int = 5000, max_batches: int = 200,
                            max_seconds: float = 300.0, statement_seconds: int = 30,
                            pause_seconds: float = 0.2) -> int:
    """Age-close shared-pool rows, reading each old row ONCE per run.

    This was one unbounded ``UPDATE job SET is_closed = true WHERE user_id =
    '__shared__' AND first_seen < cutoff`` written inline in the maintenance
    job, and it hit Supabase's statement timeout EVERY night from at least
    2026-09-05 to 09-11. Nothing downstream noticed, because the failure was
    caught and logged at WARNING: shared rows were never closed, so they were
    never purged either, and the table (and every scan's egress) grew without
    bound while the job reported itself as merely "failed".

    The first fix batched it (select open ids LIMIT n, update, repeat) and it
    still failed on 4 of 5 runs 2026-10-28..29. ``is_closed`` is not in
    ``ix_job_user_firstseen``, so every batch re-walked the rows the batches
    before it had just closed, and the LAST batch — the one that proves nothing
    is left — walked the whole closed band between this cutoff and the 60-day
    purge (~340k rows, a heap fetch each) inside one statement.

    Now it is a keyset walk down ``(first_seen, id)`` from the cutoff: each
    statement reads the next ``batch`` rows whatever their state, closes the
    open ones among them, and moves on, so no row is read twice and no
    statement reads more than ``batch`` rows (2.4 s per 5,000 cold, measured).
    Each statement has its own ceiling and the run its own budget; what one
    chunk committed stays committed, and the walk covers the whole band each
    run, so a posting reopened below the cutoff is still found.
    """
    import time as _time

    from sqlalchemy import or_

    from app.db.init_db import get_session
    from app.db.models import Job
    from app.discovery.pipeline import SHARED_POOL_USER
    from app.strategy.scoring_lane import _arm_statement_timeout, _is_statement_timeout

    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    started = _time.monotonic()
    closed = read = 0
    cursor = None              # (first_seen, id) of the last row read
    stopped = "complete"
    for n in range(max_batches):
        if n and _time.monotonic() - started >= max_seconds:
            stopped = "budget"
            break
        try:
            with get_session() as session:
                _arm_statement_timeout(session, statement_seconds)
                q = select(Job.id, Job.first_seen, Job.is_closed).where(
                    Job.user_id == SHARED_POOL_USER,
                    Job.first_seen < cutoff,
                )
                if cursor is not None:
                    fs, last_id = cursor
                    q = q.where(Job.first_seen <= fs,
                                or_(Job.first_seen < fs, Job.id < last_id))
                rows = session.exec(
                    q.order_by(Job.first_seen.desc(), Job.id.desc()).limit(batch)
                ).all()
                if not rows:
                    break
                open_ids = [r[0] for r in rows if not r[2]]
                if open_ids:
                    session.exec(
                        update(Job)
                        .where(Job.id.in_(open_ids),
                               Job.is_closed == False)  # noqa: E712 — closed meanwhile keeps its reason
                        .values(is_closed=True,
                                closed_reason=f"shared-pool retention ({days}d)")
                    )
                session.commit()
        except Exception as e:
            stopped = "statement_timeout" if _is_statement_timeout(e) else "error"
            log.warning("Job retention: shared-pool close stopped (%s) after %d read, %d closed: %s",
                        stopped, read, closed, type(e).__name__)
            break
        read += len(rows)
        closed += len(open_ids)
        cursor = (rows[-1][1], rows[-1][0])
        if len(rows) < batch:
            break
        if pause_seconds > 0:
            _time.sleep(pause_seconds)
    else:
        stopped = "max_batches"
    log.info("Job retention: closed %d stale shared-pool posting(s) older than %dd "
             "(%d read, %s, %.0fs)", closed, days, read, stopped,
             _time.monotonic() - started)
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

    Age here is KNOWN age — how long WE have held the row — not the source's
    posted_at (app/common/freshness.py). On the old
    ``coalesce(posted_at, first_seen, discovered_at)`` measure this stripped the
    JD off a posting discovered TODAY whenever a source dated it two weeks back,
    and since the scoring window now accepts source dates up to
    ``scoring_max_posted_age_days``, that would delete the description out from
    under a job still queued to be scored.
    """
    from app.common.freshness import known_ref
    from app.db.init_db import get_session
    from app.db.models import Application, Job
    from sqlalchemy import or_, update

    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    age = known_ref()
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
                    # Unscored, or "scored" only by the age-expiry stamp.
                    # expired_at is the reliable form; the reasoning LIKE keeps
                    # rows stamped before that column shipped in scope.
                    or_(Job.rerank_score.is_(None),
                        Job.expired_at.is_not(None),
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
