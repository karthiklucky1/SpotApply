"""Adoption — fill a user's pool from the SHARED job pool with a cheap DB copy.

The shared pool (see ``SHARED_POOL_USER`` in discovery/pipeline.py) holds every
posting any lane has fetched, once. Adoption copies the subset matching a
user's target roles + location preferences into their own pool — no HTTP, no
scraping, just database reads and the standard ``_upsert`` dedupe path. This is
what makes onboarding instant: a brand-new user (or a user who just edited
their roles) gets weeks of already-collected matching jobs in seconds, then the
regular matching pass scores them.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import load_only
from sqlmodel import select

from app.common.freshness import is_fresh
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Job

log = logging.getLogger(__name__)

# How far back to adopt by default: old enough to fill a board, young enough
# that postings are still worth applying to.
ADOPT_MAX_AGE_DAYS = 21
# Cap per adoption pass — a full matching pass only LLM-scores a slice per run
# anyway, and the next cycles keep draining.
ADOPT_MAX_JOBS = 400
#: One read of the shared pool. Kept at the size the egress work settled on —
#: a projected page, not a 5000-full-row scan.
ADOPT_PAGE_SIZE = 3000
#: How far past the newest page adoption will look for postings this user does
#: not already have. Bounded because each page is another read; in steady state
#: the first page always contains new postings and the loop stops there. It only
#: pages deeper for a user whose pool already holds everything at the top, which
#: is exactly the case that used to adopt nothing at all.
ADOPT_MAX_PAGES = 4


def _semantic_query_vector(matcher, user_id, roles):
    """A single normalized embedding representing what this user wants — their
    target roles plus their résumé (top ~3k chars). None if there's nothing to
    embed. Reuses the shared local MiniLM model, so there's no API cost."""
    parts = []
    if roles:
        parts.append(", ".join(roles))
    try:
        from app.matching.pipeline import _load_resume
        uid_arg = None if (not user_id or user_id == "local") else user_id
        resume = (_load_resume(user_id=uid_arg) or "").strip()
        if resume:
            parts.append(resume)
    except Exception as e:
        log.debug("adoption semantic: résumé unavailable (%s)", e)
    text = "\n\n".join(p for p in parts if p).strip()
    if not text:
        return None
    return matcher.encode([text[:3000]])[0]


def _semantic_extras(others, roles, user_id, need):
    """The closest résumé-neighbours among jobs whose TITLE didn't match a role —
    so "same work, different title" postings are adopted and scored instead of
    dropped. Returns at most ``need`` jobs, sorted by cosine, all ≥ the threshold.
    Any embedding failure raises (caller falls back to title-only)."""
    if need <= 0 or not others:
        return []
    from app.matching.matcher import Matcher
    matcher = Matcher(user_id=None)  # encode-only; never persists an index
    query = _semantic_query_vector(matcher, user_id, roles)
    if query is None:
        return []
    pool = others[: max(0, settings.adoption_semantic_max_candidates)]
    embs = matcher.encode([Matcher._job_text(j) for j in pool])
    sims = embs @ query  # both L2-normalized → dot product == cosine
    threshold = settings.adoption_semantic_threshold
    ranked = sorted(
        ((j, float(s)) for j, s in zip(pool, sims) if float(s) >= threshold),
        key=lambda x: -x[1],
    )
    picked = [j for j, _ in ranked[:need]]
    if picked:
        log.info("Adoption semantic: +%d neighbour(s) (cosine ≥ %.2f) from %d off-title jobs",
                 len(picked), threshold, len(pool))
    return picked


def _source_key(source) -> str:
    """`Job.source` is an enum column; per-user copies preserve its value."""
    return source.value if hasattr(source, "value") else str(source)


