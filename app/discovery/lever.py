"""Lever public postings API: https://api.lever.co/v0/postings/{company}?mode=json

Like Greenhouse, this is the same endpoint Lever-powered careers pages use.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import (
    EVIDENCE_ORG_ENTITY,
    EVIDENCE_TEAM_OR_DEPARTMENT,
    RawJob,
)
from app.discovery.hiring_context import put

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
        self.last_error = ""
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as e:
            # See app/discovery/base.py: an empty list with no last_error is a
            # genuinely empty board; one with a last_error is a failed poll.
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("Lever fetch failed for %s: %s", self.company_slug, e)
            return []

        from app.discovery.geo_verify import lever_geo

        jobs: List[RawJob] = []
        for j in r.json():
            cats = j.get("categories") or {}
            location = cats.get("location") or ""
            # EVERY site, the structured `country` code and `workplaceType`
            # (remote / hybrid / on-site). The scraper used to keep only
            # allLocations[0], so a posting listed "Berlin · Austin, TX" was a
            # Berlin posting to the gate and dropped for a US user, and it read
            # "remote" off the commitment field (full-time/part-time) instead
            # of the field Lever provides for it.
            geo = lever_geo(j)
            sites = geo.sites or ([location] if location else [])
            workplace = " · ".join(dict.fromkeys(s for s in sites if s))
            remote = geo.work_mode == "remote" or "remote" in workplace.lower()

            # Lever timestamps are unix ms
            created_at = j.get("createdAt")
            posted_dt = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc) if created_at else None

            # Description: descriptionPlain or strip the HTML one
            desc = j.get("descriptionPlain") or _strip_html(j.get("description", ""))
            lists = j.get("lists", [])
            for lst in lists:
                desc += "\n\n" + (lst.get("text", "") + "\n" + _strip_html(lst.get("content", "")))

            # `categories` already carries the org unit; only location and
            # commitment were ever read out of it.
            ctx: dict = {}
            put(ctx, "team", cats.get("team"), EVIDENCE_TEAM_OR_DEPARTMENT,
                "categories.team")
            put(ctx, "department", cats.get("department"),
                EVIDENCE_TEAM_OR_DEPARTMENT, "categories.department")
            put(ctx, "ats", "lever", EVIDENCE_ORG_ENTITY, "scraper")

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
                    origin="lever",
                    context=ctx,
                    geo=geo if (geo.sites or geo.country or geo.work_mode) else None,
                )
            )
        log.info("Lever[%s]: %d jobs", self.company_slug, len(jobs))
        return jobs
