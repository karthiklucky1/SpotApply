"""Lever public postings API: https://api.lever.co/v0/postings/{company}?mode=json

Like Greenhouse, this is the same endpoint Lever-powered careers pages use.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import RawJob

log = logging.getLogger(__name__)

BASE = "https://api.lever.co/v0/postings"


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


class LeverScraper:
    name = "lever"

    def __init__(self, company_slug: str):
        self.company_slug = company_slug

    def fetch(self) -> List[RawJob]:
        url = f"{BASE}/{self.company_slug}?mode=json"
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Lever fetch failed for %s: %s", self.company_slug, e)
            return []

        jobs: List[RawJob] = []
        for j in r.json():
            cats = j.get("categories") or {}
            location = cats.get("location") or ""
            commitment = cats.get("commitment", "")
            workplace = (cats.get("allLocations") or [location])[0] if cats.get("allLocations") else location
            remote = "remote" in (workplace + " " + commitment).lower()

            # Lever timestamps are unix ms
            created_at = j.get("createdAt")
            posted_dt = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc) if created_at else None

            # Description: descriptionPlain or strip the HTML one
            desc = j.get("descriptionPlain") or _strip_html(j.get("description", ""))
            lists = j.get("lists", [])
            for lst in lists:
                desc += "\n\n" + (lst.get("text", "") + "\n" + _strip_html(lst.get("content", "")))

            jobs.append(
                RawJob(
                    source="lever",
                    external_id=j["id"],
                    company=self.company_slug.replace("-", " ").replace("_", " ").title(),
                    title=j.get("text", ""),
                    location=workplace,
                    remote=remote,
                    url=j.get("hostedUrl", ""),
                    description=desc.strip(),
                    posted_at=posted_dt,
                )
            )
        log.info("Lever[%s]: %d jobs", self.company_slug, len(jobs))
        return jobs
