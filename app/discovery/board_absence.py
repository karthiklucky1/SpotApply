"""Postings that left a board a COMPLETE fetch just read — closed, and undone.

WHY THIS EXISTS (live test, 2026-10-09). A closed Hasbro Greenhouse posting was
still open in the shared pool, with copies on other users' boards.
`pipeline.mark_ghost_jobs` closes vanished postings, but only inside the full
discovery pass, and that pass rotates boards by ``CompanyRegistry.last_seen``
ascending — the column the pulse lane bumps on every poll. So the boards the
pulse lane watches most closely were the ones nothing ever ghost-closed. For
Ashby it is worse: there is no public per-posting endpoint and every Ashby page
check reads LIVE, so absence from the board is the ONLY closure signal there is.

The pulse lane hands `reconcile_many` the listings of the boards whose fetch it
consumed and whose adapter can vouch for the whole board
(`listing_from_fetch`), AFTER its consume loop, inside a bounded time slice —
so the step never delays routing and never causes a deferral. The rules, each
of which is a way this could otherwise close a live job:

* **Only sources whose ids and URLs pin a posting to ONE board**
  (`CLOSING_SOURCES`). A posting id must be unique to the board and survive an
  edit, and the stored URL must name the board, or "absent from this listing"
  can describe a live posting. Workday (tenant-scoped requisitions, one
  requisition on several sites of a tenant, a title edit moves the path),
  Teamtailor (the id carries the title slug, so a rename mints a new id),
  BambooHR (tenant-scoped ids) and Workable (its URLs never name the board, and
  account display names collide — "Reach" is two accounts) are excluded and
  keep the full pass + 45-day retention.
* **Complete or nothing.** A capped, partial, failed, throttled (429) or
  blocked (403) fetch never gets here — absence from a subset is not absence
  (CLAUDE.md, Liveness). Each adapter proves completeness itself
  (app/discovery/base.py); an adapter that does not declare it is not trusted.
  SmartRecruiters skips non-tech titles, so presence is judged against every
  posting its LISTING named (`listed_ids`), never the filtered list.
* **This board exactly.** Candidates are shared rows of this source and these
  company names whose URL sits on this board (`board_prefix`) and that were
  first seen before the fetch STARTED (a row another lane inserted while this
  listing waited is not this listing's to judge). Rows an aggregator filed
  under the ATS bucket (`origin` set to something else) are skipped.
* **Delete nothing on any doubt.** An empty listing closes nothing. A fetch
  that would close more than `pulse_ghost_close_max_share` of the board's open
  rows (and more than `pulse_ghost_close_min_rows`) closes nothing — decided
  from one aggregate count before any row is read, and remembered while no
  new listing could have resolved it (at most a few hours). A listing that COLLAPSED
  against the board's census is held by the lane (`census_verdict`) until it
  repeats on consecutive complete polls.
* **Bounded.** One aggregate statement per board answers a steady board; rows
  are read only for a board that has something to close or reopen. Every
  closure of a tick is ONE transaction in ONE session, job rows locked in
  ascending id order before any application (`slate.place` locks the job
  first too), and the liveness verdicts are one INSERT … ON CONFLICT per
  source under a SAVEPOINT of that same transaction.
* **Self-healing.** A row closed by board absence (this module's reason, or
  mark_ghost_jobs', both starting ``ABSENCE_REASON_PREFIX``) that a later
  complete fetch lists again is reopened, its liveness verdict set back to
  LIVE, and so are its per-user copies that no application holds. A copy whose
  application was moved to Removed stays there: restoring a SHORTLISTED
  application would bypass `slate.place()`, the only writer of one.

Copies follow `delivery_gate._close_copies` exactly: an application still
WAITING (discovered/matched/shortlisted/error) goes to Removed with a
"job closed" note — a system skip, never the user's "not interested" — and
anything TAILORED or later keeps its place with only ``is_closed`` set.

Aggregate metrics only: no job id, external id or URL is ever logged or used
as a label.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import urlparse

from app.config import settings

log = logging.getLogger(__name__)

#: Every board-absence closure's ``closed_reason`` starts with these words —
#: mark_ghost_jobs' ("Removed from company <source> ATS board") and this
#: module's. Reopening matches on the prefix, so only a closure THIS kind of
#: evidence made can be undone by the opposite evidence: an age expiry, a
#: user's report or a confirmed 404 is never reopened here.
ABSENCE_REASON_PREFIX = "Removed from company "

#: Sources whose board absence closes postings. Each one's posting id is
#: platform-wide (a UUID, or one global sequence — checked against production
#: 2026-10-10), unchanged by an edit, and its stored URL names the board (or,
#: for JOIN, the company name is the board slug). Adding one needs the same
#: evidence: an id that a title edit or a second site can change, or a URL
#: shared by every board of the platform, reads a live posting as gone.
CLOSING_SOURCES = frozenset({
    "greenhouse",       # numeric job ids, platform-wide
    "lever",            # UUIDs
    "ashby",            # UUIDs; no per-posting endpoint, so this is its only signal
    "smartrecruiters",  # platform-wide numeric ids; judged on `listed_ids`
    "recruitee",        # platform-wide offer ids (the slug in the URL is not the id)
    "personio",         # platform-wide position ids
    "rippling",         # UUIDs
    "breezy",           # per-position hex ids (the title slug is not part of it)
    "pinpoint",         # platform-wide numeric ids
    # "join" is NOT here (verification 2026-10-10): its walk pages at 5 a page
    # by offset, a 404 or empty later page ended the walk still "complete",
    # and a posting unpublished mid-walk shifts the next one into a page
    # already read, so a multi-page JOIN listing can omit live postings.
})

_CHUNK = 200

# Per-statement ceiling for the step's transaction (Postgres only). A board
# whose read would run longer is skipped this tick, never waited on.
_STATEMENT_TIMEOUT_S = 10

# A board that read as "too much would close" is not read again while a new
# listing's counts could not have resolved it — and for at most this long, so
# the rows the 45-day retention closes meanwhile are eventually seen.
# Production (2026-10-10): a Lever board listing 4.5k postings holds ~21.7k
# shared rows (45-day retention at ~600 new postings/day), so it is in doubt on
# every poll, its listing size changes on every poll, and ONE census of it
# reads ~18k blocks from disk: 10.6-11.5 s.
_DOUBT_MEMO_SECONDS = 6 * 3600
# The share of the absent rows the memo assumes something else (retention, the
# full pass, a report) may have closed since the verdict. Retention alone
# closes ~0.6% of a steady pool in 6 h.
_MEMO_SLACK = 0.1
# A board whose read FAILED is left alone for this long whatever its counts
# (a dropped connection) — a read that hit the statement ceiling as long as a
# doubt: a board that big is in doubt, and re-reading it costs the most.
_ERROR_MEMO_SECONDS = 30 * 60

# More listed ids than this is not compared in one statement; such a board is
# skipped and counted.
_MAX_LISTED_IDS = 10000

_METRICS: Counter = Counter()
_metrics_lock = threading.Lock()

# board id -> (monotonic expiry, absent rows at the verdict — None = any listing).
_DOUBT_MEMO: dict[int, tuple] = {}
_doubt_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    if n:
        with _metrics_lock:
            _METRICS[key] += n


def metrics_snapshot(reset: bool = False) -> dict:
    """Aggregate counters since process start (or the last reset)."""
    with _metrics_lock:
        data = dict(_METRICS)
        if reset:
            _METRICS.clear()
    return data


def reset_state() -> None:
    """Forget the process-local memo (tests; a deploy does the same)."""
    with _doubt_lock:
        _DOUBT_MEMO.clear()


def closed_reason_for(source: str) -> str:
    return f"{ABSENCE_REASON_PREFIX}{source} ATS board (absent from a complete board fetch)"


def _source_name(source) -> str:
    return (source.value if hasattr(source, "value") else str(source or "")).strip().lower()


def closes_on_absence(source) -> bool:
    """Does a complete listing of this source's board close what it no longer
    names? (`CLOSING_SOURCES`)"""
    return _source_name(source) in CLOSING_SOURCES


def board_prefix(url: Optional[str]) -> str:
    """The part of a posting URL that names its BOARD: host (``www.`` dropped)
    plus the first path segment, lowercased. ``""`` when there is no host.

    ``job-boards.greenhouse.io/hasbro``, ``jobs.lever.co/acme`` and
    ``jobs.ashbyhq.com/acme`` each name exactly one board; one Workday tenant
    runs several sites (``fedex.wd1.myworkdayjobs.com/fxe_apac_external`` and
    ``…/fxe-lac_external_career_site``) under one company name."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    seg = next((s for s in (p.path or "").split("/") if s), "")
    return f"{host}/{seg.lower()}"