def _drop_already_adopted(jobs, user_id):
    """Remove postings this user's pool already holds.

    This has to happen BEFORE the cap, not inside `_upsert` afterwards.
    `_upsert` deduplicates on `(user_id, source, external_id)`, so duplicates
    were silently discarded — but they had already consumed the cap on the way
    in, which is how a user could adopt nothing at all while eligible postings
    sat unadopted behind them.

    Asks only about the keys in hand (chunked `IN`), so the cost tracks the
    page size rather than the size of the user's pool — which reached ~115k
    rows for one production user.
    """
    from app.db.models import Job as _Job

    if not jobs:
        return []
    keys = [( _source_key(j.source), j.external_id) for j in jobs]
    have: set = set()
    try:
        with get_session() as session:
            scope = (_Job.user_id == user_id) if user_id else _Job.user_id.is_(None)
            for start in range(0, len(keys), 300):
                chunk = keys[start:start + 300]
                rows = session.exec(
                    select(_Job.source, _Job.external_id).where(
                        scope,
                        _Job.external_id.in_([k[1] for k in chunk]))
                ).all()
                for src, ext in rows:
                    have.add((_source_key(src), ext))
    except Exception as e:
        # Fail OPEN: adopting a duplicate is free (`_upsert` drops it), while
        # skipping this filter entirely is the old behaviour, not a new fault.
        log.debug("adoption: duplicate pre-filter unavailable (%s)", e)
        return jobs
    return [j for j, k in zip(jobs, keys) if k not in have]


def _select_adoptable(fresh_jobs, roles, user_id, limit):
    """Which fresh shared-pool jobs to copy into the user's pool: every title-role
    match, then — to catch differently-titled-but-relevant postings — the closest
    résumé-neighbours to fill remaining slots. Bounded by ``limit``. Falls back to
    title-only when semantic adoption is off or embeddings are unavailable."""
    from app.discovery.title_filter import role_title_match
    title_hits, others = [], []
    for j in fresh_jobs:
        (title_hits if role_title_match(j.title, roles) else others).append(j)

    if settings.adoption_semantic_enabled and others and len(title_hits) < limit:
        # Every extra is a new unscored row the LLM lanes must pay to score, so
        # the semantic pass gets a hard per-pass budget of its own instead of
        # filling every slot the title pass left open.
        need = min(limit - len(title_hits),
                   max(0, settings.adoption_semantic_max_extras))
        if need > 0:
            try:
                extras = _semantic_extras(others, roles, user_id, need)
                return (title_hits + extras)[:limit]
            except Exception as e:
                log.warning("Adoption semantic pass failed (%s) — title-only", e)
    return title_hits[:limit]


def adopt_shared_jobs(user_id: str | None, max_age_days: int = ADOPT_MAX_AGE_DAYS,
                      limit: int = ADOPT_MAX_JOBS, since: datetime | None = None) -> int:
    """Copy recent, role-matching shared-pool postings into ``user_id``'s pool.

    Reuses ``_upsert`` so per-user dedupe (source+external_id and cross-source
    slug), the country gate, and direct-ATS upgrades behave exactly as if the
    jobs had been scraped for this user. Returns the number of NEW rows.
    ``since`` narrows the shared page to postings first seen at or after it
    (the incremental step below); None walks the whole ``max_age_days`` window."""
    return _adopt(user_id, max_age_days, limit, since)[0]


# ── Incremental adoption: the 5-minute step between the 6-hour passes ────────
# Adoption used to run only on the global discovery scheduler (~every 6 h) and
# on résumé/role edits. The pulse lane routes a NEW posting to a user in the
# same tick it lands (measured 4.4 s), but only when the posting arrives through
# a changed pulse board AND the user's roles title-match it at that moment.
# Everything else — feed and aggregator finds, a board that changed while the
# user's roles did not match, a user whose roles changed since — waited for the
# next global pass: measured adoption p95 was ~110 minutes for a DB copy that
# takes seconds. This step runs every matching-lane tick and asks only "what
# did the shared pool gain since I last looked?", so the query is one range
# walk on ``ix_job_unscored (user_id, first_seen)`` (shared rows are never
# scored, so the partial index covers them).
#
# WATERMARKS, done so nothing is lost: the watermark advances ONLY when the
# pass did not hit its cap — a capped pass re-reads the same window next tick
# and ``_drop_already_adopted`` makes the replay free. Every window overlaps
# the previous one by the lane interval (a posting whose first_seen landed
# between "select" and "commit" of the last pass is still inside it), and the
# 6-hour full pass remains the reconciliation for anything a bounded step
# could not reach. Process-local: a restart re-walks the full window once.
_ADOPT_WATERMARK: dict[str, datetime] = {}
ADOPT_INCREMENTAL_LIMIT = 200


