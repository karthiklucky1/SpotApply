"""SmartRecruiters public postings API: https://api.smartrecruiters.com/v1/companies/{slug}/postings

This retrieves job postings dynamically using the public endpoints careers sites use.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import (
    EVIDENCE_DIRECT_PERSON,
    EVIDENCE_ORG_ENTITY,
    EVIDENCE_STRUCTURED_PERSON,
    EVIDENCE_TEAM_OR_DEPARTMENT,
    EVIDENCE_TITLE_ONLY,
    GeoEvidence,
    RawJob,
)
from app.discovery.hiring_context import put

log = logging.getLogger(__name__)

BASE = "https://api.smartrecruiters.com/v1/companies"

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



def _label(value) -> str:
    """SmartRecruiters returns taxonomy values as {id,label} objects, but some
    tenants return a bare string for the same key. Accept both, reject the
    rest."""
    if isinstance(value, dict):
        return value.get("label") or value.get("name") or ""
    return value if isinstance(value, str) else ""


# Custom-field labels worth reading. Tenants name these freely, so an
# unrecognised label is ignored rather than guessed at.
_CUSTOM_KEYS = {
    "hiring manager": ("reporting_manager_name", EVIDENCE_DIRECT_PERSON),
    "recruiter": ("recruiter_name", EVIDENCE_DIRECT_PERSON),
    "reports to": ("reporting_title", EVIDENCE_TITLE_ONLY),
    "reporting to": ("reporting_title", EVIDENCE_TITLE_ONLY),
    "team": ("team", EVIDENCE_TEAM_OR_DEPARTMENT),
    "division": ("division", EVIDENCE_TEAM_OR_DEPARTMENT),
    "business unit": ("division", EVIDENCE_TEAM_OR_DEPARTMENT),
    "legal entity": ("hiring_entity", EVIDENCE_ORG_ENTITY),
    "requisition id": ("requisition_id", EVIDENCE_ORG_ENTITY),
}


def _creator_name(node) -> str:
    """`creator` is documented as the employee who CREATED the posting. It is
    stored as posting_creator_name and never as a hiring manager. The live
    audit found it populated on 0 of 4 postings, so nothing may depend on it."""
    if not isinstance(node, dict):
        return ""
    name = node.get("name") or " ".join(
        x for x in (node.get("firstName"), node.get("lastName")) if isinstance(x, str))
    return name.strip() if isinstance(name, str) else ""


def _context_for(listing: dict, detail: dict) -> dict:
    """Org unit + requisition + creator, all from responses already fetched."""
    ctx: dict = {}
    for src_name, src in (("detail", detail), ("listing", listing)):
        if not isinstance(src, dict):
            continue
        put(ctx, "department", _label(src.get("department")),
            EVIDENCE_TEAM_OR_DEPARTMENT, f"{src_name}.department.label")
        put(ctx, "division", _label(src.get("function")),
            EVIDENCE_TEAM_OR_DEPARTMENT, f"{src_name}.function.label")
        put(ctx, "requisition_id", src.get("refNumber"),
            EVIDENCE_ORG_ENTITY, f"{src_name}.refNumber")
        put(ctx, "hiring_entity", (src.get("company") or {}).get("name")
            if isinstance(src.get("company"), dict) else None,
            EVIDENCE_ORG_ENTITY, f"{src_name}.company.name")
        put(ctx, "posting_creator_name", _creator_name(src.get("creator")),
            EVIDENCE_STRUCTURED_PERSON, f"{src_name}.creator")
        raw_custom = src.get("customField") or src.get("customFields") or []
        if isinstance(raw_custom, dict):
            raw_custom = [raw_custom]
        for entry in raw_custom:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("fieldLabel") or entry.get("label")
                        or entry.get("fieldId") or "").strip().lower()
            mapped = _CUSTOM_KEYS.get(label)
            if not mapped:
                continue
            target, evidence = mapped
            put(ctx, target, entry.get("valueLabel") or entry.get("value"),
                evidence, f"{src_name}.customField[{label}]")
    put(ctx, "ats", "smartrecruiters", EVIDENCE_ORG_ENTITY, "scraper")
    return ctx


class SmartRecruitersScraper:
    name = "smartrecruiters"

    def __init__(self, company_slug: str):
        self.company_slug = company_slug

    def fetch(self) -> List[RawJob] | None:
        url = f"{BASE}/{self.company_slug}/postings"
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            if r.status_code != 200:
                log.warning("SmartRecruiters fetch postings failed for %s: HTTP %d", self.company_slug, r.status_code)
                return None
        except httpx.HTTPError as e:
            log.warning("SmartRecruiters fetch postings failed for %s: %s", self.company_slug, e)
            return None

        payload = r.json()
        jobs: List[RawJob] = []

        # Iterate over all posting summaries
        postings = payload.get("content", [])
        # The API pages at 100; we request one page, so a bigger board arrives
        # TRUNCATED. Flag it so the pipeline does not ghost-close the postings
        # we simply never saw. Same flag is set below if a detail fetch fails.
        total_found = payload.get("totalFound")
        self.fetch_complete = not (
            isinstance(total_found, int) and total_found > len(postings))
        if not self.fetch_complete:
            log.info("SmartRecruiters[%s]: board truncated (%d of %d) — ghost-close disabled",
                     self.company_slug, len(postings), total_found)
        log.info("SmartRecruiters[%s]: found %d total job postings", self.company_slug, len(postings))

        # LISTING-phase identity of every tech posting, taken from the ONE list
        # response before any detail GETs. The pulse lane hashes these for its
        # poll signature, so a failed detail fetch (which drops the posting
        # from the parsed list below) no longer reads as board change. The list
        # response is a single atomic snapshot, so the entries are stable even
        # when the board is truncated at the API's page size.
        self.signature_entries = [
            (str(p["id"]), p.get("name", ""))
            for p in postings
            if p.get("id") and not _is_obvious_non_tech(p.get("name", ""))
        ]
        self.signature_stable = True

        for p in postings:
            title = p.get("name", "")
            
            # Optimization: skip detail fetch for obvious non-tech jobs
            if _is_obvious_non_tech(title):
                continue
                
            posting_id = p.get("id")
            if not posting_id:
                continue
                
            # Fetch details for this job
            detail_url = f"{BASE}/{self.company_slug}/postings/{posting_id}"
            try:
                dr = httpx.get(detail_url, timeout=15.0)
                if dr.status_code != 200:
                    # Posting is live but missing from the parsed list — the
                    # result is PARTIAL (the exception path below already
                    # flagged this; a 500/404 detail response is the same loss).
                    self.fetch_complete = False
                    continue
                d = dr.json()
            except Exception as e:
                log.debug("SmartRecruiters: failed to fetch details for job %s: %s", posting_id, e)
                # This posting is live but missing from our result — treat the
                # board as partial so it is not ghost-closed on the way out.
                self.fetch_complete = False
                continue

            # Extract description. `or {}` everywhere: these keys can be present
            # and JSON-null, and None.get() would fail the entire board.
            job_ad = d.get("jobAd") or {}
            sections = job_ad.get("sections") or {}
            desc_parts = []
            for sect_name in ["companyDescription", "jobDescription", "qualifications", "additionalInformation"]:
                sect = sections.get(sect_name) or {}
                text = sect.get("text")
                if text:
                    title_text = sect.get("title") or sect_name.capitalize()
                    desc_parts.append(f"### {title_text}\n{_strip_html(text)}")
            description = "\n\n".join(desc_parts)
            
            # Parse location. The DETAIL response carries the same `location`
            # object and is preferred when the listing's is thin.
            loc = p.get("location") or {}
            if not isinstance(loc, dict) or not (loc.get("fullLocation") or loc.get("city")):
                loc = d.get("location") if isinstance(d.get("location"), dict) else (loc or {})
            # `or ""` — city can be JSON-null, and None.lower() crashed the board.
            full_loc = loc.get("fullLocation") or loc.get("city") or ""
            remote = loc.get("remote", False) or "remote" in full_loc.lower()
            # Structured country (ISO-2, "us") and region beside the display
            # string — the gate read only `fullLocation` before.
            country = str(loc.get("country") or "").strip()
            site = full_loc or ", ".join(str(loc.get(k) or "").strip() for k in ("city", "region", "country")
                                         if str(loc.get(k) or "").strip())
            geo = GeoEvidence(
                sites=[site] if site else [], sites_field="location.fullLocation",
                country=country, country_field="location.country" if country else "",
                work_mode="remote" if loc.get("remote") is True else "",
                work_mode_field="location.remote" if loc.get("remote") is True else "",
            ) if (site or country or loc.get("remote") is True) else None
            
            # Parse date
            released = p.get("releasedDate")
            posted_dt = None
            if released:
                try:
                    posted_dt = datetime.fromisoformat(released.replace("Z", "+00:00"))
                except Exception:
                    posted_dt = None
                    
            apply_url = f"https://jobs.smartrecruiters.com/{self.company_slug}/{posting_id}"
            
            jobs.append(
                RawJob(
                    source="smartrecruiters",
                    external_id=str(posting_id),
                    company=(p.get("company") or {}).get("name") or self.company_slug.replace("-", " ").replace("_", " ").title(),
                    title=title,
                    location=full_loc,
                    remote=remote,
                    url=apply_url,
                    description=description,
                    posted_at=posted_dt,
                    origin="smartrecruiters",
                    context=_context_for(p, d),
                    geo=geo,
                )
            )
            
        log.info("SmartRecruiters[%s]: %d tech jobs parsed successfully", self.company_slug, len(jobs))
        return jobs