def norm_url(url: Optional[str]) -> str:
    """The form posting URLs are compared in. Case-folded and without a
    trailing slash: a stored row and a listing that spell one posting's URL
    with different case must read as the SAME posting — the safe direction,
    since a mismatch here would read a live posting as gone."""
    return (url or "").strip().rstrip("/").lower()


# ── the listing ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BoardListing:
    """What one complete fetch says the board holds."""
    source: str                    # JobSource value ("greenhouse")
    companies: frozenset           # company names the fetch's postings carry
    present_ids: frozenset         # every external id the listing named
    present_urls: frozenset        # every posting URL the listing named (`norm_url`)
    listed_count: int              # this poll's census (the lane's board_count)
    previous_count: Optional[int]  # the census the registry held before it
    board_id: Optional[int] = None
    # Wall clock when the fetch STARTED: a row first seen after it is one the
    # fetch could not have listed.
    fetched_at: Optional[datetime] = None


@dataclass
class AbsenceResult:
    outcome: str = "skipped"
    open_rows: int = 0
    absent: int = 0
    closed: int = 0                # shared rows closed
    copies_closed: int = 0         # per-user copies closed with them
    applications_removed: int = 0  # waiting applications moved to Removed
    kept_engaged: int = 0          # TAILORED+ applications left in place
    reopened: int = 0              # shared rows a complete fetch listed again
    copies_reopened: int = 0
    pending: int = 0               # absent rows the per-tick budget left


