"""Is this posting still real?

The live audit found 5 of 45 shortlisted jobs (11%) already dead or unusable:
a Greenhouse 404, removed ATS postings, a Teamtailor "position no longer
active", a 410, a careers index served instead of the job, and a Workday
tenant returning 403. Those are six different situations and only four of them
mean the job is gone.

The rule this module exists to enforce: **an endpoint that refuses to answer is
not evidence that the job is dead.** 429 and 403 say something about us or the
board, not about the vacancy. Treating them as death would close live jobs
every time a board rate-limited a polling lane — and the pulse lane polls
watchlist boards every five minutes.

Only REMOVED and EXPIRED are grounds for closing a posting. Everything else
either leaves it alone or asks again later.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Iterable, Optional, Tuple

from app.db.models import JobLivenessState

log = logging.getLogger(__name__)

#: Body text that means the employer took the posting down, even though the
#: server happily returned 200. Matched case-insensitively against a bounded
#: prefix of the response. Kept deliberately literal: a generic word like
#: "closed" appears in plenty of live postings ("closed-loop systems").
# Written as patterns rather than literals because the real wording varies in
# ways a fixed string misses: Teamtailor says "This position IS no longer
# active", which the literal "position no longer active" does not match. The
# optional-filler group absorbs that class of difference without loosening the
# match into anything a live posting would trip.
_FILL = r"(?:\s+(?:is|has been|was|are|have been))?"
_GONE_PATTERNS = tuple(re.compile(p, re.I) for p in (
    rf"\b(?:position|posting|job|vacancy|role|opportunity|listing){_FILL}\s+"
    r"(?:no longer (?:active|available|open|accepting)|closed|filled|expired|"
    r"been filled|been closed|been removed)\b",
    r"\bno longer accepting applications\b",
    r"\bapplications?\s+(?:are|is|has been|have been)?\s*closed\b",
    r"\bthe (?:job|position|page) you (?:are|were) looking for\b[^.]{0,40}"
    r"\b(?:no longer|not|cannot be found|doesn't exist|does not exist)\b",
    r"\bthis (?:job|position|posting) has expired\b",
    r"\bwe are no longer (?:hiring|accepting)\b",
))

#: A careers index or board home page served in place of the specific posting.
#: Two independent signals: the final URL lost the job identifier, or the body
#: reads like a listing page.
_INDEX_PATH = re.compile(
    r"/(?:jobs|careers|openings|positions|search|board|opportunities)/?$", re.I)
_INDEX_BODY = re.compile(
    r"(all open (?:roles|positions|jobs)|browse (?:all )?(?:jobs|openings)|"
    r"\b\d+\s+open (?:roles|positions|jobs)\b)", re.I)

#: A posting on Greenhouse's own hosted board: ``/{board token}/jobs/{id}``.
#: (EU boards included; the token is case-insensitive on Greenhouse's side.)
GREENHOUSE_POSTING = re.compile(
    r"^https?://(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([^/?#]+)/jobs/(\d+)", re.I)

#: Only these mean the vacancy is gone.
DEAD_STATES = (JobLivenessState.REMOVED.value, JobLivenessState.EXPIRED.value)
#: These mean "ask again", and must never close a job.
INCONCLUSIVE_STATES = (
    JobLivenessState.RATE_LIMITED.value,
    JobLivenessState.BLOCKED.value,
    JobLivenessState.UNKNOWN.value,
)


def classify(status: Optional[int], *, requested_url: str = "",
             final_url: str = "", body: str = "",
             error: Optional[str] = None) -> Tuple[str, str]:
    """Map one HTTP result onto a liveness state. Pure; no I/O.

    Returns (state, reason). `reason` is a short machine-readable cause, safe
    to store and to aggregate in metrics — it never contains page text.
    """
    S = JobLivenessState
    if error:
        # Transport failure says nothing about the vacancy.
        return S.UNKNOWN.value, f"transport:{error[:40]}"
    if status is None:
        return S.UNKNOWN.value, "no_response"

    if status == 410:
        return S.EXPIRED.value, "http_410"
    if status == 404:
        # A 404 on the exact posting URL is the strongest removal signal an ATS
        # gives. It is still only trusted for a URL that identifies ONE posting;
        # a 404 on a board root means the board moved, not that a job died.
        if _INDEX_PATH.search((requested_url or "").split("?")[0]):
            return S.UNKNOWN.value, "http_404_on_index_url"
        return S.REMOVED.value, "http_404"
    if status == 429:
        return S.RATE_LIMITED.value, "http_429"
    if status in (401, 403):
        return S.BLOCKED.value, f"http_{status}"
    if 500 <= status <= 599:
        return S.UNKNOWN.value, f"http_{status}"
    if status not in (200, 201, 203, 206):
        return S.UNKNOWN.value, f"http_{status}"

    # 200 from here on. The body decides.
    # Collapse whitespace first: these strings are usually split across markup.
    head = re.sub(r"\s+", " ", (body or "")[:20000])
    for pattern in _GONE_PATTERNS:
        if pattern.search(head):
            return S.REMOVED.value, "body_no_longer_active"

    # Greenhouse answers a closed posting by redirecting to the board's own
    # index (`/hasbro?error=true`, "The job you are looking for is no longer
    # open."), and that message is drawn in the browser, so the body we read
    # never says it. Live test 2026-10-09: Hasbro and Accela, both closed, were
    # filed WRONG_PAGE ("redirected_to_index") and the board said "still open".
    # This is not the generic careers-index case below: the redirect stays on
    # Greenhouse, lands on the SAME board and drops the id, which is what the
    # ATS does for exactly one reason.
    if greenhouse_board_redirect(requested_url, final_url):
        return S.REMOVED.value, "greenhouse_redirect_to_board"

    # Redirected away from the posting to a listing page. Compare identifiers
    # rather than whole URLs: many boards legitimately canonicalise a slug.
    if final_url and requested_url and final_url != requested_url:
        req_tail = _identifier(requested_url)
        fin_tail = _identifier(final_url)
        if req_tail and req_tail not in (final_url or "").lower():
            if _INDEX_PATH.search(final_url.split("?")[0]) or not fin_tail:
                return S.WRONG_PAGE.value, "redirected_to_index"
            return S.WRONG_PAGE.value, "redirected_away"
    if not final_url and _INDEX_BODY.search(head) and _INDEX_PATH.search(
            (requested_url or "").split("?")[0]):
        return S.WRONG_PAGE.value, "index_body"

    return S.LIVE.value, "http_200"


def greenhouse_board_redirect(requested_url: str, final_url: str) -> bool:
    """A Greenhouse posting URL that ended on its own board's index.

    True only when ALL of: the request was a hosted Greenhouse posting
    (`GREENHOUSE_POSTING`); the final URL is still on greenhouse.io; the
    posting id is gone from it; and it is the same board's root or carries
    Greenhouse's ``error=true`` flag. A redirect to the employer's own careers
    page (``?gh_jid=<id>`` keeps the id) or anywhere else is not this case.
    That employer page reads LIVE to `classify`, which sees only the page;
    `delivery_gate._check` asks Greenhouse's API before accepting it.
    """
    from urllib.parse import parse_qs, urlparse

    m = GREENHOUSE_POSTING.match(requested_url or "")
    if not m or not final_url:
        return False
    token, job_id = m.group(1).lower(), m.group(2)
    try:
        p = urlparse(final_url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if host != "greenhouse.io" and not host.endswith(".greenhouse.io"):
        return False
    if job_id in final_url:
        return False
    path = (p.path or "").rstrip("/").lower()
    errored = any(v.strip().lower() == "true"
                  for v in parse_qs(p.query or "").get("error", []))
    return errored or path in (f"/{token}", f"/{token}/jobs")


def _identifier(url: str) -> str:
    """The last path segment that looks like a posting id, lowercased."""
    if not url:
        return ""
    tail = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()
    # A pure word like "jobs" is a section, not an identifier.
    if len(tail) < 4 or tail.isalpha() and len(tail) < 8:
        return ""
    return tail


def is_dead(state: Optional[str]) -> bool:
    """True only when we have positive evidence the vacancy is gone."""
    return state in DEAD_STATES


def record(source: str, external_id: str, state: str, *, reason: str = "",
           http_status: Optional[int] = None, checked_url: str = "") -> None:
    """Store one liveness observation for a posting, shared across tenants.

    An inconclusive result increments a streak instead of overwriting a known
    good state: a board that starts 403-ing every poll should show up as a
    blocked board, not as a pile of dead jobs.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobLiveness

    now = datetime.utcnow()
    try:
        with get_session() as session:
            row = session.exec(
                select(JobLiveness).where(
                    JobLiveness.source == source,
                    JobLiveness.external_id == external_id,
                )
            ).first()
            if row is None:
                row = JobLiveness(source=source, external_id=external_id)
            inconclusive = state in INCONCLUSIVE_STATES
            if inconclusive and row.state in (JobLivenessState.LIVE.value,) + DEAD_STATES:
                # Keep the last conclusive verdict; just count the miss.
                row.inconclusive_streak = (row.inconclusive_streak or 0) + 1
            else:
                row.state = state
                row.inconclusive_streak = (row.inconclusive_streak or 0) + 1 if inconclusive else 0
            row.reason = reason[:120] if reason else None
            row.http_status = http_status
            row.checked_at = now
            if checked_url:
                row.checked_url = checked_url[:500]
            session.add(row)
            session.commit()
    except Exception as e:
        log.debug("liveness.record failed for %s/%s: %s", source, external_id, e)


