"""Arbeitnow source — free public API, no auth needed.

https://www.arbeitnow.com/api/job-board-api
Returns tech jobs with visa sponsorship info. Good volume, updated daily.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

import httpx

from app.config import settings
from app.discovery.base import GeoEvidence, RawJob
from app.discovery.title_filter import matches_title

log = logging.getLogger(__name__)

_API_URL = "https://www.arbeitnow.com/api/job-board-api"
_MAX_PAGES = 5  # 100 results/page → up to ~500 candidates


class ArbeitnowSource:
    """Fetches jobs from the Arbeitnow public API, filtered to keywords."""

    def __init__(self, keywords: List[str] | None = None):
        self.keywords = [k.lower() for k in (keywords or settings.jobs_keywords_list)]

    async def fetch_jobs(self) -> List[RawJob]:
        if not settings.arbeitnow_enabled:
            return []

        jobs: List[RawJob] = []
        seen: set[str] = set()
        limit = settings.max_jobs_per_source

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for page in range(1, _MAX_PAGES + 1):
                    if len(jobs) >= limit:
                        break
                    r = await client.get(_API_URL, params={"page": page})
                    if r.status_code == 429:
                        log.warning("Arbeitnow: rate-limited — stopping")
                        break
                    if r.status_code != 200:
                        log.warning("Arbeitnow: HTTP %d on page %d", r.status_code, page)
                        break

                    data = r.json()
                    results = data.get("data", [])
                    if not results:
                        break

                    for item in results:
                        if len(jobs) >= limit:
                            break
                        try:
                            slug = item.get("slug") or ""
                            if not slug or slug in seen:
                                continue
                            title = (item.get("title") or "").strip()
                            if not matches_title(title, self.keywords):
                                continue
                            seen.add(slug)

                            # No location from the board is "not stated", not
                            # "Remote": the old default fabricated a work mode.
                            stated = (item.get("location") or "").strip()
                            remote_flag = bool(item.get("remote", False))
                            location = stated or ("Remote" if remote_flag else "")
                            remote = remote_flag or "remote" in location.lower()
                            geo = GeoEvidence(
                                sites=[stated] if stated else [], sites_field="location" if stated else "",
                                work_mode="remote" if remote_flag else "",
                                work_mode_field="remote" if remote_flag else "",
                            ) if (stated or remote_flag) else None

                            posted_at: datetime | None = None
                            ts = item.get("created_at")
                            if ts:
                                try:
                                    posted_at = datetime.utcfromtimestamp(int(ts))
                                except Exception:
                                    pass

                            jobs.append(RawJob(
                                source="arbeitnow",
                                external_id=slug,
                                company=(item.get("company_name") or "Unknown").strip(),
                                title=title,
                                location=location,
                                remote=remote,
                                url=(item.get("url") or "").strip(),
                                description=(item.get("description") or "").strip(),
                                posted_at=posted_at,
                                origin="arbeitnow",
                                geo=geo,
                            ))
                        except Exception as e:
                            log.debug("Arbeitnow: parse failed for %s: %s", item.get("slug"), e)

                    if not data.get("links", {}).get("next"):
                        break

        except Exception as e:
            log.warning("Arbeitnow: fetch failed: %s", e)

        log.info("ArbeitnowSource: fetched %d jobs", len(jobs))
        return jobs