@dataclass
class _Plan:
    listing: BoardListing
    res: AbsenceResult
    close: list = field(default_factory=list)
    reopen: list = field(default_factory=list)


def listing_from_fetch(source, raw: list, meta: Optional[dict], *,
                       listed_count: int,
                       previous_count: Optional[int],
                       board_id: Optional[int] = None) -> Optional[BoardListing]:
    """The listing a fetch can vouch for, or None when it cannot speak for the
    whole board (or its source does not close on absence).

    An adapter that hands over its LISTING (`listed_ids`/`listed_urls`) is
    judged on `listing_complete`; any other on `fetch_complete` — and only when
    it SET it: the lane's old default (absent → complete) is right for the
    signature logic and wrong for closing jobs."""
    meta = meta or {}
    src = _source_name(source)
    if src not in CLOSING_SOURCES:
        return None
    listed_ids = meta.get("listed_ids")
    listed_urls = meta.get("listed_urls")
    if listed_ids is not None or listed_urls is not None:
        complete = meta.get("listing_complete") is True
    else:
        complete = meta.get("complete_declared") is True
    if not complete:
        return None
    raw = raw or []
    ids = {str(r.external_id) for r in raw if r.external_id}
    ids.update(str(e) for e in (listed_ids or ()) if e)
    urls = {norm_url(r.url) for r in raw if r.url}
    urls.update(norm_url(u) for u in (listed_urls or ()) if u)
    urls.discard("")
    fetched_at = meta.get("fetched_at")
    return BoardListing(
        source=src,
        companies=frozenset(r.company for r in raw if r.company),
        present_ids=frozenset(ids), present_urls=frozenset(urls),
        listed_count=int(listed_count or 0),
        previous_count=None if previous_count is None else int(previous_count),
        board_id=board_id,
        fetched_at=fetched_at if isinstance(fetched_at, datetime) else None,
    )