def record_board_absence(source: str, present_ids: Iterable[str],
                         known_ids: Iterable[str], *, board_complete: bool) -> int:
    """The free liveness signal: a posting missing from a COMPLETE board fetch.

    This costs nothing — the board listing was already fetched by the discovery
    or pulse lane. It is the cheapest possible evidence and it is why the
    per-job HTTP check below only ever has to run for the small set of jobs a
    board fetch could not speak for.

    `board_complete=False` (a truncated or partially-failed fetch) records
    NOTHING: absence from an incomplete listing is not absence.
    """
    if not board_complete:
        return 0
    present = set(present_ids)
    gone = [e for e in known_ids if e not in present]
    record_board_states(source, removed=gone)
    return len(gone)


def record_board_states(source: str, *, removed: Iterable[str] = (),
                        live: Iterable[str] = (), session=None) -> int:
    """Board-level verdicts for many postings at once: REMOVED for postings
    absent from a COMPLETE board fetch, LIVE for postings a complete fetch
    lists again after board absence had closed them.

    Both are conclusive, so — exactly as `record` treats a conclusive state —
    they overwrite whatever the row said. ONE ``INSERT … ON CONFLICT (source,
    external_id) DO UPDATE`` per 200 keys, in sorted key order: no read first,
    and no race with a concurrent writer of the same key (the read-then-insert
    version fell back to a session PER KEY on that race — ~4 round trips each
    on the pulse lane's consumer). With ``session`` the caller commits; without
    one this opens and commits its own.
    """
    wanted: dict = {}
    for ext in removed:
        if ext:
            wanted[str(ext)] = (JobLivenessState.REMOVED.value,
                                "absent_from_complete_board_fetch")
    for ext in live:
        if ext:
            wanted[str(ext)] = (JobLivenessState.LIVE.value,
                                "present_in_complete_board_fetch")
    if not wanted:
        return 0
    if session is not None:
        _upsert_states(session, source, wanted)
        return len(wanted)
    from app.db.init_db import get_session
    try:
        with get_session() as s:
            _upsert_states(s, source, wanted)
            s.commit()
        return len(wanted)
    except Exception as e:
        log.debug("liveness.record_board_states failed for %s: %s", source, e)
        return 0