def adopt_incremental(user_id: str | None, *, interval_seconds: int = 300,
                      limit: int = ADOPT_INCREMENTAL_LIMIT) -> int:
    """One bounded adoption step for ``user_id``: shared postings first seen
    since the last COMPLETE step (with overlap). Returns new rows adopted."""
    key = user_id or "local"
    started = datetime.utcnow()
    mark = _ADOPT_WATERMARK.get(key)
    overlap = timedelta(seconds=max(60, int(interval_seconds or 0)))
    since = (mark - overlap) if mark is not None else started - timedelta(days=ADOPT_MAX_AGE_DAYS)
    adopted, hit_cap = _adopt(user_id, ADOPT_MAX_AGE_DAYS, limit, since)
    if not hit_cap:
        _ADOPT_WATERMARK[key] = started
    elif mark is None:
        # A first pass that hit the cap has at least covered the newest slice;
        # keep the watermark unset so the next pass walks the window again.
        pass
    return adopted


def _adopt(user_id: str | None, max_age_days: int, limit: int,
           since: datetime | None) -> tuple[int, bool]:
    """The adoption pass. Returns (new_rows, hit_cap): ``hit_cap`` is True when
    the pass selected as many candidates as it was allowed to, i.e. there may
    be eligible postings it did not reach."""
    from app.api.server import _get_target_roles
    from app.discovery.base import RawJob
    from app.discovery.pipeline import SHARED_POOL_USER, _upsert

    # Location preferences — resolved through the ONE helper the scoring prompt
    # also reads (app/common/tenant_prefs). A blank profile country used to mean
    # "no country gate here", while the prompt still told Claude the candidate
    # wants United States and scored everything else 0-30 — so we admitted
    # foreign postings for free and paid the LLM to reject them.
    from app.common.tenant_prefs import effective_country, effective_remote_ok, geo_prefs
    country, remote_ok = effective_country(None, user_id), True
    prefs = geo_prefs(None, user_id)
    p = None
    try:
        from app.autofill.answer_pack import _get_or_create_profile
        p = _get_or_create_profile(user_id=user_id)
        if p:
            country = effective_country(p, user_id)
            remote_ok = effective_remote_ok(p)
            prefs = geo_prefs(p, user_id)
    except Exception as e:
        log.debug("adoption: profile unavailable (default %s): %s", country, e)

    roles = [r.lower() for r in (_get_target_roles(user_id or "local") or [])]
    if not roles:
        # No saved roles → do NOT adopt the whole shared firehose. role_title_match
        # accepts EVERY title when roles is empty, so a role-less user copies the
        # entire shared pool into their own (seen in prod: one user's pool grew to
        # ~115k jobs, making their FAISS rebuild take ~9 min and starving the board
        # fetch of CPU). Fall back to roles derived from the profile so the feed
        # stays focused and the pool stays small; the user can still edit them.
        try:
            from app.api.server import _suggest_roles
            roles = [r.lower() for r in _suggest_roles(p)]
            log.info("Adoption: user %s had no target roles — using derived roles %s",
                     user_id or "local", roles)
        except Exception as _re:
            log.debug("adoption role fallback failed: %s", _re)

    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    # The incremental step narrows the walk to what the pool gained since the
    # last complete step; never wider than the age window either way.
    lower = max(cutoff, since) if since is not None else None

    def _page(offset: int):
        with get_session() as session:
            return _shared_page(session, cutoff, offset)

    def _shared_page(session, cutoff, offset: int):
        q = (
            select(Job)
            # Only load the columns adoption uses (RawJob fields + the freshness
            # timestamps) — the big JSON blobs (rerank_*/hire_probability_signals/
            # corporate_insights) are dead weight here. Combined with the SQL
            # freshness cutoff and a tighter cap, this replaces a 5000-full-row
            # (~16 MB) scan-to-keep-400 with a much smaller read.
            # discovered_at is loaded because _fresh_enough reads it (the known
            # reference is coalesce(first_seen, discovered_at)). These rows are
            # used AFTER the session closes, so any column the freshness rule
            # touches must be loaded here — a deferred attribute on a detached
            # instance raises DetachedInstanceError rather than returning None,
            # which would take out the whole adoption pass.
            .options(load_only(
                Job.source, Job.external_id, Job.company, Job.title, Job.location,
                Job.remote, Job.url, Job.description, Job.posted_at, Job.first_seen,
                Job.discovered_at,
                # ≤120 chars: the ATS's own pay statement (RawJob.salary_text),
                # carried into the copy below so the regex fallback in
                # _build_job does not replace it. Loaded here because these
                # rows are read after the session closes (see above).
                Job.salary_text,
            ))
            .where(Job.user_id == SHARED_POOL_USER,
                   Job.is_closed == False,  # noqa: E712
                   Job.first_seen != None,  # noqa: E711
                   # Freshness prefilter in SQL so stale rows aren't streamed
                   # over just to be dropped in Python. Deliberately a SUPERSET
                   # of _fresh_enough (which applies the real two-bound rule):
                   # anything that rule keeps has known_age <= max_age_days and
                   # therefore satisfies the `first_seen >= cutoff` arm here.
                   (Job.posted_at >= cutoff) | (Job.first_seen >= cutoff))
        )
        if lower is not None:
            # Range on the indexed column: the incremental step's whole cost.
            q = q.where(Job.first_seen >= lower)
        return session.exec(
            q.order_by(Job.first_seen.desc())
            .limit(ADOPT_PAGE_SIZE)
            .offset(offset)
        ).all()

    def _fresh_enough(j: Job) -> bool:
        """The SAME two-bound rule the rest of the funnel uses
        (app/common/freshness.py).

        This was the last place where ``posted_at`` still won a coalesce, and it
        is the earliest gate on the forward path — so it silently narrowed the
        effective posted bound from ``scoring_max_posted_age_days`` (30d) to
        ``ADOPT_MAX_AGE_DAYS`` (21d). A posting discovered TODAY that an ATS
        dated 25 days back was dropped here and never reached the user's pool at
        all, so neither the fixed scoring gate nor the fixed render window ever
        got a chance to keep it.

        Worse, it made behaviour depend on WHICH LANE found the job: the pulse
        lane's per-user ``_upsert`` applies no age filter, so the identical
        posting reached a user when the pulse lane routed it and was discarded
        when adoption did.

        Known age is bounded by ``max_age_days`` (don't copy shared rows we have
        sat on for weeks); the source's own date is bounded far more loosely, and
        only to suppress evergreen/ancient listings. The SQL prefilter above is a
        superset of this rule — anything this keeps has ``known_age <=
        max_age_days``, which satisfies the query's ``first_seen >= cutoff`` arm.
        """
        return is_fresh(j, max_age_days,
                        int(getattr(settings, "scoring_max_posted_age_days", 0) or 0))

    # Walk the shared pool until we have enough postings this user does NOT
    # already have, rather than capping the newest page and then discovering
    # every one of them is a duplicate.
    #
    # The old shape took the newest ADOPT_PAGE_SIZE rows, cut them to `limit`,
    # and only deduplicated inside `_upsert` — so the second pass re-selected
    # the same already-copied jobs and inserted nothing, while eligible
    # postings ranked below the cap were never adopted at all. Three eligible
    # jobs against a two-job cap adopted 2, then 0, then 0: the third never
    # reached scoring, ever.
    from app.discovery.title_filter import role_title_match

    pool: list = []
    seen: set = set()
    for page in range(ADOPT_MAX_PAGES):
        shared = _page(page * ADOPT_PAGE_SIZE)
        if not shared:
            break
        for j in shared:
            key = (_source_key(j.source), j.external_id)
            if key in seen:
                continue
            seen.add(key)
            if _fresh_enough(j):
                pool.append(j)
        pool = _drop_already_adopted(pool, user_id)
        # The quota that matters is ROLE-MATCHING new jobs — those are what
        # `_select_adoptable` will actually take. Counting raw rows would stop
        # early on a page full of off-role postings.
        if sum(1 for j in pool if role_title_match(j.title, roles)) >= limit:
            break
        if len(shared) < ADOPT_PAGE_SIZE:
            break                      # the pool is exhausted, not the budget

    candidates = _select_adoptable(pool, roles, user_id, limit)
    hit_cap = len(candidates) >= max(1, int(limit))
    if not candidates:
        return 0, False

    raw = [RawJob(
        source=j.source.value if hasattr(j.source, "value") else str(j.source),
        external_id=j.external_id,
        company=j.company,
        title=j.title,
        location=j.location,
        remote=bool(j.remote),
        url=j.url,
        description=j.description or "",
        posted_at=j.posted_at,
        # Carry the SHARED row's first sighting into the user's copy. Without
        # this the copy was stamped first_seen=now, so a posting we had held for
        # three weeks entered the board as "found today": it re-entered the
        # 5-day scoring window, re-entered the render window, and the freshness
        # promise measured the age of a DB copy instead of the age of the job.
        first_seen=j.first_seen or j.discovered_at,
        # Same principle for pay: the shared row may hold the ATS's own salary
        # summary (Ashby). A copy that dropped it would be re-stamped from the
        # description regex, which for those postings found nothing.
        salary_text=j.salary_text,
    ) for j in candidates]

    inserted = _upsert(raw, user_id=user_id, preferred_country=country,
                       remote_ok=remote_ok, user_keywords=roles or None,
                       geo_prefs=prefs)
    log.info("Adoption: %d shared candidates → %d new jobs for user %s%s",
             len(candidates), inserted, user_id or "local",
             " (cap hit — more may be waiting)" if hit_cap else "")
    return inserted, hit_cap


