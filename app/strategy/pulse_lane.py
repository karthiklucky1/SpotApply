"""Pulse lane — the freshness guarantee.

Replaces the hot lane's rotating fixed-size batches with a per-board schedule
(``CompanyRegistry.next_poll_at``) that enforces two promises:

  * FAST (minutes): boards for companies any user follows ("My Companies")
    and boards that posted a new job in the last ``pulse_active_days`` days are
    polled every ``pulse_fast_interval_minutes``.
  * FLOOR (within the hour): every other LIVE board is polled at least every
    ``pulse_floor_interval_minutes`` — no company can go stale for hours just
    because it hasn't posted lately.
  * Dead weight (404s / boards that have never held a job) decays to a daily
    retry so the budget goes to boards that can actually produce.

A board that posts anything auto-promotes to FAST (``last_new_job_at`` moves),
so a "random" company only ever pays the floor price once.

Cheap-by-default: each poll computes a signature of the board's posting list
(ids + titles). Unchanged board → zero downstream work (no upserts, no per-user
routing). Only changed boards touch the DB. Description-only edits don't move
the signature — the fresh/full lanes' content-hash upserts still catch those.

Brand-new jobs take a PER-JOB FAST PATH: role match → cheap-tier prescore →
Claude score → shortlist (daily limit + company cap respected) → fresh alert —
no waiting for the next batch matching tick, and no discovery lock (the fast
path never loads FAISS or the embedding model).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor, TimeoutError as _FutureTimeout, as_completed as _as_completed,
)
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import select

from app.common.freshness import GHOST_SENTINEL_SCORE
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    Application, ApplicationStatus, CompanyRegistry, FunnelEvent, Job, UserProfile,
)

log = logging.getLogger(__name__)


def _record_spend(uid: str | None, kind: str) -> None:
    """Per-user spend attribution for fast-path LLM calls — never raises."""
    try:
        from app.analytics.spend import record_llm_spend
        record_llm_spend(uid, kind)
    except Exception:
        pass


def _norm(s: str) -> str:
    """Alphanumeric-only lowercase form for fuzzy company/slug comparison."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _watchlist_terms() -> set[str]:
    """Union of every user's followed companies, normalized. One shared fast
    lane serves all tenants (scrape once, serve many)."""
    terms: set[str] = set()
    try:
        with get_session() as session:
            rows = session.exec(select(UserProfile.target_companies)).all()
        for tc in rows:
            for part in (tc or "").split(","):
                n = _norm(part)
                if n:
                    terms.add(n)
    except Exception as e:
        log.debug("pulse: watchlist load failed: %s", e)
    return terms


def _is_watched(row: CompanyRegistry, terms: set[str]) -> bool:
    if not terms:
        return False
    slug_n = _norm(row.slug)
    name_n = _norm(row.company_name or "")
    for t in terms:
        if t and (t == slug_n or t == name_n
                  or (len(t) >= 4 and (t in slug_n or t in name_n))):
            return True
    return False


def _cadence(row: CompanyRegistry, terms: set[str], now: datetime) -> timedelta:
    """How long until this board's next poll, from its signals."""
    if _is_watched(row, terms):
        return timedelta(minutes=settings.pulse_fast_interval_minutes)
    if row.last_new_job_at and \
            (now - row.last_new_job_at) <= timedelta(days=settings.pulse_active_days):
        return timedelta(minutes=settings.pulse_fast_interval_minutes)
    if (row.job_count or 0) > 0:
        return timedelta(minutes=settings.pulse_floor_interval_minutes)
    # Never held a job (or 404s en route to retirement) — daily retry.
    return timedelta(hours=settings.pulse_dead_interval_hours)


