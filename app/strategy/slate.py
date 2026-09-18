"""The day's slate: what reached the board, and what a later job must beat.

WHY THIS EXISTS (2026-09-12). The daily promise — Free 20 / Pro 35 — was
implemented as a stop. Once ``delivered_today >= target`` the finals budget
returned an allowance of zero, and all three scorers took that as "the day is
over": the pulse fast path returned before it had even run Tier-1. On three of
six observed days the board filled between 18:00 and 22:00 UTC and nothing was
looked at again until 00:00. A posting that appeared at 15:12 and would have
scored 92 waited behind thirty-five jobs scoring 71-73.

That conflates two different quantities:

    how many recommendations the user SEES today   <- the plan's promise
    whether we keep LOOKING for better ones        <- a money question

This module owns the first. The second stays in matching/finals_budget.py,
which now raises its Tier-1 gate to this module's cutoff instead of stopping.

THE RULE. While the slate has room, anything at or above the shortlist bar
takes a free slot. Once it is full, a new job must beat the weakest REPLACEABLE
entry by ``slate_displace_margin`` to take its place. If nothing is replaceable
— every entry has been opened or acted on — a job that beats the weakest entry
by the larger ``slate_overflow_margin`` is still delivered, up to
``slate_overflow_daily`` extra jobs, because a user who has read all 35 and is
still looking should not be denied the best job of the day.

REPLACEABLE means SHORTLISTED, never viewed, delivered today, and not something
the user asked for. Anything the user opened, tailored, auto-filled, submitted
or dismissed keeps its place permanently. This is the same rule the per-company
cap has used since August (``pipeline._displace_weaker_shortlisted``), which
refuses to evict anything past SHORTLISTED; the slate adds "and not viewed",
because between shortlisting and tailoring there is a state the old rule could
not see.

ONE PLACEMENT PATH. The three lanes each carried their own copy of
``today_count < shortlist_daily_limit(uid)`` plus their own call to the company
cap, which is three chances to drift and was already drifting (only the scoring
lane checked the local/provisional bar). ``place()`` is now the single writer of
a SHORTLISTED application.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlmodel import select

from app.config import settings
from app.db.models import Application, ApplicationStatus, Job

log = logging.getLogger(__name__)

# Past SHORTLISTED: the user or the agent has invested something. Never evicted.
PROTECTED_STATUSES = (
    ApplicationStatus.TAILORED,
    ApplicationStatus.AUTOFILLED,
    ApplicationStatus.AWAITING_USER,
    ApplicationStatus.READY_TO_SUBMIT,
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.INTERVIEWING,
    ApplicationStatus.OFFER,
    ApplicationStatus.ACCEPTED,
    ApplicationStatus.REJECTED,
)

# Written into ``notes`` when the slate itself removes an entry. Distinguishes
# our own housekeeping from a user's dismissal, which preference learning reads
# as an opinion (app/matching/preference_learning.py) and which delivered_today
# still counts.
SLATE_REPLACED_MARKER = "slate_replaced"


@dataclass
class Placement:
    """What happened to one qualifying job."""
    created: bool
    outcome: str            # placed | replaced | overflow | company_cap | below_cutoff | dead | exists | ineligible | unverified_location
    displaced_id: Optional[int] = None
    cutoff: Optional[float] = None

    def __bool__(self) -> bool:            # `if place(...)` reads naturally
        return self.created


def _day_start() -> datetime:
    """Midnight UTC of the current budget day.

    Deliberately reads finals_budget's clock rather than keeping its own: the
    slate and the budget must agree on when "today" ends, and a test that pins
    one has to move both or it is testing a day boundary that cannot happen.
    """
    from app.matching.finals_budget import _utc_day
    return datetime.combine(_utc_day(), datetime.min.time())


def _uid_clause(q, user_id: Optional[str]):
    uid_arg = user_id if (user_id and user_id != "local") else None
    return q.where(Application.user_id == uid_arg) if uid_arg \
        else q.where(Application.user_id.is_(None))


def todays_entries(session, user_id: Optional[str]) -> list[Application]:
    """Applications delivered to this user's board today and still on it.

    Excludes rows the SLATE removed (they are not on the board and must not
    consume a slot) but keeps rows the USER dismissed: a dismissal is an
    opinion about a job we did deliver, and re-filling behind every dismissal
    would turn the board into a treadmill the budget never escapes.
    """
    q = select(Application).where(Application.created_at >= _day_start(),
                                  Application.apply_track != "email_import")
    rows = session.exec(_uid_clause(q, user_id)).all()
    return [a for a in rows
            if not (a.status == ApplicationStatus.SKIPPED
                    and SLATE_REPLACED_MARKER in (a.notes or ""))]


def _score_of(session, app: Application) -> float:
    job = session.get(Job, app.job_id)
    return float(job.rerank_score) if (job and job.rerank_score is not None) else 0.0


def _replaceable(session, entries: list[Application]) -> list[Application]:
    return [a for a in entries
            if a.status == ApplicationStatus.SHORTLISTED and a.viewed_at is None]


def cutoff(user_id: Optional[str], session=None) -> Optional[float]:
    """The score a later job has to beat, or None while the slate has room.

    The weakest REPLACEABLE entry when there is one; otherwise the weakest
    entry of any kind, which is what an overflow candidate is measured against.
    """
    from app.common.plan_limits import shortlist_daily_limit
    from app.db.init_db import get_session

    def _compute(s):
        entries = todays_entries(s, user_id)
        if len(entries) < shortlist_daily_limit(user_id):
            return None
        pool = _replaceable(s, entries) or entries
        if not pool:
            return None
        return min(_score_of(s, a) for a in pool)

    if session is not None:
        return _compute(session)
    with get_session() as s:
        return _compute(s)


def place(session, job: Job, score: float, *, user_id: Optional[str],
          is_local: bool = False) -> Placement:
    """Decide whether ``job`` reaches the board, and make it so.

    The ONLY path that creates a SHORTLISTED application. Callers pass a job
    that has already cleared the score bar; this decides capacity, runs the
    per-company cap, and writes the row. The caller commits.
    """
    from app.common.plan_limits import shortlist_daily_limit
    from app.matching.pipeline import _AUTOFILL_SOURCES, _check_and_enforce_company_cap
    from app.strategy import delivery_gate as _delivery_gate

    uid_arg = user_id if (user_id and user_id != "local") else None

    # ── One application per job row, decided HERE ───────────────────────────
    # Every caller already did its own "is there an application?" check before
    # calling us, and production still delivered one job twice, 1.7 s apart:
    # the matching lane released its scoring claim before its Phase-3 write,
    # the scoring lane picked the still-unscored job up, and both lanes passed
    # their own check before either had committed. A check outside the writer
    # is a check that can race. Inside the ONE writer, after taking the job
    # row's lock (Postgres `FOR UPDATE`; SQLite renders no lock and is
    # single-writer anyway), the second placer waits for the first to commit
    # and then sees its row. The job's own copy is per user, so job_id alone
    # identifies (user, posting).
    session.exec(select(Job.id).where(Job.id == job.id).with_for_update())
    if session.exec(select(Application.id).where(Application.job_id == job.id)
                    .limit(1)).first() is not None:
        return _record(session, job, score, user_id, Placement(False, "exists"))

    # ── Dead postings never reach a board ───────────────────────────────────
    # Cached read only: this runs with `session` open and the codebase never
    # holds a pooled connection across network I/O. The network refresh happens
    # in the lanes, just before they call us (app/strategy/delivery_gate.py).
    # This is the backstop that cannot be bypassed — it refuses a posting we
    # already have CONCLUSIVE evidence is gone (REMOVED/EXPIRED only; a 429 or
    # a 403 is not evidence of anything and never lands here).
    #
    # Returning a non-created Placement rather than raising matters: the caller
    # continues with the next candidate, so one dead job does not stop the
    # slate from being filled by other legitimate ones — and does not force
    # delivery of anything weaker to hit the count either, because the score
    # bar and the cutoff are unchanged.
    blocked, _state = _delivery_gate.blocks_delivery(session, job.source, job.external_id)
    if blocked:
        # Refusing is not enough: the row stays open, so the lanes keep
        # nominating it. Production refused ONE posting 17 times in 7 hours
        # (04:43-13:52, the same Workday req) — each refusal cheap, all of them
        # together a candidate slot burned on a corpse every cycle, forever.
        # Close it here, where we already hold the session and the row, and the
        # queue stops offering it. Conclusive evidence only, so this cannot fire
        # on a 429 or a 403. Safe to do at delivery time by construction: this
        # job has no application yet, so nothing tailored or applied can be
        # closed by it.
        if not job.is_closed:
            job.is_closed = True
            job.closed_reason = f"Deactivated (posting {_state} before delivery)"
            session.add(job)
        log.info("Slate: refused '%s' — posting is %s before delivery", job.title, _state)
        return _record(session, job, score, user_id, Placement(False, "dead"))

    # ── A hard eligibility failure never reaches a board ────────────────────
    # The verdict was decided once (app/common/eligibility.py) and stamped on
    # the row; this is the last door, and a fit score — however high — does
    # not override it. "unknown" is held while verification is pending
    # (settings.geo_hold_unresolved). Both leave a placement event that says so.
    _elig = (getattr(job, "eligibility", None) or "")
    if _elig == "ineligible":
        log.info("Slate: refused '%s' — %s", job.title, job.eligibility_reason or "ineligible location")
        return _record(session, job, score, user_id, Placement(False, "ineligible"))
    if _elig == "unknown" and settings.geo_hold_unresolved:
        return _record(session, job, score, user_id, Placement(False, "unverified_location"))

    cap = shortlist_daily_limit(user_id)
    entries = todays_entries(session, user_id)
    visible = len(entries)

    def _create() -> None:
        track = "autofill" if job.source in _AUTOFILL_SOURCES else "manual"
        session.add(Application(
            job_id=job.id, status=ApplicationStatus.SHORTLISTED,
            apply_url=job.url, apply_track=track, user_id=uid_arg,
            provisional=is_local,
        ))

    def _done(p: Placement) -> Placement:
        return _record(session, job, score, user_id, p)

    # ── Room on the slate: the ordinary case ────────────────────────────────
    if visible < cap:
        if not _check_and_enforce_company_cap(session, job, score):
            return _done(Placement(False, "company_cap"))
        _create()
        return _done(Placement(True, "placed"))

    # ── Slate full: the challenger path ─────────────────────────────────────
    replaceable = _replaceable(session, entries)
    if replaceable:
        weakest = min(replaceable, key=lambda a: _score_of(session, a))
        weakest_score = _score_of(session, weakest)
        if float(score) >= weakest_score + max(0, settings.slate_displace_margin):
            if not _check_and_enforce_company_cap(session, job, score):
                return _done(Placement(False, "company_cap", cutoff=weakest_score))
            weakest.status = ApplicationStatus.SKIPPED
            weakest.notes = ((weakest.notes or "") + (
                f"\n{SLATE_REPLACED_MARKER}: a stronger job arrived later today "
                f"('{job.title}' at {float(score):.0f} vs {weakest_score:.0f}) and "
                f"this one had not been opened.")).strip()
            session.add(weakest)
            _create()
            log.info("Slate: '%s' (%.0f) replaced unviewed app %s (%.0f) for user %s",
                     job.title, float(score), weakest.id, weakest_score, user_id or "local")
            return _done(Placement(True, "replaced", displaced_id=weakest.id,
                                   cutoff=weakest_score))
        return _done(Placement(False, "below_cutoff", cutoff=weakest_score))

    # ── Nothing replaceable: overflow for an exceptional match ──────────────
    floor = min(_score_of(session, a) for a in entries) if entries else 0.0
    overflow_used = max(0, visible - cap)
    if overflow_used < max(0, settings.slate_overflow_daily) \
            and float(score) >= floor + max(0, settings.slate_overflow_margin):
        if not _check_and_enforce_company_cap(session, job, score):
            return _done(Placement(False, "company_cap", cutoff=floor))
        _create()
        log.info("Slate: '%s' (%.0f) delivered as overflow %d/%d for user %s — every "
                 "slate entry has been opened or acted on (floor %.0f)",
                 job.title, float(score), overflow_used + 1,
                 settings.slate_overflow_daily, user_id or "local", floor)
        return _done(Placement(True, "overflow", cutoff=floor))

    return _done(Placement(False, "below_cutoff", cutoff=floor))


# Every qualified job leaves a record of what the slate decided. The audit
# found a job that scored 78, missed placement, and only appeared on the board
# a matching pass later — and nothing said why, because refusals were logged
# selectively (dead and below_cutoff) or not at all (company cap, an existing
# row). One FunnelEvent per decision, in the caller's session so it commits
# with the decision it describes; bounded by the number of qualified jobs.
PLACEMENT_STAGE = "placement"


PLACEMENT_REPEAT_HOURS = 6


def _record(session, job: Job, score: float, user_id: Optional[str],
            placement: Placement) -> Placement:
    try:
        import json as _json
        from datetime import datetime, timedelta

        from app.db.models import FunnelEvent
        if not placement.created:
            # The re-shortlist backstop offers the same capped job to the
            # slate every 5-minute pass; the SAME refusal again is not news.
            # One indexed read (funnel_events.job_id) per refusal, bounded by
            # the number of qualified jobs. A different outcome, or the same
            # one after PLACEMENT_REPEAT_HOURS, is recorded again.
            last = session.exec(
                select(FunnelEvent.reason, FunnelEvent.created_at)
                .where(FunnelEvent.job_id == job.id,
                       FunnelEvent.stage == PLACEMENT_STAGE)
                .order_by(FunnelEvent.id.desc()).limit(1)
            ).first()
            if last is not None and last[0] == placement.outcome and last[1] is not None \
                    and datetime.utcnow() - last[1] < timedelta(hours=PLACEMENT_REPEAT_HOURS):
                return placement
        session.add(FunnelEvent(
            job_id=job.id, stage=PLACEMENT_STAGE, passed=placement.created,
            reason=placement.outcome,
            metadata_json=_json.dumps({
                "user_id": user_id or "local", "score": round(float(score), 1),
                "cutoff": placement.cutoff, "displaced_id": placement.displaced_id,
            }),
        ))
        if not placement.created and placement.outcome not in ("below_cutoff", "dead"):
            log.info("Slate: '%s' (%.0f) not delivered for user %s — %s",
                     job.title, float(score), user_id or "local", placement.outcome)
    except Exception as e:                       # the record never blocks the decision
        log.debug("placement record skipped: %s", e)
    return placement
