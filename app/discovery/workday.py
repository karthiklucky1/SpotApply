"""Workday public CXS (Career Site External) API scraper.

Bypasses browser automation by hitting the JSON API directly.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Tuple
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import (
    EVIDENCE_ORG_ENTITY,
    BoardBlockedError,
    BoardNotSentError,
    BoardUnreadableError,
    GeoEvidence,
    RawJob,
    body_hint,
    unchanged_since_ingest,
)
from app.discovery import detail_memo
from app.discovery.job_identity import scoped_external_id
from app.discovery.pacing import Pace
from app.discovery.hiring_context import put

log = logging.getLogger(__name__)


def _setting(name: str, default: float) -> float:
    try:
        from app.config import settings
        return float(getattr(settings, name, default) or 0.0)
    except Exception:
        return default


# ONE pace for every Workday request this process sends, whichever tenant it
# is for (app/discovery/pacing.py). Workday limits by address ACROSS tenants:
# spacing the requests of one poll a second apart still left 1,143 of 1,440
# polls refused on their FIRST request (2026-11-21), because dozens of tenants
# are polled at once. A live check the day before got six requests through in
# five seconds and was then refused for the rest of the session.
_PACE = Pace(lambda: _setting("workday_request_spacing_seconds", 2.0),
             lambda: _setting("workday_max_pace_wait_seconds", 12.0))


# How much longer a poll that has already asked this board something will wait
# for its next slot than a poll that has not started (pacing.Pace.take).
_STARTED_POLL_PATIENCE = 3.0


def _take_turn(started: bool = False) -> bool:
    """Wait for the next Workday slot. False = the queue is too long: the
    request is not sent. ``started``: this poll already spent a request."""
    return _PACE.take(_STARTED_POLL_PATIENCE if started else 1.0)


def _refused() -> None:
    """Workday answered 403/429: nobody asks again for a while."""
    _PACE.cool_down(_setting("workday_refusal_cooldown_seconds", 180.0))


# Skip the detail fetch for postings nobody on the platform wants. The gate
# reads the published demand (every user's target roles), so a nurse or
# accountant posting is kept once someone is looking for one.
from app.discovery.title_filter import is_obvious_non_tech as _is_obvious_non_tech  # noqa: E402


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def parse_workday_url(career_url: str | None, slug: str) -> Tuple[str, str, str]:
    """Parse career URL to extract domain, tenant, and site.
    Fallback to slug if career_url is not a workday URL.
    """
    if not career_url:
        # Fallback logic
        if "." in slug:
            tenant = slug.split(".")[0]
            domain = f"{slug}.myworkdayjobs.com"
        else:
            tenant = slug
            domain = f"{slug}.myworkdayjobs.com"
        return domain, tenant, "External"
        
    parsed = urlparse(career_url)
    hostname = parsed.hostname or f"{slug}.myworkdayjobs.com"
    
    # Extract tenant from domain (first segment before .myworkdayjobs or .wdX)
    tenant = hostname.split(".")[0]
    
    # Extract site from path
    path_parts = [p for p in parsed.path.split("/") if p]
    site = "External"
    for part in path_parts:
        if part.lower() in ["jobs", "job", "login", "wday"]:
            continue
        # Skip language codes (e.g., en-US)
        if re.match(r"^[a-z]{2}-[A-Z]{2}$", part) or re.match(r"^[a-z]{2}$", part):
            continue
        site = part
        break
        
    return hostname, tenant, site


class WorkdayScraper:
    name = "workday"

    def __init__(self, company_slug: str, career_url: str | None = None):
        self.company_slug = company_slug
        self.career_url = career_url

    def fetch(self) -> List[RawJob] | None:
        domain, tenant, site = parse_workday_url(self.career_url, self.company_slug)
        url = f"https://{domain}/wday/cxs/{tenant}/{site}/jobs"
        
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        jobs: List[RawJob] = []
        # "" = the fetch succeeded. A first page that fails returns None WITH a
        # reason here (or raises): `None` alone reached the pulse lane as a
        # failure with an EMPTY error — not recognisable as a throttle, a
        # refusal or an outage — so every such poll was a strike, and on
        # 2026-11-14 that retired 1,329 boards holding 30,812 open jobs as
        # "unreachable x5" in ten hours with nothing recorded about why.
        self.last_error = ""
        offset = 0
        limit = 20
        total = 0   # source-reported posting count; set per fetched page
        # The LARGEST total any page reported. Most tenants send `total` on the
        # first page only (0 afterwards), so the per-page `total` ends the walk
        # at page 2 — kept as is: walking a 250-posting board to its end costs
        # ~2.5x the requests on every poll. What changes is the CLAIM: a walk
        # that stopped short of the size the board stated is partial.
        reported_total = 0
        max_total = 100  # Cap postings considered per company run to avoid timeouts
        # A PARTIAL result must never be treated as "the whole board". The
        # pipeline ghost-closes every stored job missing from a fetch, so a
        # mid-pagination failure (or hitting max_total on a big board) would
        # permanently close live postings and SKIP their applications.
        self.fetch_complete = True
        # The LISTING alone: False only when a page failed or the cap truncated
        # the walk — a failed detail GET leaves it True. The pulse lane's
        # board-absence close reads THIS, because absence is a listing question
        # and detail-fetch luck must not decide what gets closed.
        self.listing_complete = True
        # LISTING-phase identity of every tech posting considered, collected
        # BEFORE the per-posting detail GETs. The pulse lane hashes THESE for
        # its poll signature: the parsed job list shrinks with every failed
        # detail fetch, and that jitter measured as 93% of all changed-board
        # events in production (32.6% per-poll change rate on Workday vs ≤1.4%
        # everywhere else) — pagination noise billed as change, every flip
        # paying the full upsert cost. The cap below counts LISTINGS, not
        # parsed jobs, for the same reason: a cap on parsed jobs makes the
        # number of pages consumed depend on detail-fetch luck.
        self.signature_entries: list[tuple[str, str]] = []
        # False when the pagination itself died mid-way: the entry list then
        # varies with WHERE it died, and a volatile signature must never be
        # stored as the board's baseline.
        self.signature_stable = True
        # Every posting the LISTING carried (non-tech skips included), as the
        # posting URL this adapter stores — the req id that becomes the
        # external_id needs the detail fetch, the URL does not. The full pass's
        # ghost-close (`pipeline.mark_ghost_jobs`) counts these as present, so
        # a skipped title never reads as "gone". (Workday is NOT closed on
        # pulse-lane board absence: board_absence.CLOSING_SOURCES.)
        # `listing_complete` says the walk covered the whole board even when a
        # detail fetch failed; `fetch_complete` additionally needs every detail.
        self.listed_urls: set[str] = set()
        # Tech postings the listing named, in order: (posting, title, path).
        pending: list = []
        self.listing_complete = False
        walk_finished = False
        capped = False

        try:
            while len(self.signature_entries) < max_total:
                payload = {
                    "appliedFacets": {},
                    "limit": limit,
                    "offset": offset,
                    "searchText": ""
                }
                if not _take_turn(started=offset > 0):
                    # Our own pace is full. The first page: nothing was sent,
                    # the board is retried in an hour. A later page: the walk
                    # ends here and what the earlier pages listed is kept.
                    self.fetch_complete = False
                    self.listing_complete = False
                    self.signature_stable = False
                    if offset == 0:
                        raise BoardNotSentError("workday")
                    break
                r = httpx.post(url, json=payload, headers=headers, timeout=30.0)
                if r.status_code != 200:
                    log.warning("Workday fetch failed for %s: HTTP %d", tenant, r.status_code)
                    # If offset is 0, this is a fatal run error
                    self.fetch_complete = False
                    self.listing_complete = False
                    self.signature_stable = False
                    if offset != 0:
                        if r.status_code in (403, 429):
                            _refused()
                        break       # keep what the earlier pages listed
                    if r.status_code in (403, 429):
                        # Refused or throttled: backed off, and a strike only
                        # when the rest of Workday is answering (base.py).
                        _refused()
                        raise BoardBlockedError("workday", r.status_code)
                    # Not-found is spelled without the status on purpose: the
                    # recorders retire a board on the spot for one, and a
                    # tenant whose site path moved gets the usual five tries.
                    self.last_error = ("workday listing not found (site path moved?)"
                                       if r.status_code in (404, 410)
                                       else f"workday listing returned {r.status_code}")
                    return None

                try:
                    data = r.json()
                except ValueError as e:
                    if offset != 0:
                        self.fetch_complete = False
                        self.listing_complete = False
                        self.signature_stable = False
                        break   # keep what the earlier pages listed
                    raise BoardUnreadableError(
                        "workday", f"listing did not send JSON: {body_hint(r.text)}") from e
                postings = data.get("jobPostings", [])
                try:
                    reported_total = max(reported_total, int(data.get("total") or 0))
                except (TypeError, ValueError):
                    pass                  # unreadable: the size stays unknown
                if not postings:
                    # An empty page ends the walk; it only ends the BOARD when
                    # the walk already covered what the board said it holds.
                    walk_finished = offset >= reported_total
                    break

                for p in postings:
                    title = p.get("title", "")
                    _path = p.get("externalPath")
                    if _path:
                        self.listed_urls.add(f"https://{domain}/{site}{_path}")
                    if _is_obvious_non_tech(title):
                        continue

                    ext_path = p.get("externalPath")
                    if not ext_path:
                        continue

                    if len(self.signature_entries) >= max_total:
                        # Truncated at the cap — the board may hold more.
                        self.fetch_complete = False
                        self.listing_complete = False
                        capped = True
                        break
                    # Stable listing identity, recorded whether or not the
                    # detail fetch below succeeds.
                    self.signature_entries.append((str(ext_path), title))

                    # Its detail is fetched AFTER the walk, and only if the
                    # listing differs from the one already ingested.
                    pending.append((p, title, ext_path))

                # Next page — the walk's depth is unchanged (per-page total).
                try:
                    total = int(data.get("total") or 0)
                except (TypeError, ValueError):
                    total = 0
                offset += limit
                if offset >= total:
                    # The board said nothing more on this page. Finished only
                    # if the walk covered every posting the board ever said it
                    # holds — or, when it never said, the page came back short.
                    walk_finished = (offset >= reported_total if reported_total > 0
                                     else len(postings) < limit)
                    break
                    
        except httpx.HTTPError as e:
            log.warning("Workday connection failed for %s: %s", tenant, e)
            self.fetch_complete = False
            self.signature_stable = False
            if offset == 0:
                self.last_error = f"workday unreachable: {type(e).__name__}: {e}"[:300]
                return None
            # A later page: the walk ends here, and the postings the earlier
            # pages listed still get their details below.

        # Truncation can land exactly on a page boundary (max_total is a
        # multiple of the page limit, so on an all-tech board it always does):
        # the loop then exits via its while condition without ever reaching the
        # in-loop cap check, and the flag would be lost — letting ghost-close
        # treat the first max_total postings as the whole board. If the source
        # reported more postings than the pages we consumed, the result is
        # partial, full stop.
        if len(self.signature_entries) >= max_total and offset < max(total, reported_total):
            self.fetch_complete = False

        # The LISTING is whole only when the walk reached the end on its own
        # (not the cap, not an error) and it named at least as many postings
        # as the board says it has — a board that changed mid-walk, or a page
        # that came back short, fails the count and is treated as partial.
        self.listing_complete = bool(
            walk_finished and not capped and self.signature_stable
            and len(self.listed_urls) >= reported_total)
        if not self.listing_complete:
            self.fetch_complete = False

        if unchanged_since_ingest(self):
            # The same postings as the poll we already ingested in full. One
            # Workday poll used to be the listing PLUS a detail request per
            # posting — up to a hundred — whether or not anything had changed,
            # and within a day of its refusals being recorded 2,105 of 2,407
            # boards had been answered 429. The list returned is NOT the board
            # (the lane reads the signature, sees no change and does no work).
            self.fetch_complete = False
            log.info("Workday[%s]: listing unchanged (%d postings) — no detail requests",
                     tenant, len(self.signature_entries))
            return []

        # A posting's detail is fetched ONCE (app/discovery/detail_memo.py). The
        # listing names a posting by its path, and the id its row is stored
        # under is on the detail — so a known posting is recognised by its URL.
        poll = detail_memo.Poll("workday")
        company = tenant.replace("-", " ").replace("_", " ").title()
        for p, title, ext_path in pending:
            apply_url = f"https://{domain}/{site}{ext_path}"
            listed_at = p.get("locationsText")
            listed_at = listed_at.strip() if isinstance(listed_at, str) else ""
            fp = None
            if poll.on:
                fp = detail_memo.fingerprint(title, listed_at)
                held_as = detail_memo.alias("workday", apply_url)
                if held_as and not detail_memo.needs_detail("workday", held_as, fp):
                    # The pool holds it: a re-sighting, no request.
                    jobs.append(RawJob(
                        source="workday", external_id=held_as, company=company,
                        title=title, location=listed_at,
                        remote="remote" in listed_at.lower(), url=apply_url,
                        description="", origin="workday", listing_only=True))
                    continue
                if poll.off or poll.n_fetched >= poll.cap or not detail_memo.take("workday"):
                    # No budget this poll: not emitted, back on the next one.
                    poll.n_skipped += 1
                    self.fetch_complete = False
                    continue
            if not _take_turn(started=True):
                # Our own pace is full: no more detail requests this poll.
                poll.failed(stop=True)
                self.fetch_complete = False
                break
            # Fetch details
            path_suffix = ext_path if ext_path.startswith("/job") else f"/job{ext_path}"
            detail_url = f"https://{domain}/wday/cxs/{tenant}/{site}{path_suffix}"
            try:
                dr = httpx.get(detail_url, headers=headers, timeout=15.0)
                if dr.status_code in (403, 429):
                    # Told to stop: the next 99 detail requests are how a
                    # throttle becomes a block. What was parsed is kept, the
                    # result is partial, and the rest comes on a later poll.
                    self.fetch_complete = False
                    log.info("Workday[%s]: detail request refused (%d) — stopping this poll",
                             tenant, dr.status_code)
                    poll.failed(stop=True)
                    _refused()
                    break
                if dr.status_code != 200:
                    # Posting is live but missing from the parsed list —
                    # the result is PARTIAL (SmartRecruiters already
                    # flags this; Workday silently didn't, so a board
                    # with one flaky detail endpoint could ghost-close
                    # live postings in the fresh/full lanes).
                    self.fetch_complete = False
                    poll.n_skipped += 1
                    continue
                detail_data = dr.json()
            except Exception as e:
                log.debug("Workday: failed to fetch job details for %s: %s", ext_path, e)
                self.fetch_complete = False
                poll.n_skipped += 1
                continue

            info = detail_data.get("jobPostingInfo", {})
            description = _strip_html(info.get("jobDescription", ""))

            # `or [None]` — bulletFields can come back as an EMPTY list
            # (not just missing), and [] [0] raised IndexError, failing
            # the whole board over one malformed posting.
            req_id = info.get("jobReqId") or (p.get("bulletFields") or [None])[0] or ext_path.split("_")[-1]
            location = info.get("location") or p.get("locationsText") or ""  # coerce null → ""
            remote = "remote" in location.lower()
            # Both fields, not one-or-the-other: `locationsText` names
            # EVERY site of a multi-site req ("2 Locations" boards list
            # them here), and the detail's `location` is the primary.
            # `remoteType`, when a tenant emits it, is the work mode.
            sites = []
            for v in (info.get("location"), p.get("locationsText"),
                      info.get("additionalLocations")):
                for part in (v if isinstance(v, list) else [v]):
                    if isinstance(part, str) and part.strip() and part.strip() not in sites:
                        sites.append(part.strip())
            rt = str(info.get("remoteType") or p.get("remoteType") or "").strip().lower()
            work_mode = ("remote" if "remote" in rt and "hybrid" not in rt else "hybrid" if "hybrid" in rt
                         else "onsite" if ("site" in rt or "office" in rt) else "")
            geo = GeoEvidence(
                sites=sites, sites_field="jobPostingInfo.location+locationsText",
                work_mode=work_mode, work_mode_field="remoteType" if work_mode else "",
            ) if (sites or work_mode) else None

            posted = info.get("startDate")
            posted_dt = None
            if posted:
                try:
                    posted_dt = datetime.strptime(posted, "%Y-%m-%d")
                except Exception:
                    posted_dt = None

            # `hiringOrganization` is documented for schema.org job
            # postings but was NOT confirmed present in a live CXS
            # response, so it is read defensively and simply absent
            # when the tenant does not send it. Nothing depends on it.
            ctx: dict = {}
            hiring_org = info.get("hiringOrganization")
            if isinstance(hiring_org, dict):
                hiring_org = hiring_org.get("name")
            put(ctx, "hiring_entity", hiring_org, EVIDENCE_ORG_ENTITY,
                "jobPostingInfo.hiringOrganization.name")
            # The requisition is kept as its own evidenced field
            # because `external_id` is no longer the bare req id: a
            # Workday req id is unique per TENANT, so it is qualified
            # (job_identity.scoped_external_id) to stop two employers
            # sharing `R29845` from merging into one posting.
            put(ctx, "requisition_id", info.get("jobReqId"),
                EVIDENCE_ORG_ENTITY, "jobPostingInfo.jobReqId")
            put(ctx, "ats", "workday", EVIDENCE_ORG_ENTITY, "scraper")

            stored_as = scoped_external_id("workday", tenant, req_id)
            if poll.on:
                poll.fetched()
                # Which id this URL is stored under; whether the pool HOLDS
                # it is recorded by the upsert, not here.
                detail_memo.set_alias("workday", apply_url, stored_as)
            jobs.append(
                RawJob(
                    source="workday",
                    # (== stored_as; spelled out because this call is the
                    # thing test_job_identity guards)
                    external_id=scoped_external_id(
                        "workday", tenant, req_id),
                    company=company,
                    title=title,
                    location=location,
                    remote=remote,
                    url=apply_url,
                    description=description,
                    posted_at=posted_dt,
                    origin="workday",
                    context=ctx,
                    geo=geo,
                    listing_fp=fp,
                )
            )


        # A detail the pool does not hold was fetched: ingest this result even
        # if the listing reads unchanged.
        self.ingest_required = poll.ingest_required
        log.info("Workday[%s]: %d tech jobs (%d details fetched, %d waiting)",
                 tenant, len(jobs), poll.n_fetched, poll.n_skipped)
        return jobs