def census_verdict(listed_count: int, previous_count: Optional[int]) -> str:
    """Is this listing's size believable against the board's last census?

    ``unknown`` — no census to compare with (never polled, or zero: a board
    whose census read 0 says nothing about what it should hold now);
    ``collapse`` — it shrank by more than ``pulse_ghost_close_max_share`` and
    by more than ``pulse_ghost_close_min_rows`` (an empty listing included);
    ``ok`` otherwise. The pool can hold only part of a board (the tech titles),
    so a board serving a fraction of itself under a 200 can lose few POOL rows
    while most of its listing vanished — the share guard alone would not see
    it. The lane holds a collapsed listing until it repeats."""
    prev = int(previous_count or 0)
    if prev <= 0:
        return "unknown"
    few = max(0, int(settings.pulse_ghost_close_min_rows))
    share = float(settings.pulse_ghost_close_max_share)
    listed = max(0, int(listed_count or 0))
    if prev - listed > few and listed < (1.0 - share) * prev:
        return "collapse"
    return "ok"


def _too_many(absent: int, open_rows: int) -> bool:
    """The share guard: more than a few, and more than the allowed share of
    what the board holds in the pool, is a broken listing, not departures."""
    few = max(0, int(settings.pulse_ghost_close_min_rows))
    share = float(settings.pulse_ghost_close_max_share)
    return absent > few and absent > share * open_rows


def _chunks(seq: list) -> Iterable[list]:
    for start in range(0, len(seq), _CHUNK):
        yield seq[start:start + _CHUNK]


# ── entry points ──────────────────────────────────────────────────────────────

def reconcile(listing: BoardListing, *, budget: int) -> AbsenceResult:
    """One board: close its shared rows the listing no longer names (at most
    ``budget`` of them) and reopen the ones it names again. Never raises."""
    results, totals = reconcile_many([listing], budget=budget)
    res = results[0]
    # One board: the transaction's actual counts are this board's.
    for key in ("closed", "copies_closed", "applications_removed", "kept_engaged",
                "reopened", "copies_reopened"):
        setattr(res, key, getattr(totals, key))
    return res


def reconcile_many(listings: list, *, budget: int,
                   seconds: Optional[float] = None) -> tuple[list, AbsenceResult]:
    """Judge each listing (in order) until ``seconds`` run out, then apply every
    closure and reopening in ONE transaction. Database only, ONE session.

    Returns per-listing results (aligned with ``listings``; a listing the time
    slice never reached reads ``deferred``) and the transaction's totals.
    Closures are shared across listings by ``budget``; a board whose absent
    rows the budget could not all pay reports them as ``pending``; reopening
    is never held back by a spent closure budget. Never raises."""
    from app.db.init_db import get_session

    results = [AbsenceResult(outcome="deferred") for _ in listings]
    totals = AbsenceResult(outcome="totals")
    if not listings:
        return results, totals
    stop = (time.monotonic() + max(0.0, float(seconds))) if seconds is not None else None
    plans: list = []
    try:
        with get_session() as s:
            _arm_timeout(s)
            for i, listing in enumerate(listings):
                if stop is not None:
                    left = stop - time.monotonic()
                    if left < 1.0:
                        break
                    if left < _STATEMENT_TIMEOUT_S:
                        # Near the end of the slice a board's read may not
                        # take the full ceiling: the step must not overrun
                        # the tick's deadline by two statement timeouts.
                        _arm_timeout(s, left)
                res = AbsenceResult()
                results[i] = res
                _bump("boards_checked")
                try:
                    plan = _judge(s, listing, res)
                except Exception as e:      # one board's read never stops the rest
                    log.warning("board absence read failed (%s): %s",
                                listing.source, type(e).__name__)
                    _bump("errors")
                    res.outcome = "error"
                    # A read that hit the statement ceiling would hit it again
                    # next tick: leave this board alone for a while.
                    _remember_doubt(listing, _DOUBT_MEMO_SECONDS if _is_timeout(e)
                                    else _ERROR_MEMO_SECONDS)
                    s.rollback()
                    _arm_timeout(s)
                    continue
                if plan is not None and (plan.close or plan.reopen):
                    plans.append(plan)
                _bump(f"outcome:{res.outcome}")
            if plans:
                _allocate(plans, budget)
                try:
                    _apply(s, plans, totals)
                except Exception as e:
                    log.warning("board absence write failed: %s", type(e).__name__)
                    _bump("errors")
                    s.rollback()
                    for p in plans:
                        p.res.outcome = "error"
                        p.res.closed = p.res.reopened = p.res.pending = 0
                    totals = AbsenceResult(outcome="totals")
    except Exception as e:                  # the session itself failed
        log.warning("board absence step failed: %s", type(e).__name__)
        _bump("errors")
        for r in results:
            if r.outcome != "deferred":
                r.outcome = "error"
    for key in ("closed", "copies_closed", "applications_removed", "kept_engaged",
                "reopened", "copies_reopened"):
        _bump(key, getattr(totals, key))
    return results, totals