def _due_boards(now: datetime, limit: int) -> list[CompanyRegistry]:
    """Boards whose next_poll_at has arrived (never-scheduled boards first, then
    least-recently-polled), capped so a backlog stretches the floor honestly
    instead of stampeding the container."""
    with get_session() as session:
        return session.exec(
            select(CompanyRegistry)
            .where(CompanyRegistry.is_active == True,  # noqa: E712
                   (CompanyRegistry.next_poll_at == None)  # noqa: E711
                   | (CompanyRegistry.next_poll_at <= now))
            .order_by(CompanyRegistry.next_poll_at.asc().nulls_first(),
                      CompanyRegistry.last_seen.asc().nulls_first())
            .limit(limit)
        ).all()


def _board_signature(raw: list) -> str:
    """Signature of a board's posting list: which jobs exist (id + title)."""
    keys = sorted(f"{r.external_id}|{(r.title or '')[:80]}" for r in raw)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _set_schedule(slug: str, ats, next_at: datetime, poll_hash: Optional[str]) -> None:
    """Move ONE board's next_poll_at. Kept for callers outside the tick loop
    (the watchlist route pulls followed boards forward). The tick itself folds
    the schedule into ``_mark_polled``'s write, or batches it via
    ``_defer_boards`` — one round-trip per board instead of two."""
    try:
        with get_session() as session:
            row = session.exec(
                select(CompanyRegistry).where(
                    CompanyRegistry.slug == slug, CompanyRegistry.ats == ats)
            ).first()
            if not row:
                return
            row.next_poll_at = next_at
            if poll_hash is not None:
                row.poll_hash = poll_hash
            session.add(row)
            session.commit()
    except Exception as e:
        log.debug("pulse: schedule update failed for %s: %s", slug, e)


def _defer_boards(boards: list, now: Optional[datetime] = None) -> int:
    """Reschedule boards the tick NEVER FETCHED — soon, and as deferred.

    This is the fix for the lane's central lie. Every one of these boards used
    to be handed to ``_set_schedule`` with the full normal cadence, i.e. the
    exact write a *successful* poll performs. Nothing had been fetched, nothing
    had been learned, and yet ``next_poll_at`` advanced an hour (or five
    minutes, or a day) as though the board had been checked. With ~88% of each
    tick's selection being deferred, the registry's schedule — and therefore
    ``overdue_boards``, ``floor_holding`` and the dashboard's sweep claim —
    described a poll rate the lane was not achieving.

    What this does NOT touch is the point: ``last_seen`` (the "we checked it"
    record), ``poll_hash``, ``job_count``, ``failure_count`` and
    ``last_new_job_at`` all stay exactly as they were. A deferred board is
    indistinguishable from one that was never selected, except that it comes
    back quickly.

    ONE round-trip for the whole batch (executemany by primary key). The old
    per-board write was ~2-3 serial Supabase round-trips EACH, in the main
    thread, inside the tick's fetch window — hundreds of round-trips per tick
    spent recording that nothing had been done, which is itself a large part of
    why so much of the batch had to be deferred.
    """
    if not boards:
        return 0
    from sqlalchemy import update as _update
    now = now or datetime.utcnow()
    base = max(1, int(settings.pulse_deferred_retry_minutes or 1))
    jitter = max(0, int(settings.pulse_deferred_retry_jitter_seconds or 0))
    rows = []
    for b in boards:
        bid = getattr(b, "id", None)
        if bid is None:
            continue
        # Jitter derived from the board id, so it is STABLE per board: a random
        # spread would re-shuffle the tail every tick and let the same boards
        # keep losing the race. Deterministic offsets keep the rotation fair.
        offset = (bid % (jitter + 1)) if jitter else 0
        rows.append({"id": bid,
                     "next_poll_at": now + timedelta(minutes=base, seconds=offset)})
    if not rows:
        return 0
    try:
        with get_session() as session:
            session.execute(_update(CompanyRegistry), rows)
            session.commit()
        return len(rows)
    except Exception as e:
        log.warning("pulse: deferral reschedule failed for %d board(s): %s",
                    len(rows), e)
        return 0