def _upsert_states(session, source: str, wanted: dict) -> None:
    from app.db.models import JobLiveness

    now = datetime.utcnow()
    keys = sorted(wanted)
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:                                   # no upsert dialect: one row at a time
        for ext in keys:
            state, reason = wanted[ext]
            record(source, ext, state, reason=reason)
        return
    tbl = JobLiveness.__table__
    for start in range(0, len(keys), 200):
        values = [{"source": source, "external_id": ext, "state": wanted[ext][0],
                   "reason": wanted[ext][1], "http_status": None,
                   "inconclusive_streak": 0, "checked_at": now}
                  for ext in keys[start:start + 200]]
        stmt = insert(tbl).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[tbl.c.source, tbl.c.external_id],
            set_={"state": stmt.excluded.state, "reason": stmt.excluded.reason,
                  "http_status": None, "inconclusive_streak": 0,
                  "checked_at": stmt.excluded.checked_at})
        session.execute(stmt)


def needs_check(state: Optional[str], checked_at: Optional[datetime],
                *, max_age_hours: int) -> bool:
    """Should this posting be re-verified before we hand it to a user?

    Never for a posting already known dead (there is nothing to confirm — the
    caller drops it). Always for one never checked. Otherwise on age.
    """
    if max_age_hours <= 0:
        return False
    if is_dead(state):
        return False
    if not checked_at or not state or state == JobLivenessState.UNKNOWN.value:
        return True
    return checked_at < datetime.utcnow() - timedelta(hours=max_age_hours)


def load_states(pairs: list) -> dict:
    """Read liveness for [(source, external_id), …] → {key: (state, checked_at)}."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobLiveness

    if not pairs:
        return {}
    out: dict = {}
    wanted = set(pairs)
    with get_session() as session:
        for start in range(0, len(pairs), 200):
            chunk = pairs[start:start + 200]
            rows = session.exec(
                select(JobLiveness.source, JobLiveness.external_id,
                       JobLiveness.state, JobLiveness.checked_at)
                .where(JobLiveness.source.in_([k[0] for k in chunk]),
                       JobLiveness.external_id.in_([k[1] for k in chunk]))
            ).all()
            for src, ext, state, checked in rows:
                if (src, ext) in wanted:
                    out[(src, ext)] = (state, checked)
    return out
