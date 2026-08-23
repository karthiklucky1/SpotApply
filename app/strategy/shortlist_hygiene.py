"""Shortlist hygiene — keep the board inside a freshness window.

A posting that has fallen out of the freshness window is very likely already
filled or ghosted, so a job that has sat SHORTLISTED that long — never tailored,
auto-filled, or submitted — is auto-removed from the shortlist. It's marked
SKIPPED (so it lands in the "Removed" tab, visible, not deleted) which also
reopens the per-company cap slot for a fresher role.

Deliberately conservative about what it touches:
  * ONLY status == SHORTLISTED. TAILORED / AUTOFILLED / AWAITING_USER /
    READY_TO_SUBMIT / SUBMITTED / INTERVIEWING are left alone — the user already
    invested in those, so we never yank them out from under an in-flight apply.
  * TWO freshness bounds, never one (app/common/freshness.py): we prune when we
    have HELD the job longer than ``shortlist_max_age_days``, or when the
    SOURCE's date is older than ``shortlist_max_posted_age_days``. Folding those
    into a single ``coalesce(posted_at, first_seen)`` bound — the previous
    behaviour — meant an unreliable ATS date could evict a match we shortlisted
    this morning, on the very next hygiene pass.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import update
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job

log = logging.getLogger(__name__)


def prune_stale_shortlist(max_age_days: Optional[int] = None) -> int:
    """Remove SHORTLISTED apps whose posting has fallen out of the freshness
    window — on EITHER of the two bounds (app/common/freshness.py):

      * we have held it longer than ``shortlist_max_age_days``, or
      * the source says it went up over ``shortlist_max_posted_age_days`` ago.

    The window used to be a single ``coalesce(posted_at, first_seen)`` bound, so
    a match shortlisted this morning was pruned off the board on its first pass
    whenever an ATS date called the role a week old. Same rule as the render
    filter and the scoring gate, by construction.

    Global (all users, one bulk UPDATE). Returns the number pruned. Idempotent:
    once an app is SKIPPED it no longer matches, so re-running is a cheap no-op.

    ``max_age_days`` (or ``shortlist_max_age_days``) at 0 disables the WHOLE
    prune, both bounds — that is the documented contract of the setting, and a
    kill switch that only turned off half of the rule would be worse than none.
    """
    from app.common.freshness import known_ref, posting_ref
    days = int(settings.shortlist_max_age_days if max_age_days is None else max_age_days)
    if days <= 0:
        return 0
    posted_days = int(getattr(settings, "shortlist_max_posted_age_days", 0) or 0)
    now = datetime.utcnow()
    known = known_ref()
    posting = posting_ref()
    stale_clauses = [
        # We have HELD it too long — the bound that carries the product promise.
        (known != None) & (known < now - timedelta(days=days)),  # noqa: E711
    ]
    if posted_days > 0:
        # The SOURCE calls it ancient. Deliberately far looser: it suppresses
        # evergreen and long-filled listings without evicting a match we only
        # found this morning off the back of an unreliable ATS date.
        stale_clauses.append(
            (posting != None)  # noqa: E711
            & (posting < now - timedelta(days=posted_days)))
    stale = stale_clauses[0]
    for c in stale_clauses[1:]:
        stale = stale | c
    with get_session() as session:
        stale_ids = [
            r[0] if isinstance(r, tuple) else r
            for r in session.exec(
                select(Application.id).join(Job).where(
                    Application.status == ApplicationStatus.SHORTLISTED,
                    stale,
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
                notes=(f"Auto-removed from shortlist: outside the freshness window "
                       f"(held over {days}d, or posted over {posted_days}d ago)."),
            )
        )
        session.commit()
    log.info("Shortlist hygiene: pruned %d stale shortlisted app(s) (>%dd old)",
             len(stale_ids), days)
    return len(stale_ids)
