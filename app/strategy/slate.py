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
by the larger ``slate_overflow_margin`` can still be delivered, up to
``slate_overflow_daily`` extra jobs. That allowance is 0 by default since
2026-10-30 (founder: REPLACE ONLY): a job that cannot take a slot keeps its
score and stays in All Jobs, where "Strong match · not on board" lists it, and
so does the entry a challenger replaced — neither is hidden or closed.

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
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlmodel import select

from app.common.freshness import STALE_ANCIENT, STALE_HELD
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
# The challenger replacement's note, verbatim. All Jobs' "Strong match · not on
# board" filter matches on it: a job a later, stronger one pushed off the board
# is still a strong match the user may want, but an aggregator copy replaced by
# the employer's own posting is the SAME role, already on the board.
SLATE_CHALLENGER_NOTE = f"{SLATE_REPLACED_MARKER}: a stronger job arrived later today"


@dataclass
class Placement:
    """What happened to one qualifying job."""
    created: bool
    outcome: str            # placed | replaced | overflow | company_cap | below_cutoff | dead | exists | paused | held_too_long | ancient_posting | ineligible | unverified_location | thin_posting | aggregator_link | duplicate | reposted_role
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

#: `place()` refused because the job's link opens a job board's page, not the
#: employer's application (settings.board_direct_apply_only).
OUTCOME_AGGREGATOR_LINK = "aggregator_link"

#: `place()` refused because the board's own freshness window would take the
#: entry off again on the next hygiene pass (`_outside_board_window`): we have
#: held the posting longer than the window, or its source dates it past the
#: posted bound. The names `freshness.stale_reason` returns, by construction.
OUTCOME_HELD_TOO_LONG = STALE_HELD
OUTCOME_ANCIENT_POSTING = STALE_ANCIENT

#: `place()` refused because the posting has too little description for its
#: score to mean anything (`posting_text.too_thin`, settings.min_posting_chars).
OUTCOME_THIN_POSTING = "thin_posting"


#: The short columns `place()` reads off the job besides the ones every caller
#: loads: the freshness check's three dates, and the role check's location,
#: remote flag and text hash. Read with the row lock, not one by one.
_PLACE_READS = ("first_seen", "discovered_at", "posted_at", "location", "remote", "content_hash")


def _loaded(job, name: str) -> bool:
    """False when ``job`` is a partly loaded row and reading ``name`` would
    send a SELECT for it. Anything that is not such a row counts as loaded."""
    try:
        from sqlalchemy import inspect as _inspect
        return name not in _inspect(job).unloaded
    except Exception:
        return True


def _fill_unloaded(job, locked) -> None:
    """Put what the lock statement read onto a partly loaded row, as loaded
    values (nothing is marked changed). A value the caller already holds is
    left alone: it may have set it in this session."""
    if locked is None:
        return
    try:
        from sqlalchemy import inspect as _inspect
        from sqlalchemy.orm.attributes import set_committed_value
        unloaded = _inspect(job).unloaded
        for name, value in zip(_PLACE_READS, tuple(locked)[1:]):
            if name in unloaded:
                set_committed_value(job, name, value)
    except Exception as e:                    # the reads below still work, one SELECT each
        log.debug("Slate: could not fill job %s from its lock read: %s", getattr(job, "id", None), e)