# ── Per-job fast path ─────────────────────────────────────────────────────────

def _fast_path_user(uid: str, score_budget: int,
                    deadline: Optional[float] = None) -> tuple[int, int, int]:
    """Score this user's brand-new unscored jobs RIGHT NOW (bounded), shortlist
    the fits, and fire fresh alerts. Returns (scored, shortlisted, alerts).
    Stops early once ``deadline`` (monotonic) passes so it can't overrun the tick.

    Deliberately lock-free: rule filter + ghost check + LLM cascade only — no
    FAISS/embedding model. Anything left unscored (budget, errors) is picked up
    by the 5-min matching lane, so this can only make things faster, never drop
    a job."""
    from app.matching.pipeline import (
        _AUTOFILL_SOURCES, _check_and_enforce_company_cap, _load_resume,
    )
    from app.matching.reranker import Reranker, llm_budget_exhausted
    from app.matching.filters import score_ghost
    from app.strategy.scoring_lane import _finals_allowance

    # Budget gate BEFORE any spend. The fast path had no cycle-level check at
    # all: it paid for a Tier-1 prescore per job and only then discovered the
    # budget was gone, when reranker.score() raised. At 10 finals/tick and up to
    # 1,440 ticks/day that leaked thousands of prescores a day for nothing.
    if llm_budget_exhausted():
        return 0, 0, 0
    # And this user's own plan allowance — the fast path spends the same budget
    # as the scoring lane, so it has to respect the same ceiling AND the same
    # promise bar. Taking only the slice size would let burst money be spent
    # here under the everyday gate, which is exactly the spend the adaptive
    # budget's Test A exists to prevent (app/matching/finals_budget.py).
    allow = _finals_allowance(uid, score_budget)
    score_budget = allow.n
    if score_budget <= 0:
        return 0, 0, 0
    from app.matching.hire_probability import (
        blended_score as compute_blended, score_hire_probability,
    )
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    uid_arg = None if (not uid or uid == "local") else uid
    try:
        resume = _load_resume(user_id=uid_arg)
    except Exception as e:
        log.debug("pulse fast-path: no resume for %s (%s) — skipping", uid, e)
        return 0, 0, 0

    profile = None
    try:
        from app.autofill.answer_pack import _get_or_create_profile
        profile = _get_or_create_profile(user_id=uid_arg)
    except Exception:
        pass
    reranker = Reranker(profile=profile)

    cutoff = datetime.utcnow() - timedelta(minutes=15)
    posted_cut = datetime.utcnow() - timedelta(hours=48)
    roles = [r.strip().lower()
             for r in (getattr(profile, "target_roles", "") or "").split(",") if r.strip()]
    with get_session() as session:
        # Pull a WIDER slice of fresh postings than we can score, so we can pick
        # the on-role ones. Board dumps (e.g. Rippling serving a whole company's
        # departments) flood the newest-first list with off-target titles
        # (Mechanical Engineer, Contact Center Analyst); scoring newest-first
        # could spend the whole LLM budget on those while a genuinely-fresh
        # AI/ML match waits for the slower matching lane.
        rows = session.exec(
            select(Job.id, Job.title).where(
                Job.user_id == uid_arg,
                Job.rerank_score == None,  # noqa: E711
                Job.is_closed == False,  # noqa: E712
                Job.first_seen >= cutoff,
                # Fast-path LLM spend is reserved for genuinely fresh postings.
                # Jobs first-seen now but POSTED long ago (e.g. the scheduler
                # bootstrap adopting weeks of backlog) wait for the regular
                # matching lane — they can't produce a valid fresh alert anyway.
                (Job.posted_at == None) | (Job.posted_at >= posted_cut),  # noqa: E711
            ).order_by(Job.first_seen.desc()).limit(max(score_budget * 6, 60))
        ).all()
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        q = select(Application).where(Application.created_at >= today_start)
        q = q.where(Application.user_id == uid_arg) if uid_arg \
            else q.where(Application.user_id.is_(None))
        today_count = len(session.exec(q).all())

    # Relevance-first: score titles matching the user's target roles before the
    # off-role remainder (still newest-first within each group). Off-role fresh
    # jobs aren't dropped — the matching lane scores them next pass.
    if roles:
        from app.discovery.title_filter import role_title_match
        on_role = [jid for jid, t in rows if role_title_match(t or "", roles)]
        on_set = set(on_role)
        off_role = [jid for jid, t in rows if jid not in on_set]
        fresh_ids = (on_role + off_role)[:score_budget]
    else:
        fresh_ids = [jid for jid, _t in rows][:score_budget]
    if not fresh_ids:
        return 0, 0, 0

    use_prescore = settings.prescore_enabled and reranker.has_prescore_backend()
    # Two thresholds, same split as the scoring lane's _Ctx: below `gate` a job
    # is a misfit and is stamped out of the queue; between `gate` and
    # `spend_gate` it is KEPT for the soft budget — never stamped, or an
    # identical job's fate would depend on the time of day it was picked up.
    gate = min(settings.prescore_advance_threshold, settings.shortlist_score_threshold)
    spend_gate = max(gate, int(allow.gate))
    scored = 0
    shortlisted: list[int] = []
    from app.common.inflight import claim
    for jid in fresh_ids:
        if deadline is not None and time.monotonic() >= deadline:
            break  # out of tick budget — the matching lane scores the rest
        if llm_budget_exhausted():
            break  # hourly/daily cap tripped mid-loop — stop before paying Tier-1
        # NEVER hold a session across an LLM call (CLAUDE.md; the documented
        # cause of the "QueuePool limit reached" starvation). One job is three
        # phases: a short read+ghost session, the LLM calls with NO session
        # held, then a short idempotent write-back. The claim() lock spans all
        # three so another lane still cannot score this job concurrently.
        with claim(jid) as _owned:
            if not _owned:
                continue  # another lane is scoring this job right now

            # ── Phase 1: read + ghost gate (short session) ──────────────────
            with get_session() as session:
                job = session.get(Job, jid)
                if not job or job.rerank_score is not None or job.is_closed:
                    continue
                try:
                    g = score_ghost(job, session)
                    job.ghost_score = g.ghost_score
                    job.ghost_flags = g.flags_json
                    if g.is_ghost:
                        job.rerank_score = GHOST_SENTINEL_SCORE
                        job.rerank_reasoning = f"Ghost filtered (score={g.ghost_score:.2f}): {', '.join(g.flags)}"
                        job.scored_at = datetime.utcnow()
                        session.add(job)
                        session.commit()
                        continue
                    session.add(job)
                    session.commit()
                except Exception as e:
                    log.debug("pulse fast-path ghost check failed for %d: %s", jid, e)
                # Detach a fully-loaded copy for the LLM phase — the scorers
                # only read fields, and the connection goes back to the pool now.
                session.refresh(job)
                session.expunge(job)

            # ── Phase 2: LLM calls, no DB connection held ───────────────────
            prescore_val = None
            if use_prescore:
                pre = reranker.prescore(resume, job)
                _record_spend(uid, "score_prescore")
                if pre is not None:
                    prescore_val = float(pre[0])
                if pre is not None and pre[0] < gate:
                    # Tier-1 rejection — stamp it and move on.
                    with get_session() as session:
                        fresh = session.get(Job, jid)
                        if fresh is not None:
                            _now = datetime.utcnow()
                            fresh.prescore = prescore_val
                            fresh.rerank_score = prescore_val
                            fresh.rerank_reasoning = f"Pre-screened (Tier-1 fit {int(pre[0])}): {pre[1]}"[:500]
                            fresh.prescored_at = fresh.prescored_at or _now
                            fresh.scored_at = _now
                            session.add(fresh)
                            session.commit()
                    scored += 1
                    continue
                if pre is not None and pre[0] < spend_gate:
                    # Not a misfit, not strong enough for burst money: leave it
                    # Queued with its prescore ON THE ROW, so the scoring lane
                    # sorts it by promise once the soft budget reopens.
                    with get_session() as session:
                        fresh = session.get(Job, jid)
                        if fresh is not None and fresh.rerank_score is None:
                            fresh.prescore = prescore_val
                            fresh.prescored_at = fresh.prescored_at or datetime.utcnow()
                            session.add(fresh)
                            session.commit()
                    continue

            try:
                score, reason, concerns, breakdown = reranker.score(resume, job)
            except Exception as e:
                log.debug("pulse fast-path score failed for %d (left for matching lane): %s", jid, e)
                continue
            _record_spend(uid, "score_final")
            scored += 1

            # ── Phase 3: write back (short session) ─────────────────────────
            with get_session() as session:
                job = session.get(Job, jid)
                if job is None:
                    continue
                _now = datetime.utcnow()
                if prescore_val is not None:
                    job.prescore = prescore_val
                    job.prescored_at = job.prescored_at or _now
                job.rerank_score = score
                job.scored_at = _now
                job.rerank_reasoning = reason + (("\nConcerns: " + "; ".join(concerns)) if concerns else "")
                job.rerank_breakdown = json.dumps(breakdown) if breakdown else None
                try:
                    hp = score_hire_probability(job, session)
                    job.hire_probability_score = hp.score
                    job.hire_probability_signals = json.dumps(hp.signals)
                    job.blended_score = compute_blended(score, hp.score)
                except Exception:
                    pass
                session.add(job)

                if score >= settings.shortlist_score_threshold \
                        and today_count < settings.daily_shortlist_limit:
                    existing = session.exec(
                        select(Application).where(Application.job_id == job.id)
                    ).first()
                    if not existing and _check_and_enforce_company_cap(session, job, score):
                        track = "autofill" if job.source in _AUTOFILL_SOURCES else "manual"
                        session.add(Application(
                            job_id=job.id, status=ApplicationStatus.SHORTLISTED,
                            apply_url=job.url, apply_track=track, user_id=uid_arg,
                        ))
                        shortlisted.append(job.id)
                        today_count += 1
                session.commit()

    alerts = 0
    if shortlisted:
        try:
            alerts = dispatch_fresh_alerts(uid, shortlisted)
        except Exception as e:
            log.warning("pulse fast-path alerts failed for %s: %s", uid, e)
    return scored, len(shortlisted), alerts