def repair_copied_first_seen(limit: int = 2000, min_gap_hours: float = 1.0,
                             statement_timeout_seconds: int = 30) -> int:
    """Re-date per-user copies that are YOUNGER than the shared posting they
    were copied from. Returns rows updated. Bounded, daily, never raises.

    A copy must never make a posting younger (adoption has carried the shared
    ``first_seen`` since 09-12), but the pulse lane's per-user route handed the
    scraper's RawJob — no ``first_seen`` — straight to ``_upsert``, so a posting
    the pool had held for 56 days entered a user's board as found today: back
    inside the 5-day scoring window and the render window, against a promise
    to be first to apply. The audit counted 35 such copies. New copies now
    inherit the shared sighting at write time (``pipeline._upsert``); this is
    the one-off repair for the rows written before that, kept as a daily
    sweep because a stamp bug of this class has happened twice.

    Only OPEN copies are touched (a closed row is history), only where the gap
    exceeds ``min_gap_hours`` (clock skew between two lanes stamping the same
    posting seconds apart is not a defect), and applications are never
    modified — re-dating a shortlisted, unviewed copy older simply lets
    shortlist hygiene apply the same window it applies to everything else.
    """
    from sqlalchemy.orm import aliased

    from app.discovery.pipeline import SHARED_POOL_USER
    from app.strategy.scoring_lane import _arm_statement_timeout

    shared = aliased(Job)
    gap = timedelta(hours=max(0.0, float(min_gap_hours)))
    try:
        with get_session() as session:
            _arm_statement_timeout(session, statement_timeout_seconds)
            pairs = session.exec(
                select(Job.id, Job.first_seen, shared.first_seen)
                .join(shared, (shared.source == Job.source)
                      & (shared.external_id == Job.external_id)
                      & (shared.user_id == SHARED_POOL_USER))
                .where(Job.user_id.is_not(None),
                       Job.user_id != SHARED_POOL_USER,
                       Job.is_closed == False,  # noqa: E712
                       Job.first_seen.is_not(None),
                       shared.first_seen.is_not(None),
                       Job.first_seen > shared.first_seen)
                .limit(max(1, int(limit)))
            ).all()
            fixes = [{"id": jid, "first_seen": s_fs} for jid, u_fs, s_fs in pairs
                     if u_fs is not None and s_fs is not None and (u_fs - s_fs) > gap]
            if not fixes:
                return 0
            # By primary key, one bulk statement — the same shape as the
            # prescore stampers in matching/pipeline.py. (An ORM `update()`
            # with a bound WHERE refuses executemany outright.)
            session.bulk_update_mappings(Job, fixes)
            session.commit()
            log.info("Adoption repair: re-dated %d per-user copies to the shared first sighting",
                     len(fixes))
            return len(fixes)
    except Exception as e:
        log.warning("Adoption repair skipped (non-fatal): %s", e)
        return 0


