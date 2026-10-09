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
    outcome: str            # placed | replaced | overflow | company_cap | below_cutoff | dead | exists | paused | ineligible | unverified_location | duplicate
    displaced_id: Optional[int] = None
    cutoff: Optional[float] = None
    # The eligibility decision this placement was made under, with the rule /
    # verifier versions it came from (geo_verify.current_decision).
    eligibility: Optional[dict] = None

    def __bool__(self) -> bool:            # `if place(...)` reads naturally
        return self.created


#: `place()` refused because the owner paused their own search. The score stays
#: on the job and Resume's matching pass re-offers it (the re-shortlist backstop,
#: pipeline._reshortlist_scored_jobs), so nothing bought before the Pause is lost.
OUTCOME_PAUSED = "paused"


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


#: The slate's value of an entry whose verdict is being RE-JUDGED: its job's
#: ``rerank_score`` was cleared to send it back through the scorer (a new resume,
#: ``realign.rescore_board_for_new_resume``; a role change,
#: ``realign.realign_pool_to_roles``) and the new score has not landed. It used
#: to read as 0, so from the upload until the re-score every challenger beat
#: the "weakest" entry by the margin and replaced it, and the job came back at
#: 92 to find its application SKIPPED ("exists", never re-delivered). Unknown is
#: not weak: above every real 0-100 verdict, so a pending entry is never the
#: weakest and never the overflow floor (``place()`` reads both through
#: ``_score_of``). With EVERY comparable entry pending the floor is this value
#: and nothing overflows; the refused job keeps its score and the re-shortlist
#: backstop offers it again once the verdicts are back. Finite on purpose: it
#: can land in a placement event's ``cutoff`` and must stay valid JSON.
AWAITING_RESCORE_VALUE = 1000.0


def _awaiting_rescore(session, app: Application) -> bool:
    """On the board, but its score was cleared for a re-judge not yet done."""
    job = session.get(Job, app.job_id)
    return job is not None and job.rerank_score is None


def _score_of(session, app: Application) -> float:
    job = session.get(Job, app.job_id)
    if job is None:                       # orphaned entry: nothing to protect
        return 0.0
    if job.rerank_score is None:
        return AWAITING_RESCORE_VALUE
    return float(job.rerank_score)


def _replaceable(session, entries: list[Application]) -> list[Application]:
    """SHORTLISTED, never opened, and holding a real verdict. An entry awaiting
    its re-score is not evictable: we do not know yet that it is weaker."""
    return [a for a in entries
            if a.status == ApplicationStatus.SHORTLISTED and a.viewed_at is None
            and not _awaiting_rescore(session, a)]


def cutoff(user_id: Optional[str], session=None) -> Optional[float]:
    """The score a later job has to beat, or None while the slate has room.

    The weakest REPLACEABLE entry when there is one; otherwise the weakest
    entry of any kind, which is what an overflow candidate is measured against.

    None, too, while any of today's entries awaits its re-score. This value is
    the Tier-1 spend gate (finals_budget.challenger_gate), and the pending
    entries' own re-judgments pass through that same gate: a cutoff taken from
    the entries still scored would price the board's re-score out for the rest
    of the day. Falling back to the shortlist bar is what the gate did before
    (a pending entry read as 0); what changed is that ``place()`` no longer
    lets a challenger evict a pending entry or measure overflow against it.
    """
    from app.common.plan_limits import shortlist_daily_limit
    from app.db.init_db import get_session

    def _compute(s):
        entries = todays_entries(s, user_id)
        if len(entries) < shortlist_daily_limit(user_id):
            return None
        if any(_awaiting_rescore(s, a) for a in entries):
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

    # ── A paused search places nothing ──────────────────────────────────────
    # Every lane serves a user list read at the start of its pass, minutes
    # before it writes: the matching lane's Phase 3, the global pass, the hot
    # lane and the pulse fast path all reached here with scores bought before
    # a Pause, and only the scoring lane asked again. ONE gate, in the one
    # writer of a SHORTLISTED row, so no lane can miss it. Same shared reading
    # the lanes' per-user steps use (compute_policy.paused_now: Pause drops
    # its cache in this process). Nothing about the job changes here.
    from app.common.compute_policy import paused_now
    if paused_now(uid_arg):
        return _record(session, job, score, user_id, Placement(False, OUTCOME_PAUSED))

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
    #
    # The stamp is not trusted on its own (audit 2026-09-25, finding 1): it
    # was written under whatever rules, preferences and evidence held when the
    # copy was admitted, and all three move. So the verdict is decided AGAIN
    # here from the posting's current geography, the user's current profile and
    # the current rules — two indexed point reads in this session — and the
    # fresh answer is written back onto the row. A posting with no geography
    # row (pre-rollout) keeps its stamp and the string gate that admitted it.
    _elig = (getattr(job, "eligibility", None) or "")
    _elig_meta: Optional[dict] = None
    try:
        from app.discovery.geo_verify import current_decision
        _fresh, _elig_meta = current_decision(session, job.source, job.external_id, user_id)
    except Exception as e:                    # never let the recheck crash placement
        log.debug("Slate: eligibility recheck skipped for job %s: %s", job.id, e)
        _fresh = None
    if _fresh is not None:
        if _fresh.status != _elig or (job.eligibility_reason or "") != _fresh.reason[:200]:
            if _elig and _fresh.status != _elig:
                log.info("Slate: '%s' re-decided %s -> %s at delivery (%s)",
                         job.title, _elig, _fresh.status, _fresh.code)
            job.eligibility = _fresh.status
            job.eligibility_reason = _fresh.reason[:200]
            session.add(job)
        _elig = _fresh.status
    elif _elig_meta is not None:
        _elig_meta["decision"] = _elig or "unstamped"
        _elig_meta["code"] = "stamped"

    def _refuse(outcome: str) -> Placement:
        return _record(session, job, score, user_id,
                       Placement(False, outcome, eligibility=_elig_meta))

    if _elig == "ineligible":
        log.info("Slate: refused '%s' — %s", job.title, job.eligibility_reason or "ineligible location")
        return _refuse("ineligible")
    if _elig == "unknown" and settings.geo_hold_unresolved:
        return _refuse("unverified_location")

    # ── One posting, one recommendation — the employer's own copy preferred ─
    # The same role arrives through several doors (LinkedIn, SerpAPI, the
    # employer's ATS; old and new identity forms). Production 2026-09-26: 182
    # shortlisted rows were 176 distinct roles, one delivered three times.
    dup, dup_source = _duplicate_on_record(session, job, uid_arg)
    if dup is not None:
        if _replaceable_by_first_party(dup, dup_source, job):
            dup.status = ApplicationStatus.SKIPPED
            dup.notes = ((dup.notes or "") + f"\n{SLATE_REPLACED_MARKER}: replaced by the "
                         f"employer's own posting of the same role.").strip()
            session.add(dup)
            log.info("Slate: '%s' — the employer's posting replaced an aggregator copy (app %s)",
                     job.title, dup.id)
        else:
            return _record(session, job, score, user_id,
                           Placement(False, "duplicate", eligibility=_elig_meta))

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
        if p.eligibility is None:
            p.eligibility = _elig_meta
        if p.created:
            try:                        # once per user, ever (app/analytics/journey.py)
                from app.analytics.journey import record as _journey
                _journey(uid_arg, "first_shortlist", session=session)
            except Exception:
                pass
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