def _outside_board_window(job: Job, *, catchup: bool = False) -> str:
    """'' when the board would KEEP a delivery of this posting made right now,
    else which freshness bound it is past (an ``OUTCOME_*`` above).

    The rule is shortlist hygiene's, on purpose
    (``shortlist_hygiene.prune_stale_shortlist``, and the render filter
    ``server._shortlist_fresh_clause``): a SHORTLISTED entry stays while we
    have held its posting no longer than ``shortlist_max_age_days`` AND the
    source's date is within ``shortlist_max_posted_age_days``. A first-hour
    CATCH-UP delivery — the scoring lane's, which has just confirmed the
    posting is open and marks the entry ``delivered_catchup`` — is kept up to
    ``welcome_catchup_days`` of known age instead; nothing loosens the posted
    bound. ``shortlist_max_age_days`` at 0 turns the whole window off, both
    bounds, exactly as it does for hygiene.

    Delivery and hygiene deciding this separately is how they came to
    disagree: nothing here looked at a date, so whatever a lane scored was
    placed, counted against the day's allowance, and pruned on the next pass.
    """
    from app.common.freshness import stale_reason
    days = int(getattr(settings, "shortlist_max_age_days", 0) or 0)
    if days <= 0:
        return ""
    if catchup:
        days = max(days, int(getattr(settings, "welcome_catchup_days", 0) or 0))
    posted_days = int(getattr(settings, "shortlist_max_posted_age_days", 0) or 0)
    return stale_reason(job, days, posted_days)


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
          is_local: bool = False, catchup: bool = False) -> Placement:
    """Decide whether ``job`` reaches the board, and make it so.

    The ONLY path that creates a SHORTLISTED application. Callers pass a job
    that has already cleared the score bar; this decides capacity, runs the
    per-company cap, and writes the row. The caller commits.

    ``catchup`` is the scoring lane saying "this is a first-hour catch-up
    delivery": a posting known longer than the normal window, which it has
    just confirmed is still open and will mark ``delivered_catchup``. It widens
    the KNOWN-age window to ``welcome_catchup_days`` and nothing else.
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
    #
    # The same statement brings back the few short columns the checks below
    # read (`_PLACE_READS`). The re-shortlist backstop hands over rows loaded
    # WITHOUT them (`load_only`, to keep its 500-row read small), and an
    # attribute that was not loaded is fetched on first touch, one SELECT each:
    # three dates for the freshness check and the location for the role key,
    # on every re-offer of every pass. Asked for here they cost no round trip.
    locked = session.exec(select(Job.id, *[getattr(Job, c) for c in _PLACE_READS])
                          .where(Job.id == job.id).with_for_update()).first()
    _fill_unloaded(job, locked)
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

    # ── The board never receives what its own window would remove ───────────
    # Freshness was every lane's own business — the queue it read from, a
    # sweep that had (probably) already run — and nothing in the one writer of
    # a board entry. So when a lane's list let a stale posting through, it was
    # placed: production 2026-10-29..11-15 delivered 17 postings whose source
    # dated them 30 to 711 days back (twelve of them also held 15-46 days),
    # and shortlist hygiene removed every one on its next pass. Each had cost
    # a final, taken a slot and counted against the day's allowance for a job
    # the user never saw. Refused here on the SAME rule hygiene prunes by, so
    # the two cannot disagree again whichever lane is asking. Read from the
    # row in hand (its dates came with the lock above): no query, no network.
    # The score stays on the job.
    _stale = _outside_board_window(job, catchup=catchup)
    if _stale:
        log.info("Slate: refused '%s' — %s", job.title,
                 "its source dates the posting past the posted-age bound"
                 if _stale == OUTCOME_ANCIENT_POSTING
                 else "we have held the posting longer than the board's window")
        return _record(session, job, score, user_id, Placement(False, _stale))

    # ── A posting nobody could have read is not a match ─────────────────────
    # With no description (or a line of one) the score came from the title:
    # every cap that keeps a score honest reads the posting's words. The free
    # pre-filter now stamps such a row before any model is asked; this is the
    # same rule at the last door, for any lane that reaches here another way
    # (app/common/posting_text.py). ONLY when the text is already in hand: the
    # lanes that have just scored a job hold its description, and the
    # re-shortlist backstop deliberately does not — touching it there would
    # fetch a whole description per re-offer, every five minutes, which is the
    # egress this codebase has been over quota for. The score stays on the job.
    from app.common.posting_text import too_thin
    if _loaded(job, "description") and too_thin(getattr(job, "description", "")):
        log.info("Slate: refused '%s' — too little posting text to judge a match from",
                 job.title)
        return _record(session, job, score, user_id, Placement(False, OUTCOME_THIN_POSTING))

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

    # ── The board is jobs SpotApply can apply to ────────────────────────────
    # A link that opens a job board's page (jobicy, Adzuna, Jooble, LinkedIn,
    # Jobright…) has no form the extension can fill, and most of them ask the
    # user to register before they show the employer's. Judged by the LINK,
    # not the source (app/common/apply_link.py): a SerpAPI row can carry a
    # direct Greenhouse link, a Jobicy row never can. Refused here — the one
    # writer of a board entry — so no lane can deliver one; the job keeps its
    # score and stays in All Jobs ("Strong match · not on board"), labelled
    # with the site it opens. Refusing also frees the role for the employer's
    # own posting: an aggregator copy on the board made that one a "duplicate".
    if settings.board_direct_apply_only:
        from app.common.apply_link import aggregator_host
        _via = aggregator_host(job.url or "")
        if _via:
            log.info("Slate: refused '%s' — the link opens %s, not the employer's form",
                     job.title, _via)
            return _refuse(OUTCOME_AGGREGATOR_LINK)

    # ── One posting, one recommendation — the employer's own copy preferred ─
    # The same role arrives through several doors (LinkedIn, SerpAPI, the
    # employer's ATS; old and new identity forms). Production 2026-09-26: 182
    # shortlisted rows were 176 distinct roles, one delivered three times.
    #
    # The same role is also one title under several spellings — a city or
    # "| Remote" added to it — and one opening under two names when a
    # recruiting board republishes it (`_role_on_record`). The board's copy is
    # the one that yields: it is refused while an employer's role with that
    # title is on record, and an unopened one gives way when the employer's
    # posting arrives. A copy the user opened or acted on is never touched,
    # and never costs the employer's posting its place.
    on_record = _role_on_record(session, job, uid_arg)
    if on_record is not None:
        if on_record.kind == EMPLOYER_ROLE:
            log.info("Slate: refused '%s' — a recruiting board's copy; a role with this "
                     "title from an employer is already on record (app %s)",
                     job.title, on_record.app_id)
            return _refuse(OUTCOME_REPOSTED_ROLE)
        gives_way = on_record.untouched and (
            on_record.kind == REPOSTER_COPY or _first_party_replaces(on_record.source, job))
        earlier = session.get(Application, on_record.app_id) if gives_way else None
        if earlier is not None:
            earlier.status = ApplicationStatus.SKIPPED
            earlier.notes = ((earlier.notes or "") + "\n" + (
                SLATE_REPOST_NOTE if on_record.kind == REPOSTER_COPY else
                f"{SLATE_REPLACED_MARKER}: replaced by the employer's own posting of the "
                f"same role.")).strip()
            session.add(earlier)
            log.info("Slate: '%s' — the employer's posting replaced %s (app %s)", job.title,
                     "a recruiting board's copy" if on_record.kind == REPOSTER_COPY
                     else "an aggregator copy", earlier.id)
        elif on_record.kind == SAME_ROLE:
            return _refuse("duplicate")

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
            try:                        # the board changed: the cutoff moved
                from app.matching.finals_budget import forget_gate
                forget_gate(user_id)
            except Exception:
                pass
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
                f"\n{SLATE_CHALLENGER_NOTE} "
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
# on". A SKIPPED row that is our own housekeeping (a challenger, the company
# cap, the freshness window) does not stop a better copy of the same role
# arriving later; a SKIPPED row the USER dismissed does (`_role_on_record`).
_ACTIVE_STATUSES = (ApplicationStatus.SHORTLISTED,) + PROTECTED_STATUSES

#: How far back a delivered role blocks a repeat of itself.
DUPLICATE_LOOKBACK_DAYS = 40

#: The most rows one placement reads to look for the role, newest first. The
#: read is narrowed in SQL to this employer's rows (and a recruiting board's,
#: `_role_on_record`), so this is a ceiling, not the usual size.
DUPLICATE_SCAN_LIMIT = 200

#: `place()` refused a recruiting board's copy (apply_link.REPOSTER_BOARDS)
#: because a role with the same title, from an employer, is already on record.
#: Not "duplicate": such a posting does not name its employer, so nothing can
#: show it is the same opening. The copy keeps its score and stays in All Jobs.
OUTCOME_REPOSTED_ROLE = "reposted_role"

#: The note on a recruiting board's copy that gave way to an employer's posting
#: with the same title. All Jobs' "Strong match · not on board" lists it.
SLATE_REPOST_NOTE = (f"{SLATE_REPLACED_MARKER}: an employer's own posting with the same "
                     "title arrived; this copy came from a recruiting board")


def _norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# ── What a title says about the ROLE, apart from where and how ──────────────
# Production 2026-11-03, one board: "Virtual Assistant - Houston" at 78 beside
# "Virtual Assistant - Detroit" at 82 (one remote role, posted once per city,
# the city in each title), and "Scheduling & Operations Assistant | Remote"
# beside "Scheduling & Operations Assistant". The role key compared titles
# WHOLE, so a city or the word Remote made each copy its own role and
# `same_role` — which would have said yes, both were remote — was never asked.
#
# A title is read in PARTS (split at " - ", a dash, a bar, a slash, a comma,
# brackets). A part is left out of the key only when it says HOW the work is
# done ("Remote", "Work From Home", "Hybrid") or WHERE, and where is believed
# only from the row's own location: the part is a place the location names,
# or the country or US state the location is in. "Software Engineer -
# Payments" keeps both parts; so does "… - Houston" on a row located elsewhere.
# The FIRST part kept is never a place: "Mobile - Software Engineer" in
# Mobile, AL is a team. What is left is spelled one way ("Sr" as "Senior",
# "&" and "and" as nothing). Two rows with one key are still ONE role only if
# `same_role` agrees, so two on-site jobs in two cities stay two.
_TITLE_PART_RE = re.compile(r"\s+-+\s*|\s*-+\s+|[–—|/;:,()\[\]{}•·]+")
_LOCATION_PART_RE = re.compile(r"[,;/|()\[\]•·]+|\s+(?:or|and)\s+|\s+[-–—]\s+", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")
_SPELLING = {"sr": "senior", "snr": "senior", "jr": "junior", "and": ""}

_HOW_WORDS = frozenset({"remote", "remotely", "hybrid", "onsite", "wfh", "telecommute",
                        "telework", "anywhere", "virtual"})
_HOW_PHRASES = ("work from home", "working from home", "work at home", "home based",
                "on site", "in office", "in person")
#: Words that may stand beside a work mode in a part that still names no role
#: ("100% Remote", "Remote in the US", "Fully Remote Position"). Never enough
#: on their own: "US" alone is a place, decided by the location.
_HOW_FILLER = frozenset({"100", "fully", "full", "first", "friendly", "eligible", "optional",
                         "ok", "only", "position", "role", "opportunity", "based", "work",
                         "working", "from", "at", "home", "in", "the", "within", "or", "and",
                         "us", "usa", "u", "s", "united", "states", "nationwide", "site",
                         "on", "office", "person", "time"})
_US_KEYS = frozenset({"us", "usa", "unitedstates", "unitedstatesofamerica"})
#: Words beside a place that add nothing to it: "(US Only)", "Based in Texas".
_WHERE_FILLER = frozenset({"only", "based", "in", "the", "within", "area", "metro"})
#: A title or a location is read this far and no further: a real one is a line,
#: and the part-splitting pattern is not meant for a page of white space.
_MAX_TITLE_CHARS = 300
_MAX_LOCATION_CHARS = 400


def _says_how(words: list) -> bool:
    if any(w not in _HOW_WORDS and w not in _HOW_FILLER for w in words):
        return False
    return any(w in _HOW_WORDS for w in words) or any(p in " ".join(words) for p in _HOW_PHRASES)


class _Where:
    """What a row's own location states. Read only when a title has a part
    that might be a place, and each fact only when that part asks for it: the
    country and the state cost a pass of `geo`'s patterns over the location,
    and most parts ("Enterprise", "Payments") are settled by the names alone.
    """

    def __init__(self, location):
        self._location = str(location or "")[:_MAX_LOCATION_CHARS]
        self._names: Optional[frozenset] = None
        self._state: Optional[str] = None
        self._in_us: Optional[bool] = None

    def says(self, part: str, key: str) -> bool:
        if not self._location or not key:
            return False
        if self._names is None:
            names = set()
            for piece in _LOCATION_PART_RE.split(self._location):
                names.add(_norm_key(piece))
                names.update(_norm_key(p) for p in re.split(r"[-–—]", piece))
            self._names = frozenset(n for n in names if len(n) >= 3)
        if key in self._names:
            return True
        try:
            if key in _US_KEYS:
                if self._in_us is None:
                    from app.common import geo
                    self._in_us = geo.detect_country(self._location) == "united states"
                return self._in_us
            code = _state_names().get(key)
            if code is None and len(key) == 2 and part.strip().isupper():
                code = key
            if code is None:
                return False
            if self._state is None:
                from app.common import geo
                self._state = geo.detect_us_state(self._location) or ""
            return code == self._state
        except Exception:                     # a title key must never fail on a location
            return False


_STATE_NAMES: dict = {}


def _state_names() -> dict:
    """US state name (letters only) -> its two-letter code."""
    if not _STATE_NAMES:
        from app.common import geo
        _STATE_NAMES.update({_norm_key(label): code for code, label in geo.US_STATE_LABELS.items()})
    return _STATE_NAMES


def _spelled(words) -> str:
    return "".join(_SPELLING.get(w, w) for w in words)


def _read_title(title, location="") -> tuple:
    """(the role the title names, the places the title itself named)."""
    text = str(title or "")[:_MAX_TITLE_CHARS]
    whole = _spelled(_WORD_RE.findall(text.lower()))
    parts = [p for p in _TITLE_PART_RE.split(text) if p and p.strip()]
    if len(parts) < 2:
        return whole, frozenset()
    where = _Where(location)
    kept: list = []
    places: list = []
    for part in parts:
        words = _WORD_RE.findall(part.lower())
        if not words or _says_how(words):
            continue
        place = "".join(w for w in words if w not in _WHERE_FILLER)
        if kept and where.says(part, place):
            places.append(place)
            continue
        kept.append(_spelled(words))
    if not kept:
        return whole, frozenset()
    return "".join(kept), frozenset(places)


def title_key(title, location="") -> str:
    """The role a title names, without the parts that only say where or how."""
    return _read_title(title, location)[0]


def titled_apart(title_a, location_a, title_b, location_b) -> bool:
    """Each title names its OWN place, and they are different places: "Store
    Manager - Houston" and "Store Manager - Detroit". The country alone
    ("(US)" on both, or on one) is not a place apart."""
    a = _read_title(title_a, location_a)[1] - _US_KEYS
    b = _read_title(title_b, location_b)[1] - _US_KEYS
    return bool(a) and bool(b) and a.isdisjoint(b)


#: sha256 of an empty description: two postings with NO text are not the same
#: text, so this hash never makes two rows one role.
_EMPTY_TEXT_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def role_key(company, title, location="") -> Optional[tuple]:
    """(company, title) normalised, or None when either is blank (blank
    companies are never grouped: aggregator rows often have none). The
    company is compared by its letters and digits, so "Freddie Mac" from one
    door and "Freddiemac" (an ATS slug) from another are one employer; the
    title by `title_key`, which reads ``location`` to tell a place from a team.
    """
    c, t = _norm_key(company), title_key(title, location)
    return (c, t) if (c and t) else None


def same_role(source_a, location_a, remote_a, hash_a,
              source_b, location_b, remote_b, hash_b, *, apart: bool = False) -> bool:
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

    ``apart`` (`titled_apart`): each title named its own, different place. The
    titles only compare equal because `title_key` left those places out, so
    "another door" proves nothing — an ATS row "… - Houston" and a search
    result "… - Detroit" are two jobs unless the evidence above says one.
    """
    sa, sb = getattr(source_a, "value", source_a), getattr(source_b, "value", source_b)
    if sa != sb and not apart:
        return True
    if _norm_key(location_a) == _norm_key(location_b):
        return True
    if hash_a and hash_a == hash_b and hash_a != _EMPTY_TEXT_HASH:
        return True
    return bool(remote_a) and bool(remote_b)


