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
        try:
            extras = _semantic_extras(others, roles, user_id, limit - len(title_hits))
            return (title_hits + extras)[:limit]
        except Exception as e:
            log.warning("Adoption semantic pass failed (%s) — title-only", e)
    return title_hits[:limit]


def adopt_shared_jobs(user_id: str | None, max_age_days: int = ADOPT_MAX_AGE_DAYS,
                      limit: int = ADOPT_MAX_JOBS) -> int:
    """Copy recent, role-matching shared-pool postings into ``user_id``'s pool.

    Reuses ``_upsert`` so per-user dedupe (source+external_id and cross-source
    slug), the country gate, and direct-ATS upgrades behave exactly as if the
    jobs had been scraped for this user. Returns the number of NEW rows."""
    from app.api.server import _get_target_roles
    from app.discovery.base import RawJob
    from app.discovery.pipeline import SHARED_POOL_USER, _upsert

    # Location preferences — same defaults run_discovery uses.
    country, remote_ok = "United States", True
    p = None
    try:
        from app.autofill.answer_pack import _get_or_create_profile
        p = _get_or_create_profile(user_id=user_id)
        if p:
            # Empty = user hasn't chosen a country → no country gate (None).
            country = (getattr(p, "preferred_country", "") or "").strip() or None
            remote_ok = bool(getattr(p, "remote_ok", True))
    except Exception as e:
        log.debug("adoption: profile unavailable (default US): %s", e)

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
    with get_session() as session:
        shared = session.exec(
            select(Job)
            # Only load the columns adoption uses (RawJob fields + the freshness
            # timestamps) — the big JSON blobs (rerank_*/hire_probability_signals/
            # corporate_insights) are dead weight here. Combined with the SQL
            # freshness cutoff and a tighter cap, this replaces a 5000-full-row
            # (~16 MB) scan-to-keep-400 with a much smaller read.
            .options(load_only(
                Job.source, Job.external_id, Job.company, Job.title, Job.location,
                Job.remote, Job.url, Job.description, Job.posted_at, Job.first_seen,
            ))
            .where(Job.user_id == SHARED_POOL_USER,
                   Job.is_closed == False,  # noqa: E712
                   Job.first_seen != None,  # noqa: E711
                   # Freshness cutoff in SQL (mirrors _fresh_enough) so stale rows
                   # aren't streamed over just to be dropped in Python.
                   (Job.posted_at >= cutoff) | (Job.first_seen >= cutoff))
            .order_by(Job.first_seen.desc())
            .limit(3000)
        ).all()

    def _fresh_enough(j: Job) -> bool:
        ref = j.posted_at or j.first_seen
        if ref is None:
            return False
        if ref.tzinfo is not None:
            ref = ref.replace(tzinfo=None)
        return ref >= cutoff

    fresh = [j for j in shared if _fresh_enough(j)]
    candidates = _select_adoptable(fresh, roles, user_id, limit)
    if not candidates:
        return 0

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
    ) for j in candidates]

    inserted = _upsert(raw, user_id=user_id, preferred_country=country,
                       remote_ok=remote_ok, user_keywords=roles or None)
    log.info("Adoption: %d shared candidates → %d new jobs for user %s",
             len(candidates), inserted, user_id or "local")
    return inserted


def adopt_and_match(user_id: str | None) -> int:
    """Adoption + a matching pass — the 'instant feed' used after resume upload
    and role edits. Matching waits politely on the discovery lock; adoption
    itself never needs it (pure DB copy)."""
    adopted = adopt_shared_jobs(user_id)
    try:
        from app.common.discovery_lock import discovery_guard
        from app.matching.pipeline import run_matching
        with discovery_guard(label="instant feed") as ran:
            if ran:
                run_matching(user_id)
    except Exception as e:
        log.warning("instant-feed matching failed for %s: %s", user_id, e)
    return adopted


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
