"""SerpAPI — Google Jobs source.

Google's job aggregation index covers LinkedIn, Indeed, Glassdoor, ZipRecruiter,
and hundreds of company career pages. SerpAPI provides a clean JSON wrapper.

Free tier: 100 searches/month (~1,000 jobs, 10 results per search by default).
Signup: https://serpapi.com  →  Dashboard  →  copy your API key.
No org ID, no credit card required for free tier.
Set SERPAPI_KEY in .env.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import List

import httpx

from app.config import settings
from app.discovery.base import RawJob

log = logging.getLogger(__name__)

_SEARCH_URL = "https://serpapi.com/search.json"

_POSTED_AT_MAP = {
    "just now": 0, "today": 0, "1 day ago": 1, "2 days ago": 2,
    "3 days ago": 3, "4 days ago": 4, "5 days ago": 5,
    "6 days ago": 6, "1 week ago": 7, "2 weeks ago": 14,
    "3 weeks ago": 21, "1 month ago": 30, "2 months ago": 60,
}


def _parse_posted_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    low = raw.lower().strip()
    days = _POSTED_AT_MAP.get(low)
    if days is not None:
        return datetime.utcnow() - timedelta(days=days)
    # "X days ago" fallback
    import re
    m = re.match(r"(\d+)\s+day", low)
    if m:
        return datetime.utcnow() - timedelta(days=int(m.group(1)))
    return None


def _make_id(title: str, company: str, location: str) -> str:
    raw = f"{title}|{company}|{location}".lower()
    return hashlib.md5(raw.encode()).hexdigest()


# Map a free-text country to Google's `gl` (country) code for SerpAPI.
_COUNTRY_GL = {
    "united states": "us", "usa": "us", "us": "us",
    "united kingdom": "gb", "uk": "gb", "england": "gb",
    "canada": "ca", "india": "in", "germany": "de", "france": "fr",
    "spain": "es", "netherlands": "nl", "ireland": "ie", "australia": "au",
    "poland": "pl", "portugal": "pt", "brazil": "br", "mexico": "mx",
    "singapore": "sg", "japan": "jp", "philippines": "ph",
}


class SerpAPISource:
    """Fetches jobs via SerpAPI's Google Jobs engine for each configured keyword."""

    def __init__(self, keywords: List[str] | None = None, country: str | None = None):
        self.keywords = keywords or settings.jobs_keywords_list
        self.country = (country or "United States").strip()
        self._gl = _COUNTRY_GL.get(self.country.lower(), "us")

    async def fetch_jobs(self) -> List[RawJob]:
        if not settings.serpapi_key:
            log.debug("SerpAPI: SERPAPI_KEY not set — skipping")
            return []

        jobs: List[RawJob] = []
        seen_ids: set[str] = set()
        limit = settings.max_jobs_per_source

        async with httpx.AsyncClient(timeout=20.0) as client:
            for kw in self.keywords:
                if len(jobs) >= limit:
                    break
                try:
                    params = {
                        "engine": "google_jobs",
                        "q": f"{kw} {self.country}",
                        "hl": "en",
                        "gl": self._gl,
                        "chips": "date_posted:week",  # last 7 days
                        "api_key": settings.serpapi_key,
                    }
                    r = await client.get(_SEARCH_URL, params=params)

                    if r.status_code == 401:
                        log.warning("SerpAPI: invalid API key — stopping")
                        break
                    if r.status_code == 429:
                        log.warning("SerpAPI: monthly quota reached — stopping")
                        break
                    if r.status_code != 200:
                        log.warning("SerpAPI: HTTP %d for '%s': %s", r.status_code, kw, r.text[:200])
                        continue

                    data = r.json()
                    for item in data.get("jobs_results", []):
                        if len(jobs) >= limit:
                            break
                        try:
                            title = (item.get("title") or "").strip()
                            company = (item.get("company_name") or "Unknown").strip()
                            location = (item.get("location") or self.country).strip()

                            job_id = item.get("job_id") or _make_id(title, company, location)
                            if job_id in seen_ids:
                                continue
                            seen_ids.add(job_id)

                            description = (item.get("description") or "").strip()

                            ext = item.get("detected_extensions") or {}
                            remote = bool(ext.get("work_from_home", False)) or "remote" in location.lower()
                            posted_at = _parse_posted_at(ext.get("posted_at"))

                            # Best apply URL: prefer direct apply link over Google redirect
                            apply_url = ""
                            for opt in (item.get("apply_options") or []):
                                lnk = opt.get("link", "")
                                # Prefer non-Google links
                                if lnk and "google.com" not in lnk:
                                    apply_url = lnk
                                    break
                            if not apply_url:
                                apply_url = (item.get("apply_options") or [{}])[0].get("link", "")

                            # Map publisher to source
                            via = (item.get("via") or "").lower()
                            if "linkedin" in via:
                                source = "linkedin"
                            elif "indeed" in via:
                                source = "indeed"
                            else:
                                source = "serpapi"

                            jobs.append(RawJob(
                                source=source,
                                external_id=job_id,
                                company=company,
                                title=title,
                                location=location,
                                remote=remote,
                                url=apply_url or f"https://www.google.com/search?q={title}+{company}+jobs",
                                description=description,
                                posted_at=posted_at,
                            ))
                        except Exception as e:
                            log.debug("SerpAPI: failed to parse item: %s", e)

                except Exception as e:
                    log.warning("SerpAPI: request failed for '%s': %s", kw, e)

        log.info("SerpAPISource: fetched %d jobs", len(jobs))
        return jobs