# Statuses that mean "this role is already in front of the user, or was acted
# on". A SKIPPED row is a dismissal or our own housekeeping — it does not stop a
# better copy of the same role arriving later.
_ACTIVE_STATUSES = (ApplicationStatus.SHORTLISTED,) + PROTECTED_STATUSES

#: How far back a delivered role blocks a repeat of itself.
DUPLICATE_LOOKBACK_DAYS = 40


def _norm_key(text: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "", (text or "").lower())


#: sha256 of an empty description: two postings with NO text are not the same
#: text, so this hash never makes two rows one role.
_EMPTY_TEXT_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def role_key(company, title) -> Optional[tuple]:
    """(company, title) normalised, or None when either is blank (blank
    companies are never grouped: aggregator rows often have none)."""
    c, t = (company or "").strip().lower(), _norm_key(title)
    return (c, t) if (c and t) else None


def same_role(source_a, location_a, remote_a, hash_a,
              source_b, location_b, remote_b, hash_b) -> bool:
    """Two postings with the same company and normalised title: ONE role?

    Another door (an aggregator and the ATS) -> yes. Within ONE source the same
    title can be two real requisitions (another team, another city), so it
    takes evidence that it is not:
      * the same location (a repost);
      * the same posting TEXT (``content_hash`` = sha256 of the description):
        one role posted once per city (live test 2026-10-09: Miratech's "Full
        Stack Java Engineer" as two SmartRecruiters rows, New York and Miami,
        identical text, both on the board at 72 and 78);
      * both remote: the city on a remote posting is not where the work is,
        so it cannot tell two requisitions apart for the person applying.
    """
    sa, sb = getattr(source_a, "value", source_a), getattr(source_b, "value", source_b)
    if sa != sb:
        return True
    if _norm_key(location_a) == _norm_key(location_b):
        return True
    if hash_a and hash_a == hash_b and hash_a != _EMPTY_TEXT_HASH:
        return True
    return bool(remote_a) and bool(remote_b)


def _duplicate_on_record(session, job: Job, uid_arg: Optional[str]):
    """(Application, its job's source) for the user's active application to
    the SAME role (same company, same title after normalisation, `same_role`),
    else (None, None). Bounded: one indexed read of the user's recent active
    applications at this company."""
    from datetime import timedelta
    from sqlalchemy import func
    key = role_key(job.company, job.title)
    if key is None:
        return None, None
    company = (job.company or "").strip()
    q = (select(Application, Job.title, Job.source, Job.location, Job.remote, Job.content_hash)
         .join(Job, Job.id == Application.job_id)
         .where(Application.status.in_(_ACTIVE_STATUSES),
                Application.created_at >= datetime.utcnow() - timedelta(days=DUPLICATE_LOOKBACK_DAYS),
                func.lower(Job.company) == company.lower(),
                Application.job_id != job.id))
    q = q.where(Application.user_id == uid_arg) if uid_arg else q.where(Application.user_id.is_(None))
    for app_row, title, source, location, remote, chash in session.exec(q.limit(50)).all():
        if _norm_key(title) != key[1]:
            continue
        if same_role(source, location, remote, chash,
                     job.source, job.location, job.remote, job.content_hash):
            return app_row, source
    return None, None


def _replaceable_by_first_party(existing: Application, existing_source, job: Job) -> bool:
    """An untouched aggregator copy gives way to the employer's own posting."""
    from app.matching.filters.constants import source_quality
    if existing.status != ApplicationStatus.SHORTLISTED or existing.viewed_at is not None:
        return False
    return source_quality(job.source) >= 1.0 > source_quality(existing_source)


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
                "eligibility": placement.eligibility,
            }),
        ))
        if not placement.created and placement.outcome not in ("below_cutoff", "dead"):
            log.info("Slate: '%s' (%.0f) not delivered for user %s — %s",
                     job.title, float(score), user_id or "local", placement.outcome)
    except Exception as e:                       # the record never blocks the decision
        log.debug("placement record skipped: %s", e)
    return placement