#: The longest spelling `_any_spelling` writes out in full. A longer name is
#: matched by its start; the comparison in Python is the exact one.
_MAX_SPELLED_CHARS = 80


def _any_spelling(key: str, *, whole: bool = True) -> str:
    """A pattern for a LOWER-CASED column that matches ``key``'s letters and
    digits however they are spaced or punctuated: "freddiemac" finds "freddie
    mac" and "freddie-mac". The same decision as `_norm_key` equality, for
    SQL. Character classes only, and no flags, so Postgres (`~`) and SQLite
    (SQLAlchemy's REGEXP) read it the same way. ``whole`` = the entire value
    is the key; otherwise the key may sit anywhere in it."""
    gap = "[^a-z0-9]*"
    letters = key[:_MAX_SPELLED_CHARS]
    body = gap.join(letters)
    if not whole:
        return body
    return "^" + gap + body + (gap + "$" if len(key) <= _MAX_SPELLED_CHARS else "")


def _anchor_words(title, title_key_) -> list:
    """Up to the two longest words of ``title`` that are in its role key as
    written: a title with the same key has those letters in a row too. Used
    only to narrow a read; empty when no word is long enough to narrow by."""
    words = {w for w in _WORD_RE.findall(str(title or "")[:_MAX_TITLE_CHARS].lower())
             if len(w) >= 4 and w not in _SPELLING and w not in _SPELLING.values()
             and w not in _HOW_WORDS and w in title_key_}
    return sorted(words, key=lambda w: (-len(w), w))[:2]


