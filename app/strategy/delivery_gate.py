"""The last thing that happens before a job reaches a user's board.

The live audit found 5 of 45 shortlisted jobs (11%) already dead when the user
got them. This module is the gate that stops that, and its whole design is
shaped by one rule from `app/discovery/liveness.py`:

    an endpoint that refuses to answer is NOT evidence that the job is gone.

So a 429, a 403, a timeout or a parser failure can never block delivery. Only
REMOVED and EXPIRED can, and only ever from conclusive evidence: a 404 on the
exact posting permalink, a 410, explicit removal wording in the page, a
Greenhouse posting redirected to its own board's index, the ATS's own posting
API answering 404 while the board itself answers (`_ask_ats`), or absence from
a board fetch we know was COMPLETE.

What it is NOT: a sweep. A posting is checked before a NEW delivery (the
scoring lane via `verified_dead`, `confirmed_open` for catch-up), and a
refusal there closes only the copy being placed. A copy already on a board is
re-checked only when someone opens it (`/api/jobs/{id}/verify`) or reports it
(`verify_reported`); only those two close the other copies
(`close_dead_everywhere`). Nothing revisits delivered copies on a timer (a
complete board fetch closes only the pool it ran for, which in the scheduled
global pass is the shared row: `pipeline.mark_ghost_jobs`), so a dead posting
nobody opens or reports stays on the boards it already reached until it ages
out.

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
import re
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


# ── the posting's own ATS, asked directly ────────────────────────────────────
# A page fetch speaks for a posting only when the page IS the posting. Two
# shapes where it is not (live test, 2026-10-09):
#   * a Greenhouse posting shown on the EMPLOYER's site (`…?gh_jid=<id>`, about
#     a quarter of open Greenhouse rows): that page is the employer's shell and
#     answers 200, or redirects to their careers home, whether or not the job
#     is open (Riot Games: filed WRONG_PAGE, delivered as open). A hosted
#     posting URL that REDIRECTS there lands on the same shell: the id is kept,
#     so the page reads LIVE, and that 200 is the employer's, not the ATS's;
#   * any redirect the page check could not place (WRONG_PAGE).
# Greenhouse and Lever publish one JSON document per OPEN posting. 200 there is
# LIVE. 404 is REMOVED only once the BOARD itself answers 200: a wrong or
# renamed board token 404s every posting on it, and that says nothing about
# this one. Anything else (429, 403, 5xx, a timeout) is no answer, and the page
# check decides exactly as before.
_GH_JID = re.compile(r"[?&#]gh_jid=(\d+)", re.I)
_LEVER_POSTING = re.compile(
    r"^https?://jobs\.(eu\.)?lever\.co/([^/?#]+)/([0-9a-f]{8}-[0-9a-f-]{27})", re.I)
_BOARD_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


#: The domain each ATS serves its own posting pages from. A page check that
#: ENDS outside it was answered by someone else (`_left_the_ats`).
_ATS_DOMAINS = {"greenhouse": "greenhouse.io", "lever": "lever.co"}


class _AtsTarget:
    __slots__ = ("kind", "posting_url", "board_url", "page_speaks")

    def __init__(self, kind: str, posting_url: str, board_url: str, page_speaks: bool):
        self.kind = kind                    # "greenhouse" | "lever" (metric label)
        self.posting_url = posting_url
        self.board_url = board_url
        # The job URL is the ATS's own posting page, so the page check comes
        # first and the API only settles what it could not: a WRONG_PAGE, or a
        # LIVE read from a page outside the ATS (`_left_the_ats`).
        self.page_speaks = page_speaks


def _greenhouse_target(token: str, job_id: str, *, page_speaks: bool) -> Optional[_AtsTarget]:
    if not _BOARD_TOKEN.fullmatch(token or "") or not (job_id or "").isdigit():
        return None
    base = f"https://boards-api.greenhouse.io/v1/boards/{token}"
    return _AtsTarget("greenhouse", f"{base}/jobs/{job_id}", base, page_speaks)


def _greenhouse_token_for(external_id: str) -> str:
    """The board token of a Greenhouse posting whose URL carries none (the
    employer-site ``gh_jid`` shape).

    The board scraper names a posting's company after its board token
    (`greenhouse.board_company_name`), so the token is the ONE registered
    Greenhouse slug that produces this posting's company name. No row, no
    match or two matches is "" — the posting is then checked by its page, as
    before. Reads only, and the session is closed before any request."""
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import CompanyRegistry, Job, JobSource
    from app.discovery.greenhouse import board_company_name

    try:
        with get_session() as s:
            company = s.exec(select(Job.company).where(
                Job.external_id == str(external_id),
                Job.source == JobSource.GREENHOUSE).limit(1)).first()
            if not company:
                return ""
            base = company.strip().lower()
            candidates = sorted({base.replace(" ", sep) for sep in ("", "-", "_")})
            slugs = s.exec(select(CompanyRegistry.slug).where(
                CompanyRegistry.ats == JobSource.GREENHOUSE,
                CompanyRegistry.slug.in_(candidates))).all()
    except Exception as e:      # no token is "ask the page", never a failure
        log.debug("greenhouse token lookup failed: %s", e)
        return ""
    hits = {sl for sl in slugs if board_company_name(sl) == company}
    return hits.pop() if len(hits) == 1 else ""


def _ats_target(url: str, external_id: str = "") -> Optional[_AtsTarget]:
    """Where the posting's own ATS answers for it, or None.

    ``external_id`` enables the employer-site ``gh_jid`` shape (its board
    token is looked up, see `_greenhouse_token_for`); without it only URLs
    that name their board are mapped. EU Greenhouse boards are left to the
    page check: their API host is not one we have verified."""
    from urllib.parse import parse_qs, urlparse

    from app.discovery.liveness import GREENHOUSE_POSTING

    url = (url or "").strip()
    if not url:
        return None
    m = GREENHOUSE_POSTING.match(url)
    if m:
        if ".eu.greenhouse.io" in m.group(0).lower():
            return None
        return _greenhouse_target(m.group(1), m.group(2), page_speaks=True)
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") \
            and (p.path or "").rstrip("/").lower().endswith("/embed/job_app"):
        q = parse_qs(p.query or "")
        token, job_id = (q.get("for") or [""])[0], (q.get("token") or [""])[0]
        return _greenhouse_target(token, job_id, page_speaks=False)
    m = _GH_JID.search(url)
    if m:
        # The id in the URL must be the posting we are asked about, or the
        # answer would be about some other posting.
        if not external_id or m.group(1) != str(external_id):
            return None
        token = _greenhouse_token_for(str(external_id))
        return _greenhouse_target(token, m.group(1), page_speaks=False) if token else None
    m = _LEVER_POSTING.match(url)
    if m and _BOARD_TOKEN.fullmatch(m.group(2)):
        api = f"https://api.{'eu.' if m.group(1) else ''}lever.co/v0/postings"
        return _AtsTarget("lever", f"{api}/{m.group(2)}/{m.group(3)}",
                          f"{api}/{m.group(2)}?limit=1&mode=json", page_speaks=True)
    return None


def _same_host(a: str, b: str) -> bool:
    from urllib.parse import urlparse
    try:
        return bool(a) and (urlparse(a).hostname or "") == (urlparse(b).hostname or "")
    except ValueError:
        return False


def _left_the_ats(target: _AtsTarget, requested_url: str, final_url: str) -> bool:
    """The page check of an ATS posting URL ended on a host the ATS does not
    serve: Greenhouse handing a hosted posting to the employer's own
    ``?gh_jid=`` page, whose 200 says nothing about the vacancy.

    A hop between the ATS's own hosts (``boards.greenhouse.io`` to
    ``job-boards.greenhouse.io``) still ends on the posting page, which does
    speak (a closed one goes to the board index), so it is not this case and
    costs no extra request. No final URL, or none that parses, is not this
    case either: nothing says the page went anywhere."""
    from urllib.parse import urlparse

    if not final_url or final_url == requested_url:
        return False
    try:
        host = (urlparse(final_url).hostname or "").lower()
        asked = (urlparse(requested_url).hostname or "").lower()
    except ValueError:
        return False
    if not host or host == asked:
        return False
    domain = _ATS_DOMAINS.get(target.kind, "")
    return not domain or not (host == domain or host.endswith("." + domain))


def _ask_ats(target: _AtsTarget) -> Tuple[Optional[str], str, Optional[int]]:
    """(state, reason, http_status) from the ATS's own API. ``state`` is None
    when the answer is not conclusive — the caller then trusts the page."""
    timeout = float(settings.liveness_check_timeout_seconds)
    _bump("ats_api_checks")
    status, final_url, _body, error = _fetch(target.posting_url, timeout)
    if error is None and status == 200 and _same_host(final_url, target.posting_url):
        _bump(f"ats_api:{target.kind}:{JobLivenessState.LIVE.value}")
        return JobLivenessState.LIVE.value, "ats_api_200", status
    if error is None and status in (404, 410):
        b_status, b_final, _b, b_err = _fetch(target.board_url, timeout)
        if b_err is None and b_status == 200 and _same_host(b_final, target.board_url):
            state = (JobLivenessState.EXPIRED if status == 410
                     else JobLivenessState.REMOVED).value
            _bump(f"ats_api:{target.kind}:{state}")
            return state, f"ats_api_{status}", status
        # The board did not answer either: a token that is wrong or moved.
        _bump(f"ats_api:{target.kind}:board_unconfirmed")
        return None, "ats_api_board_unconfirmed", status
    _bump(f"ats_api:{target.kind}:inconclusive")
    return None, f"ats_api_{status or 'error'}", status


#: The page, the ATS's posting document and its board: the most one check asks.
_MAX_REQUESTS_PER_CHECK = 3


def _check(url: str, external_id: str = "") -> Tuple[str, str, Optional[int]]:
    """One posting's liveness from the evidence that can speak for it:
    (state, reason, http_status). Network only — `_ats_target` closes its
    read session before the first request, and nothing is recorded here.

    The page first when it IS the posting (one request in the common case),
    the ATS's API only for what the page cannot answer: first for a posting
    shown on the employer's site; after the page otherwise, when it read
    WRONG_PAGE, or read LIVE from a page outside the ATS (a hosted posting
    redirected to the employer's ``gh_jid`` page, `_left_the_ats`). The API's
    rules are `_ask_ats`'s: 200 LIVE, 404/410 dead only when the board answers
    200, anything else leaves the page's verdict standing."""
    from app.discovery import liveness as lv

    target = _ats_target(url, external_id)
    asked = False
    if target is not None and not target.page_speaks:
        asked = True
        state, reason, status = _ask_ats(target)
        if state:
            return state, reason, status
    status, final_url, body, error = _fetch(
        url, float(settings.liveness_check_timeout_seconds))
    state, reason = lv.classify(
        status, requested_url=url, final_url=final_url, body=body, error=error)
    if target is not None and not asked:
        offsite = (state == JobLivenessState.LIVE.value
                   and _left_the_ats(target, url, final_url))
        if offsite:
            _bump("ats_api_after_offsite_page")
        if offsite or state == JobLivenessState.WRONG_PAGE.value:
            a_state, a_reason, a_status = _ask_ats(target)
            if a_state:
                return a_state, a_reason, a_status
    return state, reason, status


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
        # Someone else is already asking. Wait for their answer: a check is at
        # most `_MAX_REQUESTS_PER_CHECK` bounded requests (`_check`).
        _bump("checks_deduplicated")
        event.wait(timeout=max(1.0, _MAX_REQUESTS_PER_CHECK
                               * settings.liveness_check_timeout_seconds + 1.0))
        state, _ = _cached(src, ext)
        return state or JobLivenessState.UNKNOWN.value, "deduped"

    try:
        started = time.monotonic()
        _bump("checks_attempted")
        new_state, reason, status = _check(url, ext)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        _record_latency(elapsed_ms)
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
    _bump("checks_attempted_direct")
    state, _reason, _status = _check(url)
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
