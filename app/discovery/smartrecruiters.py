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

from app.discovery.base import RawJob

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
        log.info("SmartRecruiters[%s]: found %d total job postings", self.company_slug, len(postings))
        
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
                    continue
                d = dr.json()
            except Exception as e:
                log.debug("SmartRecruiters: failed to fetch details for job %s: %s", posting_id, e)
                continue
                
            # Extract description
            job_ad = d.get("jobAd", {})
            sections = job_ad.get("sections", {})
            desc_parts = []
            for sect_name in ["companyDescription", "jobDescription", "qualifications", "additionalInformation"]:
                sect = sections.get(sect_name, {})
                text = sect.get("text")
                if text:
                    title_text = sect.get("title") or sect_name.capitalize()
                    desc_parts.append(f"### {title_text}\n{_strip_html(text)}")
            description = "\n\n".join(desc_parts)
            
            # Parse location
            loc = p.get("location") or {}
            full_loc = loc.get("fullLocation") or loc.get("city", "")
            remote = loc.get("remote", False) or "remote" in full_loc.lower()
            
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
                    company=p.get("company", {}).get("name") or self.company_slug.replace("-", " ").replace("_", " ").title(),
                    title=title,
                    location=full_loc,
                    remote=remote,
                    url=apply_url,
                    description=description,
                    posted_at=posted_dt,
                )
            )
            
        log.info("SmartRecruiters[%s]: %d tech jobs parsed successfully", self.company_slug, len(jobs))
        return jobs
