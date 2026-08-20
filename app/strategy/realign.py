"""Re-point an existing job pool at the user's current roles.

Scores are computed against the résumé that was in place at the time, and the
scoring queue is ``rerank_score IS NULL`` — so a job scored once is never looked
at again. That is the right default (every re-score is a paid Claude call), but
it means a user who replaces their résumé and moves from, say, AI Engineer to
Software Developer keeps a board ranked against a CV they no longer use.

This module runs only when the target roles ACTUALLY change, and it does the
cheapest correct thing for each job the user has not already acted on:

  on-role   → clear the score so the scoring lane re-judges it against the new
              résumé (the lane's per-plan daily cap paces the spend)
  off-role  → take it off the board and make sure it is never scored: an
              unscored one is stamped with an off-role marker so it leaves the
              ``rerank_score IS NULL`` queue without an LLM call, and an
              already-scored one simply keeps the score it has

Jobs the user or the agent has acted on (TAILORED and beyond) are never
touched — a role change must not disturb an application in flight. Nothing is
deleted: off-role postings stay in the pool, so changing roles back re-scores
them instead of re-scraping them.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job
from app.discovery.title_filter import role_title_match

log = logging.getLogger(__name__)

# "TAILORED and beyond" — the user or the agent has put work into these, so a
# role change leaves them alone. Mirrors the company cap's protection rule.
_COMMITTED_STATUSES = frozenset({
    ApplicationStatus.TAILORED,
    ApplicationStatus.AUTOFILLED,
    ApplicationStatus.AWAITING_USER,
    ApplicationStatus.READY_TO_SUBMIT,
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.INTERVIEWING,
    ApplicationStatus.OFFER,
    ApplicationStatus.ACCEPTED,
    ApplicationStatus.REJECTED,   # historical record — keep it intact
})

# Stamped on an unscored off-role job so it exits the scoring queue without
# costing a Claude call. Deliberately a real (low) score rather than a flag:
# the scoring lane's work list is `rerank_score IS NULL`, so this is what
# "keep it in the pool but do not score it" means in this schema.
OFF_ROLE_SCORE = 0.0
OFF_ROLE_PREFIX = "Off-role"

# Stamped on the application we skip so a later role change can tell OUR skip
# apart from one the user made or the company cap made. Only our own is undone:
# a job the user declined stays declined.
_SKIP_MARKER = "[roles-realign]"


def roles_changed(old_roles: Iterable[str], new_roles: Iterable[str]) -> bool:
    """True when the two role lists differ in substance.

    Case- and order-insensitive, so re-deriving the same roles from a résumé
    the user only lightly edited is correctly seen as NO change and leaves
    their board completely alone.
    """
    def _norm(rs):
        return {(r or "").strip().lower() for r in (rs or []) if (r or "").strip()}
    return _norm(old_roles) != _norm(new_roles)


def realign_pool_to_roles(user_id: Optional[str], new_roles: list[str],
                          old_roles: Optional[list[str]] = None) -> dict:
    """Bring an existing pool in line with ``new_roles``. Returns a stat dict.

    ``old_roles`` decides what gets PARKED, and the asymmetry is deliberate.
    role_title_match is precision-oriented — it keys off distinctive domain
    tokens, so "Backend Developer" does not match a "Software Developer" user.
    Parking on "does not match the new roles" alone would therefore bury piles
    of genuinely relevant work. Getting this wrong in one direction costs the
    user a job they never see; in the other it costs one cheap score. So only a
    posting that came in FOR THE OLD ROLES and does not fit the new ones is
    parked; anything matching neither list is ambiguous and left exactly as it
    is.
    """
    stats = {"rescore": 0, "parked": 0, "unshortlisted": 0, "unparked": 0,
             "protected": 0, "capped": 0}
    if not new_roles:
        # No roles = no opinion about what belongs. Never blank a whole board
        # on an empty list.
        log.info("Realign skipped for %s — no target roles", user_id or "local")
        return stats

    uid_arg = None if (not user_id or user_id == "local") else user_id
    cap = max(0, int(getattr(settings, "realign_max_rescore", 500) or 0))

    with get_session() as session:
        cond = (Job.user_id == uid_arg) if uid_arg else Job.user_id.is_(None)
        jobs = session.exec(
            select(Job.id, Job.title, Job.rerank_score)
            .where(cond, Job.is_closed == False)  # noqa: E712
        ).all()
        if not jobs:
            return stats

        # One query for the applications rather than one per job.
        apps = {}
        for app in session.exec(
            select(Application).where(
                Application.job_id.in_([j[0] for j in jobs]))
        ).all():
            apps[app.job_id] = app

        for job_id, title, score in jobs:
            app = apps.get(job_id)
            if app is not None and app.status in _COMMITTED_STATUSES:
                stats["protected"] += 1
                continue

            on_role = role_title_match(title or "", new_roles)
            # Only a posting the OLD roles brought in is a candidate for
            # parking; see the docstring on why this is not simply "not on_role".
            was_old_role = bool(old_roles) and role_title_match(title or "", old_roles)

            if on_role:
                # Undo OUR OWN earlier park first. The re-shortlist backstop
                # skips any job that has an application at all, so leaving the
                # skipped row behind would let the job be re-scored and still
                # never reach the board. Dropping the row restores the plain
                # "not applied to yet" state. Only ever our own marker — a job
                # the user skipped stays skipped.
                if (app is not None
                        and app.status == ApplicationStatus.SKIPPED
                        and _SKIP_MARKER in (app.notes or "")):
                    session.delete(app)
                    stats["unparked"] += 1

                # Re-judge against the new résumé. Already-unscored jobs are in
                # the queue anyway; only stamped/scored ones need clearing.
                if score is None:
                    continue
                if cap and stats["rescore"] >= cap:
                    stats["capped"] += 1
                    continue
                job = session.get(Job, job_id)
                job.rerank_score = None
                job.rerank_reasoning = None
                session.add(job)
                stats["rescore"] += 1
            elif was_old_role:
                # Old domain, not the new one: off the board, and never scored.
                if app is not None and app.status == ApplicationStatus.SHORTLISTED:
                    # A SKIPPED application also stops the re-shortlist backstop
                    # from putting it straight back.
                    app.status = ApplicationStatus.SKIPPED
                    app.notes = ((app.notes or "") +
                                 f"\n{_SKIP_MARKER} Removed from the board: no longer "
                                 f"matches your target roles "
                                 f"({', '.join(new_roles)}).").strip()
                    session.add(app)
                    stats["unshortlisted"] += 1
                if score is None:
                    job = session.get(Job, job_id)
                    job.rerank_score = OFF_ROLE_SCORE
                    job.rerank_reasoning = (
                        f"{OFF_ROLE_PREFIX}: does not match your target roles "
                        f"({', '.join(new_roles)}). Kept in your pool but not scored — "
                        f"it is re-judged automatically if your roles change back."
                    )[:500]
                    session.add(job)
                    stats["parked"] += 1

        session.commit()

    if stats["capped"]:
        log.warning(
            "Realign for %s hit the re-score cap (%d): %d on-role jobs kept their "
            "previous score. Raise REALIGN_MAX_RESCORE to re-judge them.",
            user_id or "local", cap, stats["capped"])
    log.info("Realign for %s → %s", user_id or "local", stats)
    return stats


def realign_if_roles_changed(user_id: Optional[str], old_roles: list[str],
                             new_roles: list[str]) -> dict:
    """Realign only on a real role change — the safe entry point for callers."""
    if not roles_changed(old_roles, new_roles):
        log.debug("Realign skipped for %s — roles unchanged", user_id or "local")
        return {"skipped": "roles unchanged"}
    log.info("Roles changed for %s (%s → %s) — realigning the pool",
             user_id or "local", old_roles, new_roles)
    return realign_pool_to_roles(user_id, new_roles, old_roles=old_roles)
