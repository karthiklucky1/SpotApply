"""The last thing that happens before a job reaches a user's board.

The live audit found 5 of 45 shortlisted jobs (11%) already dead when the user
got them. This module is the gate that stops that, and its whole design is
shaped by one rule from `app/discovery/liveness.py`:

    an endpoint that refuses to answer is NOT evidence that the job is gone.

So a 429, a 403, a timeout or a parser failure can never block delivery. Only
REMOVED and EXPIRED can, and only ever from conclusive evidence: a 404 on the
exact posting permalink, a 410, explicit removal wording in the page, or
absence from a board fetch we know was COMPLETE.

Three properties the placement path depends on:

* **Late.** The check runs immediately before `slate.place()`, so we only spend
  a request on a job that would otherwise appear on someone's board. Discovery
  and scoring never call it — they handle orders of magnitude more candidates.
* **Outside the session.** `place()` runs inside an open `get_session()`, and
  this codebase never holds a pooled connection across network I/O
  (CLAUDE.md, DB discipline). Callers verify first, then open their session.
  `blocks_delivery` is the in-session backstop and touches no network.
* **Once per posting.** Ten users receiving the same posting perform one check,
  not ten. Liveness is a property of the posting, so it is keyed and
  single-flighted by `(source, external_id)` exactly like `JobLiveness` itself.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Optional, Tuple

from app.config import settings
from app.db.models import JobLivenessState

log = logging.getLogger(__name__)

#: Sources whose `Job.url` is the canonical permalink for ONE posting, so a 404
#: there means that posting is gone. Aggregators are excluded on purpose: their
#: url is a redirect, a search link or an employer page, and a 404 on it says
#: nothing reliable about the vacancy. Those jobs fall back to whatever board
#: evidence exists and are otherwise delivered.
_VERIFIABLE_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters",
    "workable", "recruitee", "personio", "bamboohr", "breezy",
    "pinpoint", "rippling", "join", "teamtailor",
})

# ── aggregate metrics only ───────────────────────────────────────────────────
# Requirement from the brief: no job ids, no external ids, no URLs in labels.
# Keys are fixed strings and `by_source:<ats>`, which is bounded by the number
# of ATS families we support.
_METRICS: Counter = Counter()
_LATENCY_MS_TOTAL = "latency_ms_total"
_LATENCY_N = "latency_samples"
_metrics_lock = threading.Lock()

# ── single flight ────────────────────────────────────────────────────────────
# One in-flight check per posting. The scoring lane runs `scoring_workers` (20)
# concurrent workers across ALL users, which is exactly where ten simultaneous
# checks of one posting would otherwise come from. Across containers the
# JobLiveness row itself is the coordination point: once the first process
# writes checked_at, every other process sees fresh evidence and skips.
_inflight: dict = {}
_inflight_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    with _metrics_lock:
        _METRICS[key] += n


def metrics_snapshot(reset: bool = False) -> dict:
    """Aggregate counters since process start (or since the last reset)."""
    with _metrics_lock:
        data = dict(_METRICS)
        if reset:
            _METRICS.clear()
    n = data.get(_LATENCY_N, 0)
    total = data.get(_LATENCY_MS_TOTAL, 0)
    data["latency_ms_avg"] = round(total / n, 1) if n else 0.0
    return data


def _record_latency(ms: float) -> None:
    with _metrics_lock:
        _METRICS[_LATENCY_MS_TOTAL] += int(ms)
        _METRICS[_LATENCY_N] += 1


def blocks_delivery(session, source, external_id: str) -> Tuple[bool, str]:
    """In-session backstop: is this posting CONCLUSIVELY dead?

    Pure cached read — no network, safe to call with a session open. Returns
    (blocked, state). Anything other than REMOVED/EXPIRED returns False, which
    includes every inconclusive state and every posting we have never checked.
    """
    from sqlmodel import select
    from app.db.models import JobLiveness
    from app.discovery.liveness import is_dead

    src = source.value if hasattr(source, "value") else str(source)
    try:
        row = session.exec(
            select(JobLiveness.state).where(
                JobLiveness.source == src,
                JobLiveness.external_id == str(external_id),
            )
        ).first()
    except Exception as e:      # a liveness lookup must never break placement
        log.debug("blocks_delivery lookup failed for %s: %s", src, e)
        return False, JobLivenessState.UNKNOWN.value
    state = (row[0] if isinstance(row, tuple) else row) or JobLivenessState.UNKNOWN.value
    dead = is_dead(state)
    if dead:
        # Counted separately from the network path's `blocked_before_delivery`:
        # this one costs nothing and catches the job in `place()` itself, which
        # is the only writer of a shortlisted application. Without its own
        # counter a backstop block is invisible, and "the gate blocked nothing"
        # reads identically to "the gate never ran".
        _bump("blocked_before_delivery_cached")
        _bump(f"by_source:{src}:blocked_cached")
    return dead, state


def _cached(source: str, external_id: str):
    from app.discovery.liveness import load_states
    got = load_states([(source, external_id)])
    return got.get((source, external_id), (None, None))


def _fetch(url: str, timeout: float) -> Tuple[Optional[int], str, str, Optional[str]]:
    """One bounded, SSRF-guarded request. Returns (status, final_url, body, error).

    SSRF-guarded for the same reason `app/discovery/verify.py` is: a job row can
    be created by `POST /api/jobs/submit`, so its URL is attacker-controllable,
    and this runs server-side. Redirects are followed BY HAND so every hop is
    re-checked — which is also what lets us see the final URL and detect a
    redirect to a careers index. A blocked host is an ERROR, never a removal:
    "we refused to fetch it" is not evidence the posting is gone.
    """
    import httpx
    from app.common.ssrf import guarded_request
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False,
                          headers={"User-Agent": settings.liveness_user_agent}) as client:
            r, err = guarded_request(client, "GET", url)
            if r is None:
                return None, "", "", err or "blocked"
            # Bounded read: removal wording is always near the top, and an
            # unbounded body on a delivery path is a memory risk in a container
            # that already holds torch, FAISS and Chromium (docs/MEMORY.md).
            ctype = (r.headers.get("content-type") or "").lower()
            body = r.text[:20000] if ctype.startswith("text") else ""
            return r.status_code, str(r.url), body, None
    except Exception as e:
        # Every transport failure is inconclusive by construction.
        return None, "", "", type(e).__name__


def _checked_within(state: Optional[str], checked_at, minutes: int) -> bool:
    """A conclusive verdict recorded in the last ``minutes``."""
    from datetime import datetime, timedelta
    if not checked_at or not state or state == JobLivenessState.UNKNOWN.value:
        return False
    return checked_at >= datetime.utcnow() - timedelta(minutes=max(0, int(minutes)))


def verify_for_delivery(source, external_id: str, url: str, *,
                        force: bool = False) -> Tuple[str, str]:
    """Establish liveness for a posting about to be delivered.

    Returns (state, how) where `how` is one of: cached_dead, cached_fresh,
    unverifiable, checked, deduped, disabled. Never raises — an unverifiable
    posting is delivered, because refusing to deliver on a failed check would
    be exactly the "a refusal means death" mistake this design forbids.

    ``force`` (a person is looking at this posting right now: they reported
    it gone, `verify_reported`, or just opened it, `server.verify_job`): a LIVE
    verdict younger than ``liveness_recheck_hours`` no longer settles it —
    it may have closed since delivery — but one younger than
    ``liveness_report_min_recheck_minutes`` still does, so a posting many
    people report is fetched at most that often. Everything else (dead stays
    dead, verifiable sources only, single flight, the SSRF-guarded fetch) is
    the same path.
    """
    from app.discovery import liveness as lv

    src = source.value if hasattr(source, "value") else str(source)
    ext = str(external_id)
    key = (src, ext)

    if not settings.liveness_gate_enabled:
        return JobLivenessState.UNKNOWN.value, "disabled"

    state, checked_at = _cached(src, ext)

    # Already known gone: no request, and the caller drops it.
    if lv.is_dead(state):
        _bump("avoided_check_already_dead")
        return state, "cached_dead"

    # Conclusive evidence young enough to trust.
    if force:
        fresh = _checked_within(state, checked_at,
                                settings.liveness_report_min_recheck_minutes)
    else:
        fresh = not lv.needs_check(state, checked_at,
                                   max_age_hours=settings.liveness_recheck_hours)
    if fresh:
        _bump("avoided_check_fresh_evidence")
        return state or JobLivenessState.UNKNOWN.value, "cached_fresh"

    if src not in _VERIFIABLE_SOURCES or not url:
        # No permalink we can trust, so there is nothing safe to conclude.
        # Delivered, and counted so the gap is visible rather than silent.
        _bump("unverifiable_source")
        _bump(f"by_source:{src}:unverifiable")
        return state or JobLivenessState.UNKNOWN.value, "unverifiable"

    # ── single flight ────────────────────────────────────────────────────────
    with _inflight_lock:
        event = _inflight.get(key)
        leader = event is None
        if leader:
            event = threading.Event()
            _inflight[key] = event

    if not leader:
        # Someone else is already asking. Wait briefly, then read their answer.
        _bump("checks_deduplicated")
        event.wait(timeout=max(1.0, settings.liveness_check_timeout_seconds + 1.0))
        state, _ = _cached(src, ext)
        return state or JobLivenessState.UNKNOWN.value, "deduped"

    try:
        started = time.monotonic()
        _bump("checks_attempted")
        status, final_url, body, error = _fetch(
            url, float(settings.liveness_check_timeout_seconds))
        elapsed_ms = (time.monotonic() - started) * 1000.0
        _record_latency(elapsed_ms)
        new_state, reason = lv.classify(
            status, requested_url=url, final_url=final_url, body=body, error=error)
        lv.record(src, ext, new_state, reason=reason,
                  http_status=status, checked_url=url)
        _bump(f"state:{new_state}")
        _bump(f"by_source:{src}:{new_state}")
        return new_state, "checked"
    except Exception as e:
        # The gate itself failing is inconclusive, never fatal, never dead.
        log.warning("Liveness check errored for %s (delivering anyway): %s", src, e)
        _bump("check_errored")
        return JobLivenessState.UNKNOWN.value, "unverifiable"
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        event.set()


# ── "This job is no longer available" ────────────────────────────────────────
# Owner, 2026-10-08: "so many jobs are closed". A conclusive verdict only ever
# stopped NEW deliveries (slate.place, the scoring lane): a posting that died
# after it reached people's boards stayed there until it aged out, because the
# per-posting check runs once, before delivery. A user who went to the posting
# and found it gone is the best signal we get after that — but one person's
# word never closes it for anyone else. It asks THIS module to look again, and
# only a CONCLUSIVE REMOVED/EXPIRED takes it off other boards.

#: Applications still waiting on the user — a dead posting leaves these boards.
#: ERROR is a tailor our checks blocked: still on the board, nothing delivered,
#: nothing left to rebuild for. Anything the user has worked on (TAILORED and
#: beyond) keeps its place: the job is marked closed and the decision is theirs.
_WAITING_STATUSES = ("discovered", "matched", "shortlisted", "error")


def _close_copies(session, job_ids: list, *, closed_reason: str, note: str) -> dict:
    """Close these job rows, and take each application still WAITING on a
    board to Removed with ``note`` (it must contain "job closed": preference
    learning reads that as a system skip, never as "not interested"). Work the
    user has started — TAILORED and beyond, including SUBMITTED and
    INTERVIEWING, whose postings routinely close mid-process — keeps its
    place; only the job is marked closed. The caller commits."""
    from datetime import datetime

    from sqlalchemy import update
    from sqlmodel import select

    from app.db.models import Application, ApplicationStatus, Job

    out = {"jobs_closed": 0, "applications_removed": 0, "kept_engaged": 0}
    now = datetime.utcnow()
    waiting = {ApplicationStatus(s) for s in _WAITING_STATUSES}
    done = {ApplicationStatus.SKIPPED, ApplicationStatus.REJECTED}
    for start in range(0, len(job_ids), 500):
        chunk = job_ids[start:start + 500]
        for app in session.exec(select(Application).where(Application.job_id.in_(chunk))).all():
            if app.status in waiting:
                app.status = ApplicationStatus.SKIPPED
                app.notes = ((app.notes or "") + note).strip()
                app.updated_at = now
                session.add(app)
                out["applications_removed"] += 1
            elif app.status not in done:
                out["kept_engaged"] += 1
        res = session.execute(update(Job).where(
            Job.id.in_(chunk), Job.is_closed == False,  # noqa: E712
        ).values(is_closed=True, closed_reason=closed_reason))
        out["jobs_closed"] += int(res.rowcount or 0)
    return out


def close_dead_everywhere(source, external_id: str, state: str, *,
                          found_by: str = "after a user report",
                          checked_url: str = "") -> dict:
    """Every copy of a CONCLUSIVELY dead posting stops being offered.

    By ``(source, external_id)`` — the posting key every copy keeps
    (JobLiveness uses the same one): the shared-pool row (adoption stops
    copying it), unscored copies (the queue stops paying for them) and copies
    still waiting on a board (`_close_copies`). REMOVED/EXPIRED only, and
    only for a source whose permalink speaks for the vacancy
    (`_VERIFIABLE_SOURCES`). Counts only, never ids. ``found_by`` finishes the
    closed_reason ("confirmed …").

    A BARE Workday/BambooHR/Teamtailor id (a row written before ids were
    scoped, ``job_identity.looks_unscoped``) is unique only inside ONE
    employer — CrowdStrike and GN both had ``R29845``. For those, only copies
    whose URL names the same employer as ``checked_url`` are closed; without a
    checked URL, none are.
    """
    from datetime import datetime

    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import Job
    from app.discovery.liveness import is_dead

    from app.discovery.job_identity import looks_unscoped, tenant_from_url

    src = source.value if hasattr(source, "value") else str(source)
    ext = str(external_id)
    if not is_dead(state) or src not in _VERIFIABLE_SOURCES:
        return {"jobs_closed": 0, "applications_removed": 0, "kept_engaged": 0}
    unscoped = looks_unscoped(src, ext)
    tenant = tenant_from_url(src, checked_url) if unscoped else ""
    if unscoped and not tenant:
        return {"jobs_closed": 0, "applications_removed": 0, "kept_engaged": 0}
    with get_session() as s:
        rows = s.exec(select(Job.id, Job.source, Job.url).where(Job.external_id == ext)).all()
        ids = [r[0] for r in rows
               if (r[1].value if hasattr(r[1], "value") else str(r[1])) == src
               and (not unscoped or tenant_from_url(src, r[2]) == tenant)]
        out = _close_copies(
            s, ids, closed_reason=f"Deactivated (posting {state}, confirmed {found_by})",
            note=f"\nJob closed — the posting was {state} when re-checked on "
                 f"{datetime.utcnow():%Y-%m-%d}.")
        s.commit()
    _bump("dead_copies_closed", out["jobs_closed"])
    _bump("dead_applications_removed", out["applications_removed"])
    return out


def check_url_now(url: str) -> str:
    """One SSRF-guarded fetch of exactly this URL, classified and recorded
    NOWHERE. For a bare tenant-scoped id the shared JobLiveness row (keyed by
    that id) may describe another employer's posting, so neither its cached
    verdict nor a new one written under that key can be trusted."""
    from app.discovery import liveness as lv
    _bump("checks_attempted_direct")
    status, final_url, body, error = _fetch(url, float(settings.liveness_check_timeout_seconds))
    state, _reason = lv.classify(status, requested_url=url, final_url=final_url,
                                 body=body, error=error)
    return state


def close_own_copy(job_id: int, why: str) -> dict:
    """ONE user's copy, on evidence that is not conclusive enough for anyone
    else's (an aggregator link that 404s: its url is a redirect or a search
    page, so it does not speak for the vacancy — `_VERIFIABLE_SOURCES`)."""
    from datetime import datetime

    from app.db.init_db import get_session

    with get_session() as s:
        out = _close_copies(
            s, [job_id], closed_reason=f"Deactivated ({why[:80]})",
            note=f"\nJob closed — {why[:80]} when opened on {datetime.utcnow():%Y-%m-%d}.")
        s.commit()
    return out


def verify_reported(source, external_id: str, url: str, *,
                    user_id: Optional[str] = None) -> str:
    """A user reported this posting gone: look again, and act only on proof.

    Runs as a background task after the report route has already closed the
    reporter's own copy. Returns an aggregate-safe outcome: ``disabled``,
    ``unverifiable`` (no permalink we can trust — an aggregator), ``capped``
    (the reporter's daily allowance is spent), ``not_confirmed`` (LIVE, or an
    inconclusive 403/429/timeout — never treated as gone) or
    ``confirmed_dead`` (every other copy closed by `close_dead_everywhere`).
    Never raises.
    """
    from app.discovery import liveness as lv

    src = source.value if hasattr(source, "value") else str(source)
    ext = str(external_id)
    _bump("report_received")
    try:
        from app.discovery.job_identity import looks_unscoped
        if not settings.liveness_gate_enabled:
            _bump("report_disabled")
            return "disabled"
        # Before ANY verdict, cached or new: an aggregator's link does not
        # speak for the vacancy, so nothing about it closes anyone else's copy
        # (a cached REMOVED there can be a HEAD that landed on a careers page).
        if src not in _VERIFIABLE_SOURCES or not url:
            _bump("report_unverifiable")
            _bump(f"by_source:{src}:report_unverifiable")
            return "unverifiable"
        unscoped = looks_unscoped(src, ext)
        state = None if unscoped else _cached(src, ext)[0]
        if not lv.is_dead(state):
            # A report triggers a server-side fetch, so it is rationed per
            # reporter (the posting itself is rationed inside verify_for_delivery).
            if user_id and user_id != "local":
                from app.common.daily_counter import reserve
                if not reserve(f"liveness_report:user:{user_id}",
                               int(settings.liveness_reports_per_user_daily or 0)):
                    _bump("report_capped")
                    return "capped"
            state = (check_url_now(url) if unscoped
                     else verify_for_delivery(source, ext, url, force=True)[0])
        if not lv.is_dead(state):
            _bump("report_not_confirmed")
            _bump(f"by_source:{src}:report_not_confirmed")
            return "not_confirmed"
        close_dead_everywhere(source, ext, state, checked_url=url)
        _bump("report_confirmed_dead")
        _bump(f"by_source:{src}:report_confirmed_dead")
        return "confirmed_dead"
    except Exception as e:      # a background re-check must never surface
        log.warning("Report re-check failed for %s: %s", src, e)
        _bump("report_errored")
        return "error"


class CycleBudget:
    """A wall-clock allowance for checks inside one delivery cycle.

    Requirement: the gate must never stall the shortlist pipeline. A user with
    35 deliveries, each hitting the 6s timeout, would add 210s to a lane that
    runs every 90s. Once the budget is spent the lane stops making requests and
    decides on cached evidence alone — which still blocks anything already
    known dead, and still delivers everything else.
    """

    def __init__(self, seconds: Optional[float] = None):
        self.limit = float(settings.liveness_budget_seconds_per_cycle
                           if seconds is None else seconds)
        self.spent = 0.0

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.spent >= self.limit

    def charge(self, seconds: float) -> None:
        self.spent += max(0.0, seconds)


def verified_dead(source, external_id: str, url: str,
                  budget: "Optional[CycleBudget]" = None) -> bool:
    """Convenience for the lanes: True only when the posting is conclusively
    gone and must not be delivered.

    With a `budget`, a cycle that has already spent its allowance answers from
    cached evidence instead of making another request.
    """
    from app.discovery.liveness import is_dead

    if budget is not None and budget.exhausted:
        _bump("checks_skipped_cycle_budget")
        src = source.value if hasattr(source, "value") else str(source)
        state, _checked = _cached(src, str(external_id))
        dead = is_dead(state)
        if dead:
            _bump("blocked_before_delivery")
        return dead

    started = time.monotonic()
    state, how = verify_for_delivery(source, external_id, url)
    if budget is not None and how in ("checked", "deduped"):
        budget.charge(time.monotonic() - started)
    dead = is_dead(state)
    if dead:
        _bump("blocked_before_delivery")
    return dead


def confirmed_open(source, external_id: str, url: str,
                   seen_within_hours: float = 48.0) -> bool:
    """POSITIVE evidence the posting is still open — required before a
    first-hour catch-up delivery (a posting older than the normal window).

    The everyday gate only refuses what is conclusively DEAD and delivers
    everything it cannot verify; that is right for a job found this morning,
    and wrong for one we have held ten days (owner, 2026-09-30: "make sure
    those are not closed ones"). Open means either the shared posting was seen
    on its own board within ``seen_within_hours`` (a complete board fetch
    lists only live postings), or a check of the posting itself answered LIVE.
    Anything else — unverifiable, rate-limited, blocked — is not delivered.
    """
    from datetime import datetime as _dt, timedelta as _td

    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job
    from app.discovery.pipeline import SHARED_POOL_USER

    src = source.value if hasattr(source, "value") else str(source)
    try:
        with get_session() as s:
            row = s.exec(select(Job.last_seen, Job.is_closed).where(
                Job.user_id == SHARED_POOL_USER, Job.source == source,
                Job.external_id == str(external_id)).limit(1)).first()
        if row is not None:
            last_seen, closed = row[0], row[1]
            if closed:
                _bump("catchup_refused_closed")
                return False
            if last_seen and last_seen >= _dt.utcnow() - _td(hours=seen_within_hours):
                _bump("catchup_open_recently_listed")
                return True
    except Exception as e:
        log.debug("confirmed_open lookup failed for %s: %s", src, e)
    state, _how = verify_for_delivery(source, external_id, url)
    ok = state == JobLivenessState.LIVE.value
    _bump("catchup_open_checked" if ok else "catchup_refused_unconfirmed")
    return ok


# ── Death AFTER delivery ─────────────────────────────────────────────────────
# Production review, 2026-10-10: the gate verified a posting at delivery and
# never again. The pulse lane re-fetched live boards every 5-60 minutes and
# closed nothing; `mark_ghost_jobs` reached the 400 oldest boards per 6-hour
# pass and recorded absence for SHARED rows only. So a posting that closed the
# day after it reached a board stayed there until the 5-day freshness sweep
# (14 days once tailored) — the "dead jobs" users keep opening. Two fixes, both
# conservative: absence from a COMPLETE listing closes every copy for 0 HTTP,
# and postings already on boards are re-checked on a bounded schedule.

def close_absent_from_board(source, company: str, present_ids, *, listing_complete: bool,
                            max_closes: int = 50, max_share: float = 0.5) -> dict:
    """A posting missing from a COMPLETE board listing is gone: close the shared
    row, the queued copies and the copies waiting on boards (`_close_copies`),
    and record REMOVED so the gate never needs a request for it.

    Guards, each a known failure class:
    * an incomplete or EMPTY listing records nothing — absence from a partial
      list is not absence (Workday caps a fetch at 100, a failed page truncates);
    * more than ``max_share`` of the board's known open postings vanishing at
      once is a changed filter or a renamed board, never a mass closing: skipped
      and said so (the full lane's `mark_ghost_jobs` had no such guard);
    * at most ``max_closes`` postings per board per call, so one tick's
      bookkeeping stays bounded;
    * a BARE id (job_identity.looks_unscoped) closes only this company's copies.
    Counts only, never ids."""
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import Job, JobSource
    from app.discovery import liveness as lv
    from app.discovery.job_identity import looks_unscoped
    from app.discovery.pipeline import SHARED_POOL_USER

    out = {"gone": 0, "jobs_closed": 0, "applications_removed": 0, "kept_engaged": 0, "skipped": ""}
    src = (source.value if hasattr(source, "value") else str(source or "")).lower()
    present = {str(p) for p in (present_ids or []) if p}
    if not listing_complete:
        out["skipped"] = "incomplete_listing"
        return out
    if not present:
        out["skipped"] = "empty_listing"
        return out
    if src not in _VERIFIABLE_SOURCES:
        out["skipped"] = "unverifiable_source"
        return out
    try:
        src_enum = JobSource(src)
    except ValueError:
        out["skipped"] = "unknown_source"
        return out
    company = (company or "").strip()
    if not company:
        out["skipped"] = "no_company"
        return out
    with get_session() as s:
        known = [str(e) for e in s.exec(select(Job.external_id).where(
            Job.source == src_enum, Job.company == company,
            Job.user_id == SHARED_POOL_USER, Job.is_closed == False,  # noqa: E712
        )).all() if e]
    gone = sorted({e for e in known if e not in present})
    if not gone:
        return out
    if len(known) >= 10 and len(gone) > max_share * len(known):
        out["skipped"] = "mass_absence"
        _bump("board_absence_mass_skipped")
        return out
    gone = gone[:max(1, int(max_closes))]
    out["gone"] = len(gone)
    with get_session() as s:
        rows = s.exec(select(Job.id, Job.external_id, Job.company).where(
            Job.source == src_enum, Job.external_id.in_(gone), Job.is_closed == False,  # noqa: E712
        )).all()
        ids = [r[0] for r in rows
               if not looks_unscoped(src, str(r[1])) or (r[2] or "").strip() == company]
        from datetime import datetime as _dt
        res = _close_copies(
            s, ids,
            closed_reason=f"Deactivated (posting REMOVED, absent from the {src} board listing)",
            note=f"\nJob closed — the posting left the company's {src} board on {_dt.utcnow():%Y-%m-%d}.")
        s.commit()
    out.update({k: res[k] for k in ("jobs_closed", "applications_removed", "kept_engaged")})
    # Evidence AFTER the closes committed, in its own session (a second write
    # connection while the first holds locks waits out SQLite's lock timeout).
    try:
        lv.record_board_absence(src, present_ids=present, known_ids=gone, board_complete=True)
    except Exception as e:
        log.debug("board-absence liveness record skipped for %s: %s", src, e)
    _bump("board_absence_closed", out["jobs_closed"])
    _bump("board_absence_applications_removed", out["applications_removed"])
    return out


def reverify_delivered(*, max_seconds: float = 15.0, max_checks: int = 40,
                       min_age_hours: int = 24, daily_cap: int = 2000) -> dict:
    """Re-check postings that are ON boards right now (applications still
    waiting on the user) whose liveness verdict is older than ``min_age_hours``
    or missing — oldest verdict first. A REMOVED/EXPIRED answer closes every
    copy; a refusal (429/403/timeout) changes nothing, as everywhere else.

    Bounded three ways: ``max_seconds`` of wall clock, ``max_checks`` requests
    per call, and a platform ``daily_cap`` taken from `daily_counter` (survives
    deploys and replicas). A posting already known dead but still on a board
    (the free board-absence signal landed after delivery) is closed without a
    request. Counts only."""
    from datetime import datetime as _dt

    from sqlmodel import select

    from app.common import daily_counter
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus, Job
    from app.discovery import liveness as lv

    started = time.monotonic()
    out = {"candidates": 0, "due": 0, "checked": 0, "dead": 0, "closed": 0, "skipped_cap": 0,
           "closed_known_dead": 0}
    waiting = [ApplicationStatus(s) for s in _WAITING_STATUSES]
    with get_session() as s:
        rows = s.exec(select(Job.source, Job.external_id, Job.url).join(
            Application, Application.job_id == Job.id,
        ).where(Application.status.in_(waiting), Job.is_closed == False,  # noqa: E712
                Job.url.is_not(None)).distinct().limit(2000)).all()
    pairs = []
    seen = set()
    for r in rows:
        src = r[0].value if hasattr(r[0], "value") else str(r[0])
        key = (src, str(r[1]))
        if src in _VERIFIABLE_SOURCES and r[2] and key not in seen:
            seen.add(key)
            pairs.append((src, str(r[1]), r[2]))
    out["candidates"] = len(pairs)
    if not pairs:
        return out
    states = lv.load_states([(src, ext) for src, ext, _ in pairs])
    due = []
    for src, ext, url in pairs:
        state, checked = states.get((src, ext), (None, None))
        if lv.is_dead(state):
            res = close_dead_everywhere(src, ext, state, found_by="on re-verification after delivery",
                                        checked_url=url)
            out["closed_known_dead"] += res["jobs_closed"]
            continue
        if lv.needs_check(state, checked, max_age_hours=min_age_hours):
            due.append((checked or _dt.min, src, ext, url))
    due.sort(key=lambda d: d[0])
    out["due"] = len(due)
    for _checked, src, ext, url in due[:max(0, int(max_checks))]:
        if time.monotonic() - started > max_seconds:
            break
        if not daily_counter.reserve("liveness:reverify", int(daily_cap)):
            out["skipped_cap"] += 1
            _bump("reverify_daily_cap_hit")
            break
        state, _how = verify_for_delivery(src, ext, url)
        out["checked"] += 1
        if lv.is_dead(state):
            out["dead"] += 1
            res = close_dead_everywhere(src, ext, state, found_by="on re-verification after delivery",
                                        checked_url=url)
            out["closed"] += res["jobs_closed"]
    _bump("reverify_checked", out["checked"])
    _bump("reverify_dead", out["dead"])
    return out