def _arm_timeout(session, seconds: Optional[float] = None) -> None:
    secs = float(_STATEMENT_TIMEOUT_S) if seconds is None else \
        max(1.0, min(float(_STATEMENT_TIMEOUT_S), float(seconds)))
    try:
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy import text
            session.execute(text(f"SET LOCAL statement_timeout = {int(secs * 1000)}"))
    except Exception as e:
        log.debug("board absence statement_timeout not armed: %s", type(e).__name__)


# ── the doubt memo ───────────────────────────────────────────────────────────

def _doubt_memo_active(listing: BoardListing) -> bool:
    """Is this board still in doubt WHATEVER this listing says?

    Only a row the new listing names can stop being absent, and every row it
    names adds to the board's open rows. So with ``u`` absent rows at the
    verdict (less ``_MEMO_SLACK`` of them, closed meanwhile by something else)
    and ``p`` ids listed now, at least ``u - p`` rows are still absent against
    at most ``p`` listed: if that is still past the share guard, reading the
    board again cannot change the verdict. A listing that grew enough to
    resolve it is read again; any verdict expires after ``_DOUBT_MEMO_SECONDS``.
    """
    bid = listing.board_id
    if bid is None:
        return False
    with _doubt_lock:
        entry = _DOUBT_MEMO.get(bid)
        if entry is None:
            return False
        until, absent = entry
        if time.monotonic() >= until:
            _DOUBT_MEMO.pop(bid, None)
            return False
    if absent is None:
        return True
    p = len(listing.present_ids)
    left = absent * (1.0 - _MEMO_SLACK) - p
    share = min(0.95, max(0.0, float(settings.pulse_ghost_close_max_share)))
    few = max(0, int(settings.pulse_ghost_close_min_rows))
    return left > few and left > (share / (1.0 - share)) * p


def _remember_doubt(listing: BoardListing, seconds: float = _DOUBT_MEMO_SECONDS,
                    *, absent: Optional[int] = None) -> None:
    if listing.board_id is not None:
        with _doubt_lock:
            _DOUBT_MEMO[listing.board_id] = (time.monotonic() + seconds, absent)


def _is_timeout(e: Exception) -> bool:
    msg = str(e).lower()
    return "statement timeout" in msg or "canceling statement" in msg


# ── reading one board ────────────────────────────────────────────────────────