# ── One scheduler tick ────────────────────────────────────────────────────────

# One tick at a time — but SELF-HEALING. A plain lock froze the whole lane once
# a single slow tick (serial LLM scoring for 20+ min) held it. Now: a tick is
# hard-bounded by pulse_tick_max_seconds (it stops taking new work and releases
# promptly), and if a holder ever overruns a generous grace window it's treated
# as dead and the next tick proceeds anyway — the lane can never freeze forever.
_TICK_LOCK = threading.Lock()
_TICK_DEADLINE = [0.0]  # monotonic time by which the current holder must be done

# One fetch pool for the life of the process — see scoring_lane._worker_pool.
# A fresh 24-thread pool per 60s tick, abandoned with shutdown(wait=False), was
# constant OS-thread (and glibc-arena) churn: the RSS-climbs-forever pattern.
_FETCH_POOL = None
_FETCH_POOL_LOCK = threading.Lock()


def _fetch_pool() -> ThreadPoolExecutor:
    global _FETCH_POOL
    if _FETCH_POOL is None:
        with _FETCH_POOL_LOCK:
            if _FETCH_POOL is None:
                _FETCH_POOL = ThreadPoolExecutor(
                    max_workers=max(1, settings.pulse_fetch_workers),
                    thread_name_prefix="pulse-fetch",
                )
    return _FETCH_POOL


