"""Scoring lane — decoupled, parallel, cross-user job scoring.

The freshness lanes (pulse / fresh / full discovery) PRODUCE unscored "Queued"
jobs; this lane CONSUMES them. It drains the GLOBAL queue of unscored on-role
jobs across ALL users at once with a bounded pool of LLM workers, so scoring
throughput is bounded by the LLM rate limit — NOT by the number of users.

The old matching lane scored users one-at-a-time (``for uid in users:
run_matching(uid)``) — O(users), ~130s/user — so with 9 users a full cycle took
~20 min and fresh jobs sat "Queued". This lane is the fix: worker count is
independent of user count. 9 users or 10,000, the scorer runs the same 20
workers flat-out; a longer queue just means adding workers (or providers).

Design (producer/consumer):

    discovery lanes ──▶ [ Job.rerank_score IS NULL ] ──▶ scoring lane
       (produce Queued)      the DB IS the queue        (consume, in parallel)

  Phase A — SCORE (parallel, I/O-bound on the LLM): a bounded pool scores work
    items (user, job) — cheap ghost gate → GPT prescore (drain misfits) → Claude
    final. Each user's résumé/reranker is loaded ONCE and shared (thread-safe
    cache). Lock-free: no FAISS / embedding rebuild, so it runs continuously
    alongside discovery.
  Phase B — SHORTLIST (serial per user, cap-safe): jobs that cleared the bar
    become SHORTLISTED applications under the daily limit + per-company cap, then
    fresh alerts fire. Serial-per-user so the caps can't race.

Anything not reached in a cycle stays Queued and is drained next cycle (or by the
5-min matching lane's FAISS backstop) — so this only speeds scoring, never drops
a job.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from datetime import datetime
from typing import List, Optional, Tuple

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, FunnelEvent, Job

log = logging.getLogger(__name__)

_LANE_LOCK = threading.Lock()  # one scoring cycle at a time in this process

# One worker pool for the LIFE OF THE PROCESS, not one per cycle. A fresh
# 20-thread ThreadPoolExecutor every 90s — abandoned with shutdown(wait=False)
# — was ~19k new OS threads/day; every thread that touches malloc can pin a
# glibc arena, and arena high-water marks never shrink, so the churn read as an
# RSS climb that no heap profile could explain (docs/MEMORY.md). A persistent
# pool keeps a fixed set of threads (and arenas) instead.
_POOL: Optional[ThreadPoolExecutor] = None
_POOL_LOCK = threading.Lock()


def _worker_pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = ThreadPoolExecutor(
                    max_workers=max(1, settings.scoring_workers),
                    thread_name_prefix="scoring",
                )
    return _POOL

# ── Per-job attempt ceiling ───────────────────────────────────────────────────
# A job whose final score keeps failing (provider outage, poison payload) used
# to be re-selected EVERY 90s cycle forever — re-paying the prescore each time.
# After scoring_fail_max_attempts failures the job is deferred in memory for
# scoring_fail_defer_hours instead. Process-local by design: no schema change,
# and a restart merely retries a few times before deferring again.
_fail_counts: dict = {}
_deferred_until: dict = {}
_fail_lock = threading.Lock()


def _note_score_failure(jid: int) -> None:
    with _fail_lock:
        n = _fail_counts.get(jid, 0) + 1
        if n >= max(1, settings.scoring_fail_max_attempts):
            _fail_counts.pop(jid, None)
            _deferred_until[jid] = time.time() + settings.scoring_fail_defer_hours * 3600
            log.warning("Scoring: job %d failed %d attempts — deferred %.1fh",
                        jid, n, settings.scoring_fail_defer_hours)
        else:
            # Hard bound: a job that fails here but is later scored by another
            # lane never triggers _note_score_success, so its entry is immortal.
            if len(_fail_counts) > 50_000:
                _fail_counts.clear()
            _fail_counts[jid] = n


def _note_score_success(jid: int) -> None:
    with _fail_lock:
        _fail_counts.pop(jid, None)
        _deferred_until.pop(jid, None)


def _drop_deferred(jids: List[int]) -> List[int]:
    now = time.time()
    with _fail_lock:
        # purge expired deferrals so the dict stays small
        for j, until in list(_deferred_until.items()):
            if until <= now:
                _deferred_until.pop(j, None)
        return [j for j in jids if j not in _deferred_until]


def _deferred_ids() -> set:
    """Currently-deferred job ids (expired deferrals purged first)."""
    now = time.time()
    with _fail_lock:
        for j, until in list(_deferred_until.items()):
            if until <= now:
                _deferred_until.pop(j, None)
        return set(_deferred_until)


def _transient_llm_stall() -> bool:
    """True when NO job-specific work can succeed right now — the hourly/daily
    final budget is exhausted or every provider is in circuit-breaker cooldown.
    Failures under this condition are not the job's fault and must not count
    against its attempt ceiling."""
    from app.matching.reranker import any_provider_available, llm_budget_exhausted
    return llm_budget_exhausted() or not any_provider_available()


# Loose index scan ("skip scan"): walk the DISTINCT values of an indexed column
# by repeatedly seeking to the next one, instead of reading every row and
# deduping. Postgres has no native skip scan, so it is spelled as a recursive CTE.
#
# This is the only shape that works here, and the numbers say why. Of 411,916
# unscored OPEN jobs, 411,915 belong to '__shared__'. Filtering the shared pool
# out cannot make the query cheap, because the mass IS what has to be scanned to
# discover it is excluded — measured as a Parallel Seq Scan at 4,527 ms with
# 434,005 rows removed by filter per worker. Forcing it onto
# ix_job_unscored (user_id, first_seen) WHERE rerank_score IS NULL is FIVE TIMES
# WORSE (25,430 ms, 73,759 blocks): that index's predicate omits is_closed, so
# every candidate entry needs a heap fetch. The planner was right both times.
#
# The skip scan seeks past the entire shared block in a single index descent, so
# cost tracks the number of DISTINCT owners (~12), not the number of rows.
# Measured against the same index, same data: 35.6 ms, 85 buffers — ~128x.
_SKIP_SCAN_OWNERS = """
WITH RECURSIVE owners AS (
    -- IS NOT NULL is load-bearing, not decoration: Postgres sorts NULLs LAST in
    -- ASC, SQLite sorts them FIRST. Without it the anchor picks up the NULL
    -- owner on SQLite, the recursive term's `o.user_id IS NOT NULL` guard fires
    -- immediately, and the walk returns exactly one row. NULL owners are probed
    -- separately by the caller.
    SELECT (SELECT j.user_id FROM job j
             WHERE j.rerank_score IS NULL AND j.user_id IS NOT NULL
             ORDER BY j.user_id LIMIT 1) AS user_id
    UNION ALL
    SELECT (SELECT j.user_id FROM job j
             WHERE j.rerank_score IS NULL AND j.user_id > o.user_id
             ORDER BY j.user_id LIMIT 1)
      FROM owners o WHERE o.user_id IS NOT NULL
)
SELECT user_id FROM owners WHERE user_id IS NOT NULL
"""


def _unscored_owners_fast(session, limit: int) -> List[str]:
    """Distinct non-NULL owners having at least one unscored job, via skip scan.

    Returns owners with ANY unscored job; the caller re-checks `is_closed` per
    owner, because is_closed is not in the partial index and folding it in here
    would reintroduce the heap fetch that made the index path lose.
    """
    from sqlalchemy import text
    rows = session.execute(text(_SKIP_SCAN_OWNERS)).all()
    return [r[0] for r in rows if r[0] is not None][:limit]


def _scorable_user_ids(limit: int = 1000) -> List[Optional[str]]:
    """Distinct owners that currently have at least one unscored open job.
    The shared pool ('__shared__') is a corpus, not a user — its rows are never
    scored directly (they're adopted into per-user pools first), so it must not
    consume work slots."""
    from app.discovery.pipeline import SHARED_POOL_USER
    users: List[Optional[str]] = []
    with get_session() as session:
        try:
            candidates: List[Optional[str]] = list(
                _unscored_owners_fast(session, limit))
        except Exception as e:
            # Never let an optimisation stop the scoring lane. The plain DISTINCT
            # is slow but correct, so degrade to it rather than returning nothing.
            log.warning("skip-scan owner enumeration failed, using DISTINCT: %s", e)
            candidates = [r[0] if isinstance(r, tuple) else r for r in session.exec(
                select(Job.user_id).where(
                    Job.rerank_score == None,  # noqa: E711
                ).distinct().limit(limit)
            ).all()]
        # The NULL owner (local/legacy rows) can't be reached by `user_id >` in
        # the recursion — ORDER BY sorts NULLs last — so probe it explicitly.
        candidates.append(None)

        for uid in candidates:
            if uid == SHARED_POOL_USER:
                continue
            hit = session.exec(
                select(Job.id).where(
                    Job.user_id.is_(None) if uid is None else Job.user_id == uid,
                    Job.rerank_score == None,  # noqa: E711
                    Job.is_closed == False,    # noqa: E712
                ).limit(1)
            ).first()
            if hit is not None:
                users.append(uid)
    # Dormancy gate: even with adoption stopped, a vanished user's existing
    # unscored backlog would keep burning LLM budget (and round-robin slots
    # active users need). Skip them here too; their queue resumes on return.
    if settings.dormant_user_grace_days > 0 and users:
        from app.api.server import _user_is_active
        from app.db.models import UserProfile
        with get_session() as session:
            profiles = {p.user_id: p for p in session.exec(
                select(UserProfile).where(UserProfile.user_id.in_(
                    [u for u in users if u]))
            ).all()}
        users = [u for u in users
                 if u is None or u not in profiles or _user_is_active(profiles[u])]
    return users


def _user_queue(user_id: Optional[str], cap: int) -> List[int]:
    """A user's queued (unscored) job ids, freshest first, capped. Attempt-ceiling
    deferred jobs are excluded IN THE QUERY (not after the LIMIT) so a window of
    deferred fresh jobs can't crowd valid older jobs out of the capped freshest-
    first slice and starve them indefinitely."""
    deferred = _deferred_ids()
    with get_session() as session:
        q = select(Job.id).where(
            Job.user_id == user_id,
            Job.rerank_score == None,  # noqa: E711
            Job.is_closed == False,    # noqa: E712
        )
        # Exclude deferred ids in-SQL for the common (small) set; fall back to
        # post-filtering only if the deferred set is pathologically large.
        if deferred and len(deferred) <= 2000:
            q = q.where(Job.id.notin_(deferred))
        q = q.order_by(Job.first_seen.desc()).limit(cap)
        jids = [r[0] if isinstance(r, tuple) else r for r in session.exec(q).all()]
    return jids if len(deferred) <= 2000 else _drop_deferred(jids)


class _Ctx:
    __slots__ = ("resume", "reranker", "use_prescore", "gate")

    def __init__(self, resume, reranker, use_prescore, gate):
        self.resume = resume
        self.reranker = reranker
        self.use_prescore = use_prescore
        self.gate = gate


def _pick_provider(jid: int, ctx: "_Ctx") -> Optional[str]:
    """Option A: split the FINAL score across providers by job id — a stable,
    balanced ~claude_share/rest partition. Both providers score against the same
    rubric, so throughput becomes Claude's rate limit + GPT's, not just Claude's.
    Returns None (default priority order) when dual mode is off or only one
    provider is configured."""
    if not settings.dual_score_enabled or not ctx.reranker.has_dual():
        return None
    share = max(0.0, min(1.0, settings.dual_score_claude_share))
    return "anthropic" if (jid % 100) < int(round(share * 100)) else "openai"


def _stamp_job(jid: int, ghost: Optional[Tuple],
               score: float, reasoning: str,
               extras: Optional[Tuple] = None,
               prescore: Optional[float] = None) -> bool:
    """Short write-back session. Re-checks idempotency (another lane may have
    scored the job while we were on the LLM). Returns False if it lost the race."""
    with get_session() as session:
        job = session.get(Job, jid)
        if not job or job.rerank_score is not None or job.is_closed:
            return False
        if ghost is not None:
            job.ghost_score, job.ghost_flags = ghost
        job.rerank_score = score
        job.rerank_reasoning = reasoning
        if prescore is not None:
            job.prescore = float(prescore)
        if extras is not None:
            breakdown, hp_fn = extras
            job.rerank_breakdown = breakdown
            try:
                hp_fn(job, session)
            except Exception:
                pass
        session.add(job)
        session.commit()
    return True


# Tier-1 results for jobs whose FINAL score failed: the retry (next cycle, or
# after a deferral) reuses the prescore instead of paying the cheap model again.
# Entries are dropped the moment the job is scored/drained; the safety clear
# only guards against a pathological pile-up.
_prescore_memo: dict = {}


def _score_job(jid: int, ctx: _Ctx) -> Optional[Tuple[str, int, Optional[float], Optional[str]]]:
    """Score one queued job. Returns ("scored"|"drained", jid, score, provider)
    or None. ``provider`` is which backend produced the final score (for the
    dual-provider split stats); None for drained/ghost/single-provider jobs.
    Idempotent: a job already scored (by another worker / lane) is skipped, and
    the cross-lane in-flight claim stops two lanes paying to score the same job
    at the same time.

    DB DISCIPLINE: a connection is held only for the short read/write phases —
    NEVER across an LLM call. The old shape kept one session open for the whole
    function, so 20 workers × multi-second LLM latency pinned 20 connections and
    starved the pool for everything else (funnel/registry/web all timed out with
    "QueuePool limit reached")."""
    from app.common.inflight import claim
    with claim(jid) as ok:
        if not ok:
            return None  # another lane is scoring this job right now
        return _score_job_owned(jid, ctx)


def _score_job_owned(jid: int, ctx: _Ctx) -> Optional[Tuple[str, int, Optional[float], Optional[str]]]:
    from app.matching.filters import score_ghost
    from app.matching.hire_probability import (
        blended_score as compute_blended, score_hire_probability,
    )

    # Phase 1 — short session: load + idempotency + ghost gate (cheap, DB+text).
    ghost: Optional[Tuple] = None
    with get_session() as session:
        job = session.get(Job, jid)
        if not job or job.rerank_score is not None or job.is_closed:
            return None
        try:
            g = score_ghost(job, session)
            ghost = (g.ghost_score, g.flags_json)
            if g.is_ghost:
                job.ghost_score, job.ghost_flags = ghost
                job.rerank_score = 5.0
                job.rerank_reasoning = f"Ghost filtered (score={g.ghost_score:.2f}): {', '.join(g.flags)}"
                session.add(job)
                session.commit()
                return ("drained", jid, None, None)
        except Exception as e:
            log.debug("scoring ghost check failed for %d: %s", jid, e)
        # Detach with its loaded attributes — the LLM phase reads job fields
        # only, so it must not keep the session (and its connection) alive.
        session.expunge(job)

    # Phase 2 — NO session held: the slow LLM calls.
    # Cascade Tier-1: drain clear misfits without touching the final scorer.
    # A memoized prescore (from an attempt whose FINAL call failed) is reused so
    # retries only re-pay the step that actually failed.
    if ctx.use_prescore:
        pre = _prescore_memo.get(jid)
        if pre is None:
            pre = ctx.reranker.prescore(ctx.resume, job)
        if pre is not None and pre[0] < ctx.gate:
            reasoning = f"Pre-screened (Tier-1 fit {int(pre[0])}): {pre[1]}"[:500]
            if _stamp_job(jid, ghost, float(pre[0]), reasoning, prescore=float(pre[0])):
                _prescore_memo.pop(jid, None)
                return ("drained", jid, None, None)
            return None  # lost the race to another lane
    else:
        pre = None

    # Tier-2: authoritative score (dual routing when enabled; the rule
    # pre-filter runs inside .score()).
    provider = _pick_provider(jid, ctx)
    try:
        score, reason, concerns, breakdown = ctx.reranker.score(ctx.resume, job, provider=provider)
    except Exception as e:
        log.debug("scoring failed for %d (left for next cycle): %s", jid, e)
        if pre is not None:
            if len(_prescore_memo) > 10000:  # pathological pile-up guard
                _prescore_memo.clear()
            _prescore_memo[jid] = pre  # retry skips Tier-1
        # Transient, non-job-specific failures — the hourly/daily budget cap
        # tripping mid-cycle (the LLM_HOURLY_FINAL_CAP smoother firing) or every
        # provider being in circuit-breaker cooldown — must NOT count against
        # this job's attempt ceiling. Otherwise perfectly scorable fresh jobs
        # get deferred for scoring_fail_defer_hours purely because the budget
        # guard fired. Leave them Queued for the next eligible cycle, unpenalized.
        if _transient_llm_stall():
            return None
        _note_score_failure(jid)  # real per-job failure: attempt ceiling applies
        return None
    _note_score_success(jid)
    _prescore_memo.pop(jid, None)

    # A score() call that fell back to local models tags its reasoning — surface
    # that in the cycle stats so "who scored what" stays visible in the logs.
    from app.matching.reranker import LOCAL_REASON_PREFIX
    if reason.startswith(LOCAL_REASON_PREFIX):
        provider = "local"

    # Phase 3 — short session: idempotent write-back + hire-probability blend.
    def _hp(job, session):
        hp = score_hire_probability(job, session)
        job.hire_probability_score = hp.score
        job.hire_probability_signals = json.dumps(hp.signals)
        job.blended_score = compute_blended(score, hp.score)

    reasoning = reason + (("\nConcerns: " + "; ".join(concerns)) if concerns else "")
    extras = (json.dumps(breakdown) if breakdown else None, _hp)
    if not _stamp_job(jid, ghost, score, reasoning, extras,
                      prescore=(float(pre[0]) if pre is not None else None)):
        return None  # another lane scored it while we were on the LLM

    # Distillation shadow mode: run the local model beside this fresh LLM final
    # and record agreement (best-effort, ~50ms CPU, zero user-facing effect).
    # ONLY for real LLM finals — when the final itself came from the local
    # fallback, "shadowing" would compare the model against itself (the fake
    # MAE=0.0/100% telemetry of the Jul 2026 credits outage) and pay a second
    # inference for nothing.
    if provider != "local":
        try:
            from app.matching.local_scorer import shadow_score
            shadow_score(jid, ctx.resume, job, float(score))
        except Exception:
            pass
        # CardRace v2 shadow (docs/CARDRACE_DESIGN.md §5 Phase 3): score the
        # deterministic card engine beside this real final and record agreement.
        # Best-effort and flag-gated; never touches the authoritative score.
        try:
            from app.matching.card_shadow import shadow_card_match
            shadow_card_match(jid, ctx.resume, float(score), breakdown)
        except Exception:
            pass
    return ("scored", jid, float(score), provider)


def _plan_finals_cap(uid: Optional[str]) -> Optional[int]:
    """This user's plan allowance of Tier-2 finals per UTC day, or None if the
    plan is unknown (fail OPEN — a billing hiccup must never stall scoring).

    Imported lazily: server.py imports this module, so a top-level import would
    be circular. By the time a lane cycle runs, server is fully loaded."""
    if not uid or uid == "local":
        return None
    try:
        from app.api.server import _get_user_plan
        from app.db.models import PLAN_LIMITS
        return PLAN_LIMITS[_get_user_plan(uid)].get("finals_daily")
    except Exception as e:
        log.debug("plan lookup failed for %s (%s) — no per-user cap this cycle", uid, e)
        return None


def _remaining_finals_today(uid: Optional[str], per_cycle_cap: int) -> int:
    """How many jobs of ``uid``'s queue this cycle may take.

    Bounded by BOTH the per-cycle fairness cap and what is left of the user's
    plan allowance for the day. Returning 0 drops the user from this cycle's
    work list entirely, so their queue items are never even prescored — the
    cheap Tier-1 pass is not free either."""
    cap = _plan_finals_cap(uid)
    if cap is None or cap <= 0:
        return per_cycle_cap
    from app.matching.reranker import user_finals_today
    return max(0, min(per_cycle_cap, cap - user_finals_today(uid)))


def _shortlist_user(uid, scored: List[Tuple[int, float]], stats: dict) -> None:
    """Serial, cap-safe: shortlist a user's freshly-scored fits + fire alerts."""
    from app.matching.pipeline import _AUTOFILL_SOURCES, _check_and_enforce_company_cap
    from app.strategy.fresh_alerts import dispatch_fresh_alerts
    uid_arg = None if (not uid or uid == "local") else uid

    with get_session() as session:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        q = select(Application).where(Application.created_at >= today_start)
        q = q.where(Application.user_id == uid_arg) if uid_arg \
            else q.where(Application.user_id.is_(None))
        today_count = len(session.exec(q).all())

    # Which of these scores came from the local fallback (no AI review). Those
    # are held to the degraded bar and flagged provisional — see strategy/degraded.py.
    from app.matching.reranker import LOCAL_REASON_PREFIX
    from app.strategy.degraded import shortlist_threshold
    local_jids: set = set()
    with get_session() as session:
        for jid, _s in scored:
            j = session.get(Job, jid)
            if j and (j.rerank_reasoning or "").startswith(LOCAL_REASON_PREFIX):
                local_jids.add(jid)

    shortlisted: List[int] = []
    for jid, score in sorted(scored, key=lambda x: -x[1]):  # best first
        is_local = jid in local_jids
        if score < shortlist_threshold(is_local):
            continue
        if today_count >= settings.daily_shortlist_limit:
            break
        with get_session() as session:
            job = session.get(Job, jid)
            if not job:
                continue
            if session.exec(select(Application).where(Application.job_id == jid)).first():
                continue
            if not _check_and_enforce_company_cap(session, job, score):
                session.commit()
                continue
            track = "autofill" if job.source in _AUTOFILL_SOURCES else "manual"
            session.add(Application(
                job_id=jid, status=ApplicationStatus.SHORTLISTED,
                apply_url=job.url, apply_track=track, user_id=uid_arg,
                provisional=is_local,
            ))
            session.commit()
            shortlisted.append(jid)
            today_count += 1
    stats["shortlisted"] += len(shortlisted)
    if shortlisted:
        try:
            stats["alerts"] += dispatch_fresh_alerts(uid, shortlisted)
        except Exception as e:
            log.warning("scoring lane alerts failed for %s: %s", uid, e)


def run_scoring_lane(deadline: Optional[float] = None) -> dict:
    """One scoring cycle: drain the global unscored queue in parallel, then
    shortlist + alert. Returns cycle stats. Skips if a cycle is already running."""
    if not _LANE_LOCK.acquire(blocking=False):
        return {"skipped": "cycle already running"}
    try:
        return _run_scoring_cycle(deadline)
    finally:
        _LANE_LOCK.release()


def _expire_stale_unscored(batch: int = 2000, max_batches: int = 50) -> int:
    """Bulk-stamp unscored per-user jobs older than scoring_max_job_age_days so
    they exit the queue WITHOUT costing a prescore or final.

    Rationale: 'be first to apply' is the product — a posting that sat unscored
    for days (credit outage, backlog) is going stale, and paying LLM calls
    to rank it wastes budget and disk IO exactly when the lane is trying to
    catch up. One indexed UPDATE per cycle (0 rows in steady state) replaces
    thousands of per-job LLM calls during a backlog drain. Shared-pool rows are
    excluded (never scored directly); 0 disables."""
    days = int(getattr(settings, "scoring_max_job_age_days", 0) or 0)
    if days <= 0:
        return 0
    from datetime import timedelta

    from sqlalchemy import update

    from app.discovery.pipeline import SHARED_POOL_USER
    cutoff = datetime.utcnow() - timedelta(days=days)
    # Age is coalesce(posted_at, first_seen, discovered_at) — the SAME measure the
    # render filter, hygiene lane and pulse fast path use. Keying off first_seen
    # alone let an aggregator posting that is 30 days old but adopted today sail
    # through the gate and buy a prescore + a Claude final, only to be hidden at
    # render — exactly the spend this gate exists to prevent. discovered_at is the
    # final fallback because first_seen reached prod via a bare ALTER TABLE with no
    # backfill, so it is NULL on the oldest rows and `NULL < cutoff` is NULL.
    from sqlalchemy import func as _func
    age = _func.coalesce(Job.posted_at, Job.first_seen, Job.discovered_at)
    total = 0
    try:
        # Batched like close_stale_user_jobs: the first run after deploy can match
        # six figures of rows on a 764k-row table, and one unbounded UPDATE is the
        # Supabase statement-timeout / Disk-IO pattern we just spent a day fixing.
        for _ in range(max_batches):
            with get_session() as session:
                ids = [r[0] if isinstance(r, tuple) else r for r in session.exec(
                    select(Job.id).where(
                        Job.rerank_score == None,   # noqa: E711
                        Job.is_closed == False,     # noqa: E712
                        (Job.user_id.is_(None)) | (Job.user_id != SHARED_POOL_USER),
                        age < cutoff,
                    ).limit(batch)
                ).all()]
                if not ids:
                    break
                session.exec(
                    update(Job)
                    .where(Job.id.in_(ids))
                    .values(rerank_score=8.0,
                            rerank_reasoning=f"Expired unscored (older than {days}d "
                                             f"before scoring caught up — too stale to apply)")
                )
                session.commit()
                total += len(ids)
            if len(ids) < batch:
                break
        if total:
            log.info("Scoring: expired %d stale unscored job(s) older than %dd "
                     "(drained free, no LLM spend)", total, days)
        return total
    except Exception as e:
        # WARNING not DEBUG: this runs every 90s, so a silent failure means a
        # repeating unlogged IO burn.
        log.warning("stale-unscored expiry failed (non-fatal): %s", e)
        return total


def _run_scoring_cycle(deadline: Optional[float]) -> dict:
    from app.matching.pipeline import _load_resume
    from app.matching.reranker import (
        Reranker, any_provider_available, llm_budget_exhausted,
    )
    stats = {"users": 0, "queued": 0, "scored": 0, "drained": 0,
             "shortlisted": 0, "alerts": 0, "by_claude": 0, "by_gpt": 0,
             "by_local": 0}

    # Age gate first: never spend LLM budget (or backlog IO) on postings too
    # old to be worth applying to.
    _expire_stale_unscored()

    # Fast-exit guards: when every provider is cooling down (credit/quota) or
    # the daily spend cap is hit, a cycle would only burn CPU and log noise —
    # jobs stay Queued and the next eligible cycle picks them up. With the
    # local-score fallback enabled, providers being down is NOT a stall: the
    # cycle proceeds and Reranker.score() stamps free local estimates instead.
    if not any_provider_available() and not settings.local_score_fallback:
        return {**stats, "skipped": "all LLM providers cooling down"}
    if llm_budget_exhausted():
        return {**stats, "skipped": "LLM budget reached (hourly/daily cap)"}

    users = _scorable_user_ids()
    if not users:
        return stats

    # Build the global work list, freshest-first per user, ROUND-ROBIN across
    # users. Interleaving (vs. the old user-by-user fill) does two things:
    # 1. fairness — when the global cap bites, every user gets a share of the
    #    cycle instead of the first users taking all 600 slots;
    # 2. cache efficiency — same-user finals share a cached résumé prefix, and a
    #    cache entry is only readable after the first call finishes. Interleaved
    #    items keep the 20 workers on DIFFERENT users' prefixes, so job #2 for a
    #    user usually finds the cache its job #1 just wrote.
    # Every user's queue is fetched (tiny indexed id-only selects) BEFORE the
    # cap is applied — stopping the fetch at the cap would hand all slots to
    # whichever users happened to come first, which is the unfairness this
    # rewrite removes.
    # Empty-board (new/just-onboarded) users first: they're staring at a blank
    # dashboard, so their queue items must survive the global cap and reach the
    # workers earliest. One grouped query identifies owners who already have at
    # least one scored open job; everyone else is "new" for ordering purposes.
    # This is an EXISTS question per user, so ask it that way. The obvious
    # spelling — SELECT DISTINCT user_id ... WHERE rerank_score IS NOT NULL AND
    # user_id IN (...) — has to visit every scored open row across every user and
    # then dedupe, and no index covers that predicate. Production measured it at
    # 31-38s per execution, 277 times in a day, on a lane that ticks every 90s:
    # roughly 40% of the lane's wall clock spent on an ordering nicety, with
    # per-id UPDATEs queueing behind it at 15-37s each.
    #
    # One LIMIT 1 probe per user rides ix_job_user_open (user_id, is_closed) and
    # stops at the first hit, so each is sub-millisecond and the total scales with
    # the number of ACTIVE users rather than the size of the job table.
    try:
        has_scored = set()
        with get_session() as session:
            for _u in users:
                if not _u:
                    continue
                hit = session.exec(
                    select(Job.id).where(
                        Job.user_id == _u,
                        Job.is_closed == False,  # noqa: E712
                        Job.rerank_score.is_not(None),
                    ).limit(1)
                ).first()
                if hit is not None:
                    has_scored.add(_u)
        users = sorted(users, key=lambda u: (u in has_scored,))
    except Exception as e:
        log.debug("new-user priority ordering skipped: %s", e)

    queues: List[List[Tuple[Optional[str], int]]] = []
    capped_out = 0
    for uid in users:
        # Per-plan daily allowance, not a slice of one global pool — see
        # PLAN_LIMITS["finals_daily"]. A user who has spent today's allowance
        # contributes nothing to this cycle's work list.
        allowance = _remaining_finals_today(uid, settings.scoring_per_user_cap)
        if allowance <= 0:
            capped_out += 1
            continue
        q = [(uid, jid) for jid in _user_queue(uid, allowance)]
        if q:
            queues.append(q)
    if capped_out:
        stats["plan_capped_users"] = capped_out
    items: List[Tuple[Optional[str], int]] = []
    depth = 0
    while len(items) < settings.scoring_global_cap and any(depth < len(q) for q in queues):
        for q in queues:
            if depth < len(q):
                items.append(q[depth])
                if len(items) >= settings.scoring_global_cap:
                    break
        depth += 1
    stats["users"] = len({u for u, _ in items})
    stats["queued"] = len(items)
    if not items:
        return stats

    # Per-user context (résumé + reranker), loaded ONCE and shared across workers.
    ctx_cache: dict = {}
    ctx_lock = threading.Lock()

    def _ctx_for(uid) -> Optional[_Ctx]:
        # The whole build runs under the lock, deliberately. Two things depend
        # on it being single-flight: (1) the cache prewarm below must happen
        # exactly once and must COMPLETE before any worker for this user starts
        # a real call, or the workers race the write and all miss it; (2) the
        # old check-then-build shape let two workers both miss the cache and
        # each build a résumé load + Reranker for the same user. This runs once
        # per user per cycle, so serializing it costs nothing measurable.
        with ctx_lock:
            if uid in ctx_cache:
                return ctx_cache[uid]
            uid_arg = None if (not uid or uid == "local") else uid
            try:
                resume = _load_resume(user_id=uid_arg)
            except Exception as e:
                log.debug("scoring: no résumé for %s (%s) — skipping", uid, e)
                ctx_cache[uid] = None
                return None
            profile = None
            try:
                from app.autofill.answer_pack import _get_or_create_profile
                profile = _get_or_create_profile(user_id=uid_arg)
            except Exception:
                pass
            reranker = Reranker(profile=profile)
            # Write the shared prefix once (prefill only, 0 output tokens) so the
            # jobs that follow read it at 0.1x instead of each paying the 1.25x
            # write. See Reranker.prewarm_cache. Purely an optimization, so it is
            # never allowed to fail the cycle — a scorer without the method (or a
            # provider hiccup) just means the first real call writes the cache,
            # exactly as before.
            try:
                reranker.prewarm_cache(resume)
            except Exception as e:
                log.debug("cache prewarm unavailable for %s (%s)", uid, e)
            ctx = _Ctx(resume, reranker,
                       settings.prescore_enabled and reranker.has_prescore_backend(),
                       min(settings.prescore_advance_threshold, settings.shortlist_score_threshold))
            ctx_cache[uid] = ctx
            return ctx

    def _work(item):
        uid, jid = item
        ctx = _ctx_for(uid)
        if ctx is None:
            return None
        res = _score_job(jid, ctx)
        return (uid, res) if res else None

    scored_by_user: dict = defaultdict(list)
    spend_by_user: dict = defaultdict(int)   # (uid, kind) -> calls this cycle
    pool = _worker_pool()
    futures = [pool.submit(_work, it) for it in items]
    for fut in futures:
        remaining = (deadline - time.monotonic()) if deadline else None
        if remaining is not None and remaining <= 0:
            break  # out of budget — the rest stay Queued for the next cycle
        try:
            out = fut.result(timeout=remaining if remaining else None)
        except _FutureTimeout:
            break
        except Exception:
            continue
        if not out:
            continue
        uid, (kind, jid, score, provider) = out
        if kind == "scored":
            stats["scored"] += 1
            scored_by_user[uid].append((jid, score))
            # `provider` is the REQUESTED provider, and _pick_provider returns
            # None whenever dual mode is off — which it is in production. So the
            # old `== "anthropic"` test never matched the normal path and
            # score_final was recorded ONCE in the table's entire history, while
            # 1,300+ finals actually ran: /api/admin/spend reported the single
            # most expensive call type as zero. None = default priority order =
            # the Tier-2 final; the local fallback sets provider="local"
            # explicitly above, so it cannot be miscounted here.
            if provider in (None, "anthropic"):
                stats["by_claude"] += 1
                spend_by_user[(uid, "score_final")] += 1
            elif provider == "openai":
                stats["by_gpt"] += 1
                spend_by_user[(uid, "score_prescore")] += 1
            elif provider == "local":
                stats["by_local"] += 1
                spend_by_user[(uid, "score_local")] += 1
        elif kind == "drained":
            stats["drained"] += 1
            spend_by_user[(uid, "score_prescore")] += 1
    # Cancel whatever is still QUEUED so a deadline-truncated cycle can't drain
    # its leftovers into the next one (the old per-cycle pool's shutdown(wait=
    # False) let up to ~200 queued LLM calls keep running while a fresh pool
    # spawned 90s later — overlapping thread generations). Already-running
    # calls finish on their own, bounded by llm_request_timeout; their jobs
    # stay Queued in the DB either way and are simply re-selected next cycle.
    for fut in futures:
        fut.cancel()

    # Per-user spend attribution (batched: one upsert per user+kind per cycle).
    try:
        from app.analytics.spend import record_llm_spend
        for (s_uid, s_kind), n in spend_by_user.items():
            record_llm_spend(s_uid, s_kind, n)
    except Exception:
        pass

    # Phase B — shortlist + alert, serial per user (cap-safe).
    for uid, results in scored_by_user.items():
        try:
            _shortlist_user(uid, results, stats)
        except Exception as e:
            log.warning("scoring lane shortlist failed for %s: %s", uid, e)

    # ── Degraded-mode bookkeeping ────────────────────────────────────────────
    # A cycle that produced ONLY local scores means no provider answered: the
    # board is being filled without AI review. Record the outage; when a real
    # final lands again, re-check exactly what was shortlisted during it.
    try:
        from app.strategy import degraded
        real_finals = stats["by_claude"] + stats["by_gpt"]
        if stats["scored"] > 0 and real_finals == 0 and stats["by_local"] > 0:
            degraded.note_degraded()
        elif real_finals > 0:
            window = degraded.note_healthy()
            if window:
                stats["recheck"] = degraded.recheck_provisional(window, users)
    except Exception as e:
        log.warning("degraded-mode bookkeeping failed: %s", e)

    try:
        with get_session() as session:
            session.add(FunnelEvent(
                job_id=None, stage="scoring_cycle", passed=True,
                reason=f"users={stats['users']} scored={stats['scored']}",
                metadata_json=json.dumps(stats),
            ))
            session.commit()
    except Exception as e:
        log.debug("scoring cycle event write failed: %s", e)
    log.info("Scoring cycle: %s", stats)
    return stats
