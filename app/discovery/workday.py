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
    GeoEvidence,
    RawJob,
)
from app.discovery.job_identity import scoped_external_id
from app.discovery.hiring_context import put

log = logging.getLogger(__name__)

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
                r = httpx.post(url, json=payload, headers=headers, timeout=30.0)
                if r.status_code != 200:
                    log.warning("Workday fetch failed for %s: HTTP %d", tenant, r.status_code)
                    # If offset is 0, this is a fatal run error
                    self.fetch_complete = False
                    self.signature_stable = False
                    return None if offset == 0 else jobs
                    
                data = r.json()
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
                        capped = True
                        break
                    # Stable listing identity, recorded whether or not the
                    # detail fetch below succeeds.
                    self.signature_entries.append((str(ext_path), title))

                    # Fetch details
                    path_suffix = ext_path if ext_path.startswith("/job") else f"/job{ext_path}"
                    detail_url = f"https://{domain}/wday/cxs/{tenant}/{site}{path_suffix}"
                    try:
                        dr = httpx.get(detail_url, headers=headers, timeout=15.0)
                        if dr.status_code != 200:
                            # Posting is live but missing from the parsed list —
                            # the result is PARTIAL (SmartRecruiters already
                            # flags this; Workday silently didn't, so a board
                            # with one flaky detail endpoint could ghost-close
                            # live postings in the fresh/full lanes).
                            self.fetch_complete = False
                            continue
                        detail_data = dr.json()
                    except Exception as e:
                        log.debug("Workday: failed to fetch job details for %s: %s", ext_path, e)
                        self.fetch_complete = False
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
                            
                    apply_url = f"https://{domain}/{site}{ext_path}"
                    
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

                    jobs.append(
                        RawJob(
                            source="workday",
                            external_id=scoped_external_id(
                                "workday", tenant, req_id),
                            company=tenant.replace("-", " ").replace("_", " ").title(),
                            title=title,
                            location=location,
                            remote=remote,
                            url=apply_url,
                            description=description,
                            posted_at=posted_dt,
                            origin="workday",
                            context=ctx,
                            geo=geo,
                        )
                    )
                    
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
            return None if offset == 0 else jobs

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

        log.info("Workday[%s]: %d tech jobs parsed successfully", tenant, len(jobs))
        return jobs