def _judge(s, listing: BoardListing, res: AbsenceResult) -> Optional[_Plan]:
    """Decide what closes and what reopens on this board. Reads only; the
    writes are `_apply`'s.

    ONE aggregate statement first — how many open rows the listing names, how
    many on this board it does not (first seen before the fetch), and how
    many absence-closed rows it names again. A steady board ends there; a
    board in doubt ends there too, before any row (or URL) is read. Only a
    board with something to close or reopen reads those rows."""
    if not listing.present_ids and not listing.present_urls:
        # An empty listing is what a board serving a broken page under a 200
        # looks like. It never closes anything.
        res.outcome = "doubt_empty"
        return None
    if listing.source not in CLOSING_SOURCES:
        res.outcome = "excluded"
        return None
    prefixes = {board_prefix(u) for u in listing.present_urls} - {""}
    if not prefixes or not listing.companies:
        res.outcome = "unscoped"
        return None
    if len(listing.present_ids) > _MAX_LISTED_IDS:
        res.outcome = "too_large"
        return None
    if _doubt_memo_active(listing):
        res.outcome = "doubt_memo"
        return None

    rows = _board_rows(listing, prefixes)
    n_listed, n_unlisted, n_back = s.exec(
        rows.aggregate()).one()
    n_listed, n_unlisted, n_back = int(n_listed or 0), int(n_unlisted or 0), int(n_back or 0)
    res.open_rows = n_listed + n_unlisted
    res.absent = n_unlisted                   # an upper bound until the rows are read
    if not n_unlisted and not n_back:
        res.outcome = "nothing_absent"
        return None
    # The unlisted count is an UPPER bound on what would close (a row unlisted
    # by id may still be listed by URL), so doubting on it only closes less.
    in_doubt = bool(n_unlisted) and _too_many(n_unlisted, res.open_rows)
    if in_doubt:
        _remember_doubt(listing, absent=n_unlisted)
        if not n_back:
            res.outcome = "doubt_mass"
            return None

    plan = _Plan(listing, res)
    absent = []
    for r in s.exec(rows.select(unlisted=not in_doubt, back=bool(n_back))).all():
        if r.is_closed:
            plan.reopen.append(r)
        elif norm_url(r.url) not in listing.present_urls:
            absent.append(r)
    plan.reopen.sort(key=lambda r: r.id)
    if in_doubt:
        # Presence is positive evidence even when the absence side is in doubt.
        res.outcome = "doubt_mass"
        return plan
    absent.sort(key=lambda r: r.id)
    res.absent = len(absent)
    if not absent:
        res.outcome = "reopened" if plan.reopen else "nothing_absent"
        return plan
    if _too_many(len(absent), res.open_rows):     # rows appeared between the reads
        res.outcome = "doubt_mass"
        _remember_doubt(listing, absent=len(absent))
        return plan
    plan.close = absent
    res.outcome = "closed"
    return plan


class _BoardRows:
    """This board's shared rows, as one subquery the census and the row read
    share: each row flagged listed (by id), unlisted-on-this-board (URL on the
    board, first seen before the fetch) and closed-by-absence. The listed ids
    are bound once. Served by ``ix_job_user_company_title`` on its (user_id,
    company) prefix."""

    def __init__(self, listing: BoardListing, prefixes: set):
        from sqlalchemy import and_, case, false, func, or_
        from sqlmodel import select

        from app.db.models import Job, JobSource
        from app.discovery.pipeline import SHARED_POOL_USER

        src = listing.source
        on_board = []
        for p in sorted(prefixes):
            # Literal: "_" and "%" are LIKE wildcards, and an `acme_inc` board
            # must not claim `acme-inc`'s rows.
            # ("!" as the escape: a backslash is quoted differently per dialect.)
            lit = p.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            for host in (lit, f"www.{lit}"):
                for tail in ("", "/%", "?%"):
                    on_board.append(func.lower(Job.url).like(
                        f"http%://{host}{tail}", escape="!"))
        judged = or_(*on_board)
        t = listing.fetched_at
        if t is not None:
            judged = and_(judged, or_(
                Job.first_seen < t, and_(Job.first_seen.is_(None), Job.discovered_at < t)))
        absence_closed = and_(Job.is_closed == True,  # noqa: E712
                              Job.closed_reason.like(f"{ABSENCE_REASON_PREFIX}%"))
        listed = Job.external_id.in_(sorted(listing.present_ids)) \
            if listing.present_ids else false()
        self.sub = (
            select(Job.id.label("id"), Job.external_id.label("external_id"),
                   Job.url.label("url"), Job.company.label("company"),
                   Job.is_closed.label("is_closed"),
                   case((listed, 1), else_=0).label("listed"),
                   case((judged, 1), else_=0).label("judged"))
            .where(Job.user_id == SHARED_POOL_USER,
                   Job.source == JobSource(src),
                   Job.company.in_(sorted(listing.companies)),
                   or_(Job.origin.is_(None), Job.origin == src),
                   or_(Job.is_closed == False, absence_closed))  # noqa: E712
            .subquery())

    def _open_unlisted(self):
        from sqlalchemy import and_
        c = self.sub.c
        return and_(c.is_closed == False, c.listed == 0, c.judged == 1)  # noqa: E712

    def _back(self):
        from sqlalchemy import and_
        c = self.sub.c
        return and_(c.is_closed == True, c.listed == 1)  # noqa: E712

    def aggregate(self):
        from sqlalchemy import and_, case, func
        from sqlmodel import select
        c = self.sub.c

        def _n(cond):
            return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)
        return select(_n(and_(c.is_closed == False, c.listed == 1)),  # noqa: E712
                      _n(self._open_unlisted()), _n(self._back()))

    def select(self, *, unlisted: bool, back: bool):
        from sqlalchemy import false, or_
        from sqlmodel import select
        c = self.sub.c
        conds = ([self._open_unlisted()] if unlisted else []) + ([self._back()] if back else [])
        return (select(c.id, c.external_id, c.url, c.company, c.is_closed)
                .where(or_(*conds) if conds else false()))