#: What `_role_on_record` found.
SAME_ROLE = "same_role"                  # this role, at this employer
EMPLOYER_ROLE = "employer_role"          # an employer's role with this title; the job in hand is a recruiting board's copy
REPOSTER_COPY = "reposter_copy"          # a recruiting board's copy with this title; the job in hand is an employer's


@dataclass
class _OnRecord:
    """The user's earlier application this job would repeat."""
    kind: str
    app_id: int
    source: object
    untouched: bool                      # SHORTLISTED and never opened: it may give way


def _role_on_record(session, job: Job, uid_arg: Optional[str]) -> Optional[_OnRecord]:
    """The user's application on record for the SAME role as ``job``, or None.

    On record: active (`_ACTIVE_STATUSES`), or dismissed by the user — they
    said no to the role, so another copy of it (the next city, another door)
    is the same job coming back. Our own housekeeping skips never count.

    SAME_ROLE needs the same employer, the same title (`title_key`) and
    `same_role`. A recruiting board (`apply_link.REPOSTER_BOARDS`) is the one
    case judged across employers, because its posting does not name the
    employer it is for: the same title on record from an employer, or the
    board's copy on record when the employer's posting arrives. ``place()``
    makes the board's copy the one that yields, either way — title alone is
    not proof, so it never costs an employer's posting its place.

    Bounded: ONE read, narrowed in SQL to the rows that could be the role —
    this employer's (by `_any_spelling`: the old filter compared the
    lower-cased NAME, which is why "Freddie Mac" and "Freddiemac" were two
    employers), plus a recruiting board's copies when the job in hand is an
    employer's, or titles sharing this one's longest word when the job in
    hand is a board's copy. The read is paid on every placement, a refused
    re-offer included, so it stays as small as the same-company read it
    replaced. The comparison itself is made here, on what comes back.
    """
    from datetime import timedelta

    from sqlalchemy import and_, func, or_

    from app.common.apply_link import REPOSTER_BOARDS, reposter_board
    from app.matching.preference_learning import USER_DISMISS_MARKER
    key = role_key(job.company, job.title, job.location)
    if key is None:
        return None
    board = reposter_board(job.company, job.url)
    company = func.lower(Job.company)
    could_be = [company.regexp_match(_any_spelling(key[0]))]
    if board:
        anchors = _anchor_words(job.title, key[1])
        if anchors:
            title = func.lower(Job.title)
            could_be.append(and_(*[title.regexp_match(_any_spelling(w, whole=False))
                                   for w in anchors]))
        else:
            could_be = []                 # nothing to narrow by: the limit bounds it
    else:
        for name in sorted(REPOSTER_BOARDS):
            could_be.append(company.regexp_match(_any_spelling(name)))
            could_be.append(func.lower(Job.url).like(f"%/{name}/%"))
    q = (select(Application.id, Application.status, Application.viewed_at,
                Job.company, Job.title, Job.source, Job.location, Job.remote,
                Job.content_hash, Job.url)
         .join(Job, Job.id == Application.job_id)
         .where(Application.created_at >= datetime.utcnow() - timedelta(days=DUPLICATE_LOOKBACK_DAYS),
                Application.job_id != job.id,
                or_(Application.status.in_(_ACTIVE_STATUSES),
                    and_(Application.status == ApplicationStatus.SKIPPED,
                         Application.notes.contains(USER_DISMISS_MARKER, autoescape=True)))))
    if could_be:
        q = q.where(or_(*could_be))
    q = q.where(Application.user_id == uid_arg) if uid_arg else q.where(Application.user_id.is_(None))
    rows = session.exec(q.order_by(Application.created_at.desc()).limit(DUPLICATE_SCAN_LIMIT)).all()

    across = None
    for app_id, status, viewed_at, company_name, title, source, location, remote, chash, url in rows:
        employer = _norm_key(company_name)
        if not employer:
            continue
        untouched = status == ApplicationStatus.SHORTLISTED and viewed_at is None
        if employer == key[0]:
            if title_key(title, location) == key[1] and same_role(
                    source, location, remote, chash,
                    job.source, job.location, job.remote, job.content_hash,
                    apart=titled_apart(title, location, job.title, job.location)):
                return _OnRecord(SAME_ROLE, app_id, source, untouched)
            continue
        if across is not None or bool(board) == bool(reposter_board(company_name, url)):
            continue                     # two employers, or two boards: never compared
        if title_key(title, location) == key[1]:
            across = _OnRecord(EMPLOYER_ROLE if board else REPOSTER_COPY, app_id, source, untouched)
    return across


def _first_party_replaces(existing_source, job: Job) -> bool:
    """The employer's own posting against an aggregator's copy of the role."""
    from app.matching.filters.constants import source_quality
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
