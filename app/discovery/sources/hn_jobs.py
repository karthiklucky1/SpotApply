"""Hacker News Jobs source — official HN Firebase API, completely free, no key needed.

HN job postings are from companies paying to advertise on Hacker News.
Quality is high: mostly US tech startups and growth-stage companies.

API: https://hacker-news.firebaseio.com/v0/jobstories.json
Each story: https://hacker-news.firebaseio.com/v0/item/{id}.json

Title format is usually: "Company | Role | Location (Remote) | URL"
We parse this to extract structured fields.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from html import unescape
from typing import List

import httpx

from app.config import settings
from app.discovery.base import RawJob

log = logging.getLogger(__name__)

_JOB_STORIES_URL = "https://hacker-news.firebaseio.com/v0/jobstories.json"
_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"

_US_TERMS = {"remote", "united states", "usa", "u.s.", "san francisco", "new york",
             "seattle", "austin", "boston", "chicago", "los angeles", "denver",
             "sf", "nyc", "la", "dc", "washington", "atlanta", "miami"}


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = unescape(text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _parse_hn_title(title: str) -> tuple[str, str, str]:
    """
    HN titles: "Company | Role | Location | URL"  or  "Company (Role) – Location"
    Returns (company, role, location).
    """
    # Pipe-separated format
    parts = [p.strip() for p in title.split("|")]
    if len(parts) >= 3:
        company = parts[0]
        role = parts[1]
        location = parts[2]
        # Strip any trailing URL
        location = re.sub(r"https?://\S+", "", location).strip()
        return company, role, location

    # Dash/em-dash separated
    m = re.match(r"^(.+?)\s*[–—-]\s*(.+?)\s*[–—-]\s*(.+)$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()

    # Just company and role
    m = re.match(r"^(.+?)\s*[–—|]\s*(.+)$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip(), "Remote"

    return "HN Company", title, "Remote"


def _matches_keywords(role: str, text: str, keywords: list) -> bool:
    role_low = role.lower()
    text_low = (text or "").lower()
    for kw in keywords:
        kw_low = kw.lower()
        if kw_low in role_low or kw_low in text_low[:500]:
            return True
    return False


def _is_us_or_remote(location: str) -> bool:
    low = location.lower()
    return any(t in low for t in _US_TERMS)


class HNJobsSource:
    """Fetches job postings from Hacker News official API."""

    def __init__(self, keywords: List[str] | None = None):
        self.keywords = keywords or settings.jobs_keywords_list

    async def fetch_jobs(self) -> List[RawJob]:
        jobs: List[RawJob] = []
        seen_ids: set[str] = set()
        limit = settings.max_jobs_per_source

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                # Fetch list of latest job story IDs
                r = await client.get(_JOB_STORIES_URL)
                if r.status_code != 200:
                    log.warning("HN Jobs: failed to fetch story list: HTTP %d", r.status_code)
                    return []

                story_ids = r.json()[:min(200, limit * 4)]  # fetch 4x more than limit (many won't match)
                log.info("HN Jobs: fetching details for %d stories", len(story_ids))

                # Fetch items concurrently in batches of 20
                batch_size = 20
                for i in range(0, len(story_ids), batch_size):
                    if len(jobs) >= limit:
                        break
                    batch = story_ids[i:i + batch_size]
                    tasks = [client.get(_ITEM_URL.format(sid)) for sid in batch]

                    import asyncio
                    responses = await asyncio.gather(*tasks, return_exceptions=True)

                    for resp in responses:
                        if len(jobs) >= limit:
                            break
                        if isinstance(resp, Exception):
                            continue
                        try:
                            if resp.status_code != 200:
                                continue
                            item = resp.json()
                            if not item or item.get("type") != "job":
                                continue

                            item_id = str(item.get("id", ""))
                            if item_id in seen_ids:
                                continue

                            title = (item.get("title") or "").strip()
                            text_html = item.get("text") or ""
                            description = _strip_html(text_html)
                            url = (item.get("url") or "").strip()

                            company, role, location = _parse_hn_title(title)

                            # Filter: must match our keywords AND be US/remote
                            if not _matches_keywords(role, description, self.keywords):
                                continue
                            if not _is_us_or_remote(location) and not _is_us_or_remote(description[:300]):
                                continue

                            seen_ids.add(item_id)

                            remote = "remote" in location.lower() or "remote" in description.lower()
                            posted_at: datetime | None = None
                            ts = item.get("time")
                            if ts:
                                try:
                                    posted_at = datetime.utcfromtimestamp(int(ts))
                                except Exception:
                                    pass

                            ext_id = hashlib.md5(f"hn_{item_id}".encode()).hexdigest()

                            jobs.append(RawJob(
                                source="indeed",   # HN jobs go into "indeed" bucket (manual-track)
                                external_id=ext_id,
                                company=company,
                                title=role,
                                location=location or "Remote",
                                remote=remote,
                                url=url or f"https://news.ycombinator.com/item?id={item_id}",
                                description=description or f"HN Job: {title}",
                                posted_at=posted_at,
                            ))
                        except Exception as e:
                            log.debug("HN Jobs: parse error: %s", e)

        except Exception as e:
            log.warning("HN Jobs: fetch failed: %s", e)

        log.info("HNJobsSource: fetched %d matching jobs", len(jobs))
        return jobs
