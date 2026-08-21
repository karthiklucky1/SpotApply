"""`Job.on_role` — the board's role filter, precomputed instead of re-derived.

The "My roles" filter is ON by default in the Job Explorer, and it used to be
answered live: `role_match_terms` expands a user's target roles into ~20 terms
and the query OR'd a `title ILIKE '%term%'` for each — twice per request, since
the pagination count repeats the same predicate. A leading wildcard can never
use a b-tree index, so both halves scanned. Measured on the live board: 665ms
without the filter, 1,600-1,900ms with it, on every keystroke.

The answer does not depend on the request. A job's title and its owner's target
roles are both known at write time, and `Job` rows are per-tenant, so the
boolean lives on the row:

    write time   → `_upsert` stamps it from the user's roles
    role change  → `realign_pool_to_roles` already computes it per job; it now
                   persists what it computed
    everything   → `backfill(user_id)` for rows written before the column

Two deliberate choices:

* The predicate is `role_title_match`, the boundary-aware gate the hot lane and
  realign use — NOT the looser `%term%` substring the SQL filter used. It is the
  repo's canonical "is this on-role", and it is strictly more precise: 'chair'
  no longer matches an 'AI' role.
* NULL means "not computed yet" and is treated as ON-role by the filter. The
  gate is meant to be permissive (CLAUDE.md): a false positive gets scored and
  ranked, a false negative is a job the user never sees. So a half-backfilled
  pool shows too much, never too little.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job
from app.discovery.title_filter import role_title_match

log = logging.getLogger(__name__)

_BATCH = 500


def compute(title: str, roles: Optional[Sequence[str]]) -> Optional[bool]:
    """The stamp for one title. None when the user has no roles set — there is
    no question to answer yet, and the filter keeps NULL rows."""
    if not roles:
        return None
    return bool(role_title_match(title or "", list(roles)))


def backfill(user_id: Optional[str], roles: Optional[Sequence[str]],
             only_missing: bool = True, limit: int = 20000) -> int:
    """Stamp `on_role` across one user's pool.

    ``only_missing`` (the default) touches rows that have never been computed —
    the one-time migration case. ``False`` recomputes everything, which is what
    a role change needs.

    Projected select (id + title only): the whole point of this column is to
    stop paying for the pool on a hot path, so computing it must not drag full
    rows across the wire either. Bulk-updates in batches, like
    `_persist_prescore_rejects`, because Supabase cancels a single UPDATE over
    thousands of rows on statement_timeout.
    """
    if not roles:
        return 0
    updated = 0
    with get_session() as session:
        q = select(Job.id, Job.title).where(Job.user_id == user_id)
        if only_missing:
            q = q.where(Job.on_role.is_(None))
        rows = list(session.exec(q.limit(limit)).all())

    mappings = []
    for jid, title in rows:
        mappings.append({"id": int(jid), "on_role": compute(title, roles)})

    for start in range(0, len(mappings), _BATCH):
        batch = mappings[start:start + _BATCH]
        try:
            with get_session() as session:
                session.bulk_update_mappings(Job, batch)
                session.commit()
            updated += len(batch)
        except Exception as e:
            log.warning("on_role backfill batch %d failed (non-fatal): %s", start, e)
    if updated:
        log.info("on_role: stamped %d job(s) for %s (%s)", updated, user_id or "local",
                 "missing only" if only_missing else "recomputed")
    return updated


def stamp(job_ids: List[int], values: List[Optional[bool]]) -> int:
    """Persist already-computed values for specific jobs (realign's path)."""
    mappings = [{"id": int(j), "on_role": v} for j, v in zip(job_ids, values)]
    if not mappings:
        return 0
    done = 0
    for start in range(0, len(mappings), _BATCH):
        batch = mappings[start:start + _BATCH]
        try:
            with get_session() as session:
                session.bulk_update_mappings(Job, batch)
                session.commit()
            done += len(batch)
        except Exception as e:
            log.warning("on_role stamp batch %d failed (non-fatal): %s", start, e)
    return done
