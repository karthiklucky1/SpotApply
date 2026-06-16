"""RemoteOK job source — free public API, no key needed.

https://remoteok.com/api
All jobs are remote. Strong AI/ML/Python/backend coverage.
US-based companies dominate the listings.
Provides salary ranges when available.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

import httpx

from app.config import settings
from app.discovery.base import RawJob

log = logging.getLogger(__name__)

_API_URL = "https://remoteok.com/api"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0; +personal-job-search-bot)"
}

_TARGET_TAGS = {
    "python", "machine learning", "ml", "ai", "llm", "nlp", "deep learning",
    "data science", "backend", "fastapi", "django", "flask", "pytorch",
    "tensorflow", "scikit-learn", "data engineering", "mlops", "devops",
    "golang", "rust", "java", "scala", "spark", "airflow", "kubernetes",
    "aws", "gcp", "azure", "cloud", "api", "engineer", "developer",
}


def _matches(position: str, tags: list) -> bool:
    pos_low = position.lower()
    tags_low = {t.lower() for t in tags}
    kw_list = [k.lower() for k in settings.jobs_keywords_list]
    for kw in kw_list:
        if kw in pos_low:
            return True
    return bool(tags_low & _TARGET_TAGS)


class RemoteOKSource:
    """Fetches remote tech jobs from RemoteOK's public API."""

    def __init__(self, keywords: List[str] | None = None):
        self.keywords = keywords or settings.jobs_keywords_list

    async def fetch_jobs(self) -> List[RawJob]:
        jobs: List[RawJob] = []
        seen_ids: set[str] = set()
        limit = settings.max_jobs_per_source

        try:
            async with httpx.AsyncClient(headers=_HEADERS, timeout=20.0) as client:
                r = await client.get(_API_URL)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} (likely Cloudflare/bot block)")
                try:
                    data = r.json()
                except Exception:
                    raise RuntimeError("non-JSON response (likely Cloudflare/HTML block)")
                # First item is a legal/metadata object — skip it
                for item in data[1:]:
                    if len(jobs) >= limit:
                        break
                    try:
                        job_id = str(item.get("id") or item.get("slug") or "")
                        if not job_id or job_id in seen_ids:
                            continue

                        position = (item.get("position") or "").strip()
                        tags = item.get("tags") or []

                        if not _matches(position, tags):
                            continue

                        seen_ids.add(job_id)

                        company = (item.get("company") or "Unknown").strip()
                        description = (item.get("description") or "").strip()
                        apply_url = (item.get("apply_url") or item.get("url") or "").strip()

                        posted_at: datetime | None = None
                        epoch = item.get("epoch")
                        if epoch:
                            try:
                                posted_at = datetime.utcfromtimestamp(int(epoch))
                            except Exception:
                                pass

                        # Enrich description with salary if available
                        sal_min = item.get("salary_min")
                        sal_max = item.get("salary_max")
                        if sal_min and sal_max:
                            description = f"Salary: ${sal_min:,}–${sal_max:,}/yr\n\n{description}"
                        elif sal_min:
                            description = f"Salary: ${sal_min:,}+/yr\n\n{description}"

                        jobs.append(RawJob(
                            source="remotive",  # reuse remotive bucket (both are remote-only boards)
                            external_id=f"rok_{job_id}",
                            company=company,
                            title=position,
                            location="Remote",
                            remote=True,
                            url=apply_url or f"https://remoteok.com/remote-jobs/{job_id}",
                            description=description,
                            posted_at=posted_at,
                        ))
                    except Exception as e:
                        log.debug("RemoteOK: failed to parse item: %s", e)

        except Exception as e:
            # Re-raise so the discovery run summary records *why* RemoteOK
            # returned nothing (e.g. a Cloudflare block) instead of a silent 0.
            log.warning("RemoteOK: fetch failed: %s", e)
            raise

        log.info("RemoteOKSource: fetched %d jobs", len(jobs))
        return jobs