def _board_rows(listing: BoardListing, prefixes: set) -> _BoardRows:
    return _BoardRows(listing, prefixes)


# ── writing ───────────────────────────────────────────────────────────────────

def _allocate(plans: list, budget: int) -> None:
    """Share one closure budget across the boards, in order; what a board
    could not close is reported as pending. Reopening has its own allowance
    and never waits on closures."""
    left = max(0, int(budget))
    reopen_left = max(0, int(settings.pulse_ghost_close_max_per_tick))
    for p in plans:
        if p.close:
            take = p.close[:left]
            p.res.pending = len(p.close) - len(take)
            p.close = take
            left -= len(take)
            if not take:
                p.res.outcome = "budget_spent"
            _bump("pending", p.res.pending)
        if p.reopen:
            p.reopen = p.reopen[:reopen_left]
            reopen_left -= len(p.reopen)


def _copies(s, src: str, shared_rows: list, *, closed: bool) -> dict:
    """Per-user copies of these shared rows, by shared row id: open ones
    (closing) or ones board absence closed (reopening). A copy keeps the
    company verbatim (adoption and the pulse route both copy it), so another
    company's row under the same id is not this posting's copy."""
    from sqlalchemy import or_
    from sqlmodel import select

    from app.db.models import Job, JobSource
    from app.discovery.pipeline import SHARED_POOL_USER

    by_ext: dict = defaultdict(list)
    for r in shared_rows:
        by_ext[r.external_id].append(r)
    out: dict = defaultdict(list)
    for chunk in _chunks(sorted(by_ext)):
        q = (select(Job.id, Job.external_id, Job.url, Job.company)
             .where(Job.source == JobSource(src), Job.external_id.in_(chunk),
                    or_(Job.user_id.is_(None), Job.user_id != SHARED_POOL_USER)))
        if closed:
            q = q.where(Job.is_closed == True,  # noqa: E712
                        Job.closed_reason.like(f"{ABSENCE_REASON_PREFIX}%"))
        else:
            q = q.where(Job.is_closed == False)  # noqa: E712
        for c in s.exec(q).all():
            for sh in by_ext.get(c.external_id, ()):
                if (c.company or "") == (sh.company or ""):
                    out[sh.id].append(c)
    return out


def lock_statements(ids: list) -> list:
    """The row locks `_apply` takes before it changes anything: every job row
    of the transaction, ascending by id, ``FOR UPDATE``. `slate.place` locks a
    job row and THEN moves applications; closing several postings while
    taking applications first would form the opposite order, and Postgres
    would abort one side as a deadlock."""
    from sqlmodel import select

    from app.db.models import Job

    return [select(Job.id).where(Job.id.in_(chunk)).order_by(Job.id).with_for_update()
            for chunk in _chunks(sorted(set(ids)))]