def adopt_and_match(user_id: str | None) -> int:
    """Adoption + a matching pass — the 'instant feed' used after resume upload
    and role edits. Matching waits politely on the discovery lock; adoption
    itself never needs it (pure DB copy)."""
    adopted = adopt_shared_jobs(user_id)
    matched = False
    try:
        from app.common.discovery_lock import discovery_guard
        from app.matching.pipeline import run_matching
        with discovery_guard(label="instant feed") as ran:
            if ran:
                run_matching(user_id)
                matched = True
    except Exception as e:
        log.warning("instant-feed matching failed for %s: %s", user_id, e)
    if not matched:
        # The discovery lock was busy (a global pass in flight) — previously we
        # silently skipped here and the new user stared at an empty board until
        # a scheduled lane got to them. The scoring lane is lock-free and
        # prioritizes empty-board users, so kick a cycle right now instead.
        _score_first_slice(user_id)
    return adopted


def _score_first_slice(user_id: str | None, attempts: int = 3) -> None:
    """Drive scoring-lane cycles until this user has their first scored jobs.

    Lock-free path for onboarding: run_scoring_lane() skips when a cycle is
    already in flight (whose work list won't include jobs adopted after it
    started), so retry a couple of times with short waits. Bounded and
    best-effort — the scheduled lane remains the backstop."""
    import time as _time
    from app.strategy.scoring_lane import run_scoring_lane
    uid_arg = None if (not user_id or user_id == "local") else user_id
    for attempt in range(attempts):
        try:
            with get_session() as session:
                cond = (Job.user_id == uid_arg) if uid_arg else Job.user_id.is_(None)
                scored = int(session.exec(
                    select(func.count(Job.id)).where(
                        cond, Job.is_closed == False,  # noqa: E712
                        Job.rerank_score.is_not(None),
                    )
                ).first() or 0)
            if scored > 0:
                return  # first matches are on the board — mission accomplished
            result = run_scoring_lane()
            if not result.get("skipped"):
                continue  # a cycle ran (we were in its work list) — recheck
        except Exception as e:
            log.debug("onboarding first-slice scoring attempt failed for %s: %s",
                      user_id or "local", e)
        if attempt < attempts - 1:
            _time.sleep(20)


