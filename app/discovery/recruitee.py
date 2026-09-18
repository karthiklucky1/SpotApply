"""Recruitee public offers API: https://{slug}.recruitee.com/api/offers/

The JSON feed behind every Recruitee-hosted careers site. No auth required.
Recruitee is heavily used by European companies — good coverage for non-US
users.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import (
    EVIDENCE_ORG_ENTITY,
    EVIDENCE_TEAM_OR_DEPARTMENT,
    GeoEvidence,
    RawJob,
)
from app.discovery.hiring_context import put

log = logging.getLogger(__name__)


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


class RecruiteeScraper:
    name = "recruitee"

    def __init__(self, board_slug: str):
        self.board_slug = board_slug

    def fetch(self) -> List[RawJob]:
        url = f"https://{self.board_slug}.recruitee.com/api/offers/"
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Permanent statuses mean the slug is wrong, gone, or private — let
            # the exception propagate so the discovery pipeline's dead-board
            # recorder retires the registry row (these long-tail ATSes are NOT
            # covered by the Greenhouse/Lever/Ashby validation loop, so a junk
            # slug would otherwise 404 on every cycle forever).
            if e.response is not None and e.response.status_code in (401, 403, 404, 410):
                raise
            log.warning("Recruitee fetch failed for %s: %s", self.board_slug, e)
            return []
        except httpx.HTTPError as e:
            log.warning("Recruitee fetch failed for %s: %s", self.board_slug, e)
            return []

        payload = r.json()
        jobs: List[RawJob] = []
        for j in payload.get("offers", []):
            ext_id = str(j.get("id") or "").strip()
            if not ext_id:
                continue
            if (j.get("status") or "").lower() not in ("", "published", "open"):
                continue
            location = (j.get("location") or "").strip()
            city = (j.get("city") or "").strip()
            country = (j.get("country") or "").strip()
            country_code = (j.get("country_code") or "").strip()
            if not location:
                location = ", ".join(p for p in (city, country) if p)
            # The offer carries three workplace booleans; read them rather than
            # guessing from the location string.
            work_mode = ("hybrid" if j.get("hybrid") else "remote" if j.get("remote")
                         else "onsite" if j.get("on_site") else "")
            remote = work_mode == "remote" or "remote" in location.lower()
            geo = GeoEvidence(
                sites=[location] if location else [], sites_field="location",
                country=country_code or country,
                country_field=("country_code" if country_code else "country") if (country_code or country) else "",
                work_mode=work_mode, work_mode_field="remote/hybrid/on_site" if work_mode else "",
            ) if (location or country or country_code or work_mode) else None
            posted_dt = None
            published = j.get("published_at") or j.get("created_at")
            if published:
                try:
                    posted_dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                except ValueError:
                    pass
            desc = " ".join(_strip_html(j.get(f) or "") for f in ("description", "requirements"))
            # Recruitee returns ~50 fields per offer; only 14 were ever read.
            # `department` is a plain string on this endpoint. Tags are a free
            # -text list, so they are NOT treated as an org unit — a tag is not
            # a team, and mislabelling one would put a guess on the card.
            ctx: dict = {}
            put(ctx, "department", j.get("department"),
                EVIDENCE_TEAM_OR_DEPARTMENT, "department")
            put(ctx, "requisition_id", j.get("reference") or j.get("requisition_id"),
                EVIDENCE_ORG_ENTITY, "reference")
            put(ctx, "hiring_entity", j.get("company_name"),
                EVIDENCE_ORG_ENTITY, "company_name")
            put(ctx, "ats", "recruitee", EVIDENCE_ORG_ENTITY, "scraper")
            jobs.append(
                RawJob(
                    source="recruitee",
                    external_id=ext_id,
                    company=(j.get("company_name") or self.board_slug.replace("-", " ").title()).strip(),
                    title=(j.get("title") or "").strip(),
                    location=location,
                    remote=remote,
                    url=j.get("careers_url")
                        or f"https://{self.board_slug}.recruitee.com/o/{j.get('slug') or ext_id}",
                    description=desc.strip(),
                    posted_at=posted_dt,
                    origin="recruitee",
                    context=ctx,
                    geo=geo,
                )
            )
        log.info("Recruitee[%s]: %d jobs", self.board_slug, len(jobs))
        return jobs