def _apply(s, plans: list, totals: AbsenceResult) -> None:
    """Every closure and reopening of this tick, in ONE transaction.

    Lock order matches `slate.place` (job row, then application): every job
    row this transaction will touch is locked first, ascending by id, and only
    then are applications moved. Liveness is written under a SAVEPOINT:
    recording evidence can never stop a job from closing."""
    from sqlmodel import select

    from app.db.models import Application
    from app.strategy.delivery_gate import _close_copies

    by_src_close: dict = defaultdict(list)
    by_src_reopen: dict = defaultdict(list)
    for p in plans:
        by_src_close[p.listing.source].extend(p.close)
        by_src_reopen[p.listing.source].extend(p.reopen)

    close_copies = {src: _copies(s, src, rows, closed=False)
                    for src, rows in by_src_close.items() if rows}
    reopen_copies = {src: _copies(s, src, rows, closed=True)
                     for src, rows in by_src_reopen.items() if rows}

    lock_ids: set = set()
    for groups, copies in ((by_src_close, close_copies), (by_src_reopen, reopen_copies)):
        for src, rows in groups.items():
            for r in rows:
                lock_ids.add(r.id)
                lock_ids.update(c.id for c in copies.get(src, {}).get(r.id, ()))
    for stmt in lock_statements(sorted(lock_ids)):
        s.exec(stmt).all()

    note = (f"\nJob closed — no longer listed on the employer's job board "
            f"({datetime.utcnow():%Y-%m-%d}).")
    removed: dict = defaultdict(list)
    for src, rows in sorted(by_src_close.items()):
        if not rows:
            continue
        reason = closed_reason_for(src)
        shared = _close_copies(s, sorted(r.id for r in rows),
                               closed_reason=reason, note=note)
        copy_ids = sorted({c.id for r in rows for c in close_copies[src].get(r.id, ())})
        theirs = _close_copies(s, copy_ids, closed_reason=reason, note=note)
        totals.closed += shared["jobs_closed"]
        totals.copies_closed += theirs["jobs_closed"]
        totals.applications_removed += theirs["applications_removed"]
        totals.kept_engaged += theirs["kept_engaged"]
        removed[src].extend(r.external_id for r in rows)
        _bump(f"by_source:{src}:closed", shared["jobs_closed"])
    for p in plans:
        p.res.closed = len(p.close)

    live: dict = defaultdict(list)
    like = f"{ABSENCE_REASON_PREFIX}%"
    for src, rows in sorted(by_src_reopen.items()):
        if not rows:
            continue
        copies = [c for r in rows for c in reopen_copies[src].get(r.id, ())]
        # A copy whose application this closure moved (waiting -> Removed),
        # or that is still waiting, stays as it is: slate.place is the only
        # writer of a SHORTLISTED application. A TAILORED-or-later copy was
        # never touched beyond is_closed, so it reopens with the posting
        # (otherwise its "May be closed" chip outlived a false closure).
        held: set = set()
        for chunk in _chunks(sorted({c.id for c in copies})):
            for jid, st in s.exec(select(Application.job_id, Application.status)
                                  .where(Application.job_id.in_(chunk))).all():
                if str(getattr(st, "value", st)).lower() in _HOLD_ON_REOPEN:
                    held.add(jid)
        totals.reopened += _reopen_rows(s, sorted(r.id for r in rows), like)
        totals.copies_reopened += _reopen_rows(
            s, sorted({c.id for c in copies if c.id not in held}), like)
        live[src].extend(r.external_id for r in rows)
    for p in plans:
        p.res.reopened = len(p.reopen)

    # The verdicts, in the SAME transaction under a SAVEPOINT: a failure there
    # rolls back to it and the closes still commit.
    from app.discovery import liveness
    try:
        with s.begin_nested():
            for src in sorted(set(removed) | set(live)):
                liveness.record_board_states(src, removed=removed.get(src, ()),
                                             live=live.get(src, ()), session=s)
    except Exception as e:
        log.debug("board absence liveness record skipped: %s", type(e).__name__)
    s.commit()


_HOLD_ON_REOPEN = frozenset({"discovered", "matched", "shortlisted", "error", "skipped"})


def _reopen_rows(s, ids: list, like: str) -> int:
    from sqlalchemy import update

    from app.db.models import Job

    n = 0
    for chunk in _chunks(ids):
        got = s.execute(update(Job).where(
            Job.id.in_(chunk), Job.is_closed == True,  # noqa: E712
            Job.closed_reason.like(like),
        ).values(is_closed=False, closed_reason=None))
        n += int(got.rowcount or 0)
    return n