def _user_pool_count(user_id: str | None) -> int:
    """How many open jobs sit in this user's own pool. The pool is role-gated at
    insert (adoption + discovery both apply the user's role terms), so this is
    effectively an on-role count."""
    uid_arg = None if (not user_id or user_id == "local") else user_id
    with get_session() as session:
        cond = (Job.user_id == uid_arg) if uid_arg else Job.user_id.is_(None)
        return int(session.exec(
            select(func.count(Job.id)).where(cond, Job.is_closed == False)  # noqa: E712
        ).first() or 0)


def seed_new_user(user_id: str | None) -> int:
    """Onboarding entry point (résumé upload + first role edit).

    Step 1 — instant feed: copy matching jobs already in the shared pool into the
    user's board and score them (``adopt_and_match``), so they see results within
    seconds. Step 2 — domain scrape: the shared pool is dominated by the roles
    existing users search (historically AI/ML), so a user from another field
    (mechanical, finance, nursing…) adopts almost nothing. When the instant feed
    leaves them under ``onboarding_min_jobs`` on-role jobs, actively scrape THEIR
    roles right away — the same path as the manual Discover button — instead of
    making them wait for the next 6h global pass. Returns the adopted count."""
    adopted = adopt_and_match(user_id)

    if not settings.onboarding_active_discovery or settings.onboarding_min_jobs <= 0:
        return adopted
    try:
        on_role = _user_pool_count(user_id)
    except Exception as e:
        log.debug("onboarding: pool count failed for %s: %s", user_id or "local", e)
        on_role = adopted
    if on_role >= settings.onboarding_min_jobs:
        return adopted  # shared pool already covers this user's domain — no scrape

    # Thin feed → the shared pool doesn't cover this user's field yet. Scrape it.
    try:
        from app.api.server import (
            _discover_then_match, _get_target_roles, _user_has_resume,
        )
        uid_check = user_id or "local"
        if not _get_target_roles(uid_check):
            return adopted  # no roles → nothing to search for
        if not _user_has_resume(uid_check):
            return adopted  # no résumé → matching would only surface noise
        log.info("Onboarding: user %s has only %d on-role jobs after adoption "
                 "(< %d) — actively discovering their domain",
                 user_id or "local", on_role, settings.onboarding_min_jobs)
        _discover_then_match(user_id)
    except Exception as e:
        log.warning("onboarding active discovery failed for %s: %s",
                    user_id or "local", e)
    return adopted