def run_pulse_tick() -> dict:
    """Poll every board that's due, route changes, fast-path new jobs to alerts.
    Returns tick stats; records a ``pulse_tick`` FunnelEvent when work was done."""
    now_m = time.monotonic()
    got = _TICK_LOCK.acquire(blocking=False)
    if not got:
        if now_m < _TICK_DEADLINE[0]:
            log.info("Pulse tick skipped — previous tick still running")
            return {"boards": 0, "skipped": "previous tick still running"}
        # Holder blew past its grace window → hung/abandoned thread. Proceed
        # without the lock so the lane recovers instead of freezing forever.
        log.warning("Pulse tick: prior tick overran its grace window — proceeding")
    work_deadline = now_m + settings.pulse_tick_max_seconds
    # Grace: work deadline + a cushion for in-flight ops to drain before a steal.
    _TICK_DEADLINE[0] = work_deadline + 90
    try:
        return _run_pulse_tick_locked(work_deadline)
    finally:
        if got:
            _TICK_LOCK.release()


def _run_pulse_tick_locked(deadline: float) -> dict:
    from app.discovery.pipeline import SHARED_POOL_USER, _upsert, scraper_for
    from app.strategy.hot_lane import (
        _active_users, _mark_polled, _retire_unsupported, _title_matches,
    )

    now = datetime.utcnow()
    boards = _due_boards(now, settings.pulse_max_boards_per_tick)
    # TELEMETRY CONTRACT — every board selected lands in EXACTLY ONE outcome
    # bucket, and the buckets sum to `selected`. Before this, the only number
    # recorded was `boards` (the SELECTION), so every consumer that wanted a
    # poll count multiplied ticks x selected and got a figure ~8x the real one:
    # scripts/pulse_check.py printed it verbatim as "board polls". A board is
    # only ever counted as polled if its fetch actually completed.
    #
    #   selected   = boards pulled off the due schedule (`boards` is kept as an
    #                alias for older tick events; it was ALWAYS the selection)
    #   started    = fetches that actually began running
    #   fetch_ok   = fetches that completed and returned a posting list
    #   fetch_failed / unsupported = fetches that ran and errored
    #   deferred   = never recorded as a poll, split three ways:
    #                cancelled (never started) / running (still in flight at the
    #                deadline) / unconsumed (finished, no time left to process)
    #   unchanged/changed = the fetch_ok split by board signature
    stats = {"boards": len(boards), "selected": len(boards),
             "started": 0, "fetch_ok": 0, "fetch_failed": 0, "unsupported": 0,
             "deferred": 0, "deferred_cancelled": 0, "deferred_running": 0,
             "deferred_unconsumed": 0,
             "unchanged": 0, "changed": 0, "fetched_jobs": 0,
             "new_jobs": 0, "scored": 0, "shortlisted": 0, "alerts": 0}
    # RESERVE ~40% of the budget for SCORING. During the bootstrap backlog the
    # fetch/route phase would otherwise eat the whole tick (deferring hundreds of
    # boards) and score ZERO — so fresh jobs land but sit "Queued" until the
    # slower matching lane reaches them. Stopping fetch early leaves guaranteed
    # time for the fast path to score the freshest on-role jobs each tick. When
    # fetch finishes early (steady state) the fast path just gets more time.
    fetch_deadline = deadline - max(0.0, settings.pulse_tick_max_seconds * 0.4)
    if not boards:
        return stats

    users = _active_users()
    terms = _watchlist_terms()
    all_roles = sorted({r for u in users for r in (u["roles"] or [])})

    def _fetch(board):
        """Runs on the pool. Times itself so the tick can report REAL fetch
        latency — the number you need before touching worker counts."""
        t0 = time.monotonic()
        scraper = scraper_for(board.ats, board.slug, board.career_url)
        if scraper is None:
            return board, None, "unsupported", time.monotonic() - t0
        try:
            return board, scraper.fetch(), None, time.monotonic() - t0
        except Exception as e:
            return board, None, str(e), time.monotonic() - t0

    users_touched: set[str] = set()
    latencies: list[float] = []
    deferred_boards: list = []
    backoff = int(getattr(settings, "pulse_failure_backoff_minutes", 0) or 0)
    backoff_cap = int(settings.pulse_dead_interval_hours or 24)
    pool = _fetch_pool()
    # Submit all fetches, then drain them IN COMPLETION ORDER. The old loop
    # walked them in SUBMISSION order, so one slow host at the head blocked the
    # collection of every board behind it that had already finished — the tick
    # hit its deadline holding a pile of completed, unprocessed results. Taking
    # them as they land converts that dead time into processed boards without
    # adding a single worker.
    futures = {pool.submit(_fetch, b): b for b in boards}
    try:
        for fut in _as_completed(list(futures), timeout=max(0.0, fetch_deadline - time.monotonic())):
            board = futures.pop(fut, None)
            if board is None:
                continue
            try:
                board, raw, err, elapsed = fut.result()
                latencies.append(elapsed)
            except Exception as e:
                # The fetch RAN and blew up — a real failure, so it decays the
                # board through the normal failure path (and its backoff).
                stats["fetch_failed"] += 1
                _mark_polled(board.slug, board.ats, job_count=None, ok=False,
                             error=str(e), failure_backoff_minutes=backoff,
                             failure_backoff_cap_hours=backoff_cap)
                continue
            finally:
                del fut

            if raw is None:
                if err == "unsupported":
                    stats["unsupported"] += 1
                    _retire_unsupported(board.slug, board.ats)
                else:
                    stats["fetch_failed"] += 1
                    _mark_polled(board.slug, board.ats, job_count=None, ok=False,
                                 error=err, failure_backoff_minutes=backoff,
                                 failure_backoff_cap_hours=backoff_cap)
                continue

            # From here the fetch COMPLETED — this board was genuinely polled.
            stats["fetch_ok"] += 1
            stats["fetched_jobs"] += len(raw)
            sig = _board_signature(raw)
            if raw and sig == (board.poll_hash or ""):
                # Unchanged board — zero downstream work. This is the common
                # case that makes the hourly floor affordable.
                stats["unchanged"] += 1
                _mark_polled(board.slug, board.ats, job_count=len(raw), ok=True,
                             next_poll_at=datetime.utcnow() + _cadence(board, terms, now),
                             poll_hash=sig)
                continue

            stats["changed"] += 1
            new_here = 0
            try:
                new_here += _upsert(raw, user_id=SHARED_POOL_USER, user_keywords=all_roles or None)
            except Exception as e:
                log.debug("pulse shared upsert failed %s: %s", board.slug, e)
            for u in users:
                relevant = [r for r in raw if _title_matches(r.title, u["roles"])]
                if not relevant:
                    continue
                try:
                    n = _upsert(relevant, user_id=(None if u["user_id"] == "local" else u["user_id"]),
                                user_keywords=u["roles"] or None)
                    if n:
                        users_touched.add(u["user_id"])
                        new_here += n
                except Exception as e:
                    log.debug("pulse user upsert failed %s/%s: %s", board.slug, u["user_id"], e)
            stats["new_jobs"] += new_here
            # A board that just posted has a fresh last_new_job_at, so it must
            # land on the fast lane immediately — reflect that in the cadence
            # we write in the same round-trip.
            board.last_new_job_at = datetime.utcnow() if new_here else board.last_new_job_at
            _mark_polled(board.slug, board.ats, job_count=len(raw), ok=True,
                         new_jobs=new_here,
                         next_poll_at=datetime.utcnow() + _cadence(board, terms, now),
                         poll_hash=sig)
    except _FutureTimeout:
        pass  # out of fetch budget — whatever is left is deferred, below

    # Everything still in `futures` was never turned into a poll record. NOT
    # polled: each gets a short deferral and none of the registry fields a poll
    # would move.
    #
    # The three-way split is the diagnosis, and it says which lever (if any) is
    # the right one — the reason this patch measures before it tunes:
    #   cancelled  — never started. The tick could not even begin them:
    #                too few worker-seconds for the batch size.
    #   running    — started, still in flight at the deadline. Slow hosts;
    #                a per-fetch timeout, not more workers, is the answer.
    #   unconsumed — the FETCH FINISHED and the tick ran out of time to process
    #                the result. The bottleneck is the serial main-thread work
    #                downstream of the fetch (registry writes, upserts), and
    #                adding workers would make it strictly worse.
    for fut, board in futures.items():
        if fut.cancel():
            stats["deferred_cancelled"] += 1
        elif fut.done():
            stats["deferred_unconsumed"] += 1
        else:
            stats["deferred_running"] += 1
        deferred_boards.append(board)
    stats["deferred"] = len(deferred_boards)
    # Submitting is not starting: with 24 workers and 300 submissions, most sit
    # in the pool's queue. `started` is what actually ran — everything except
    # the futures we managed to cancel before they were picked up.
    stats["started"] = stats["selected"] - stats["deferred_cancelled"]
    _defer_boards(deferred_boards)
    futures.clear()

    if latencies:
        latencies.sort()
        stats["fetch_p50_ms"] = int(latencies[len(latencies) // 2] * 1000)
        stats["fetch_p95_ms"] = int(latencies[min(len(latencies) - 1,
                                                  int(len(latencies) * 0.95))] * 1000)
        stats["fetch_max_ms"] = int(latencies[-1] * 1000)

    # Per-job fast path for every user who just received something new — best
    # effort within whatever wall-clock time the tick has left. Anything not
    # scored here is picked up by the 5-min matching lane, so this only speeds
    # alerts, never blocks the tick (which used to run serial LLM for 20+ min).
    budget = settings.pulse_fast_path_score_cap
    for uid in users_touched:
        if budget <= 0 or time.monotonic() >= deadline:
            break
        try:
            scored, short, alerts = _fast_path_user(uid, budget, deadline)
            budget -= scored
            stats["scored"] += scored
            stats["shortlisted"] += short
            stats["alerts"] += alerts
        except Exception as e:
            log.warning("pulse fast-path failed for %s: %s", uid, e)

    try:
        with get_session() as session:
            session.add(FunnelEvent(
                job_id=None, stage="pulse_tick",
                # `passed` now means "this tick actually polled something", so a
                # run that selected 300 boards and completed none stops reading
                # as a success in the funnel.
                passed=stats["fetch_ok"] > 0,
                reason=(f"selected={stats['selected']} polled={stats['fetch_ok']} "
                        f"deferred={stats['deferred']} new={stats['new_jobs']}"),
                metadata_json=json.dumps(stats),
            ))
            session.commit()
    except Exception as e:
        log.debug("pulse tick event write failed: %s", e)
    # Say the capacity story out loud every tick. A lane that can only complete
    # a fraction of what it selects is not "catching up", and this is the line
    # that makes that visible in the logs without a DB query.
    if stats["deferred"] and stats["selected"]:
        log.warning(
            "Pulse tick CAPACITY-LIMITED: polled %d/%d selected (%.0f%% deferred: "
            "%d never started, %d still running, %d fetched-but-unprocessed) · "
            "fetch p50=%sms p95=%sms · %s",
            stats["fetch_ok"], stats["selected"],
            100.0 * stats["deferred"] / stats["selected"],
            stats["deferred_cancelled"], stats["deferred_running"],
            stats["deferred_unconsumed"],
            stats.get("fetch_p50_ms", "?"), stats.get("fetch_p95_ms", "?"), stats)
    else:
        log.info("Pulse tick: %s", stats)
    return stats
