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

from app.discovery.base import RawJob

log = logging.getLogger(__name__)

# Lightweight tech title filter to avoid fetching details for obvious non-tech jobs
_TECH_TITLE_RE = re.compile(
    r'\b(engineer|scientist|developer|researcher|architect|analyst|'
    r'mlops|devops|sre|quantitative|quant|statistician|'
    r'programmer|technologist|intelligence|nlp|llm|'
    r'platform|infrastructure|backend|fullstack|full[\-\s]stack|frontend|front[\-\s]stack|'
    r'machine\s*learning|deep\s*learning|computer\s*vision|data|technical|member\s+of\s+technical\s+staff)\b',
    re.IGNORECASE,
)

_NON_TECH_TITLE_RE = re.compile(
    r'\b(sales|marketing|recruiter|hr|talent\s+acquisition|people\s+ops|'
    r'finance|accountant|accounting|payroll|billing|auditor|'
    r'legal|counsel|lawyer|compliance|'
    r'receptionist|administrative|assistant|secretary|office\s+manager|'
    r'customer\s+support|customer\s+success|sales\s+rep|account\s+exec|'
    r'copywriter|content\s+writer|editor|translator|'
    r'nurse|doctor|medical|therapist|chef|cook|driver|cashier|'
    r'facilities|janitor|security\s+guard|maintenance)\b',
    re.IGNORECASE,
)

def _is_obvious_non_tech(title: str) -> bool:
    if _NON_TECH_TITLE_RE.search(title):
        if _TECH_TITLE_RE.search(title):
            return False
        return True
    return False

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
                if not postings:
                    break
                    
                for p in postings:
                    title = p.get("title", "")
                    if _is_obvious_non_tech(title):
                        continue
                        
                    ext_path = p.get("externalPath")
                    if not ext_path:
                        continue

                    if len(self.signature_entries) >= max_total:
                        # Truncated at the cap — the board may hold more.
                        self.fetch_complete = False
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
                    
                    posted = info.get("startDate")
                    posted_dt = None
                    if posted:
                        try:
                            posted_dt = datetime.strptime(posted, "%Y-%m-%d")
                        except Exception:
                            posted_dt = None
                            
                    apply_url = f"https://{domain}/{site}{ext_path}"
                    
                    jobs.append(
                        RawJob(
                            source="workday",
                            external_id=str(req_id),
                            company=tenant.replace("-", " ").replace("_", " ").title(),
                            title=title,
                            location=location,
                            remote=remote,
                            url=apply_url,
                            description=description,
                            posted_at=posted_dt,
                        )
                    )
                    
                # Next page
                total = data.get("total", 0)
                offset += limit
                if offset >= total:
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
        if len(self.signature_entries) >= max_total and offset < total:
            self.fetch_complete = False

        log.info("Workday[%s]: %d tech jobs parsed successfully", tenant, len(jobs))
        return jobs
