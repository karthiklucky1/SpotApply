"""Shortlist hygiene — keep the board inside a freshness window.

A posting older than ``settings.shortlist_max_age_days`` is very likely already
filled or ghosted, so a job that has sat SHORTLISTED that long — never tailored,
auto-filled, or submitted — is auto-removed from the shortlist. It's marked
SKIPPED (so it lands in the "Removed" tab, visible, not deleted) which also
reopens the per-company cap slot for a fresher role.

Deliberately conservative about what it touches:
  * ONLY status == SHORTLISTED. TAILORED / AUTOFILLED / AWAITING_USER /
    READY_TO_SUBMIT / SUBMITTED / INTERVIEWING are left alone — the user already
    invested in those, so we never yank them out from under an in-flight apply.
  * Freshness = posted_at, falling back to first_seen (both stored naive-UTC),
    so a genuinely old posting we only just discovered is still treated as old.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, update
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job

log = logging.getLogger(__name__)


def prune_stale_shortlist(max_age_days: Optional[int] = None) -> int:
    """Remove SHORTLISTED apps whose posting is older than the freshness window.

    Global (all users, one bulk UPDATE). Returns the number pruned. Idempotent:
    once an app is SKIPPED it no longer matches, so re-running is a cheap no-op."""
    days = int(settings.shortlist_max_age_days if max_age_days is None else max_age_days)
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    freshness = func.coalesce(Job.posted_at, Job.first_seen)
    with get_session() as session:
        stale_ids = [
            r[0] if isinstance(r, tuple) else r
            for r in session.exec(
                select(Application.id).join(Job).where(
                    Application.status == ApplicationStatus.SHORTLISTED,
                    freshness != None,  # noqa: E711
                    freshness < cutoff,
                )
            ).all()
        ]
        if not stale_ids:
            return 0
        session.execute(
            update(Application)
            .where(Application.id.in_(stale_ids))
            .values(
                status=ApplicationStatus.SKIPPED,
                notes=f"Auto-removed from shortlist: posting older than {days} days.",
            )
        )
        session.commit()
    log.info("Shortlist hygiene: pruned %d stale shortlisted app(s) (>%dd old)",
             len(stale_ids), days)
    return len(stale_ids)
