"""Greenhouse public boards API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs

This is an officially exposed public endpoint companies use to power their
own careers pages. No auth, no rate limiting in practice, and the data is
explicitly meant to be consumed. Compliant.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import (
    EVIDENCE_DIRECT_PERSON,
    EVIDENCE_ORG_ENTITY,
    EVIDENCE_TEAM_OR_DEPARTMENT,
    EVIDENCE_TITLE_ONLY,
    RawJob,
)
from app.discovery.hiring_context import put

log = logging.getLogger(__name__)

BASE = "https://boards-api.greenhouse.io/v1/boards"

# Greenhouse `metadata` carries whatever custom fields the employer chose to
# expose on the board, so its contents are per-employer and mostly irrelevant
# ("Workplace Type", "Salary Band"). These are the names worth reading, matched
# case-insensitively after stripping punctuation. A name we do not recognise is
# ignored rather than guessed at — an unmatched custom field is not evidence of
# anything.
_METADATA_KEYS = {
    "hiring manager": ("reporting_manager_name", EVIDENCE_DIRECT_PERSON),
    "hiringmanager": ("reporting_manager_name", EVIDENCE_DIRECT_PERSON),
    "recruiter": ("recruiter_name", EVIDENCE_DIRECT_PERSON),
    "recruiter name": ("recruiter_name", EVIDENCE_DIRECT_PERSON),
    "talent partner": ("recruiter_name", EVIDENCE_DIRECT_PERSON),
    "reports to": ("reporting_title", EVIDENCE_TITLE_ONLY),
    "reporting to": ("reporting_title", EVIDENCE_TITLE_ONLY),
    "team": ("team", EVIDENCE_TEAM_OR_DEPARTMENT),
    "division": ("division", EVIDENCE_TEAM_OR_DEPARTMENT),
    "business unit": ("division", EVIDENCE_TEAM_OR_DEPARTMENT),
    "requisition id": ("requisition_id", EVIDENCE_ORG_ENTITY),
    "requisition": ("requisition_id", EVIDENCE_ORG_ENTITY),
    "legal entity": ("hiring_entity", EVIDENCE_ORG_ENTITY),
    "hiring entity": ("hiring_entity", EVIDENCE_ORG_ENTITY),
}


def _metadata_value(entry: dict) -> str:
    """Greenhouse metadata values are typed: a plain string, a list for
    multi-select, or a {label,...} object. Anything else is not a value."""
    v = entry.get("value")
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v if isinstance(x, (str, int, float)))
    elif isinstance(v, dict):
        v = v.get("label") or v.get("name") or ""
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


class GreenhouseScraper:
    name = "greenhouse"

    def __init__(self, board_slug: str):
        self.board_slug = board_slug

    def fetch(self) -> List[RawJob]:
        url = f"{BASE}/{self.board_slug}/jobs?content=true"
        self.last_error = ""
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as e:
            # Record WHY this came back empty. An empty list with no last_error
            # means the board really has no openings; one with a last_error is
            # a failed poll, and the difference decides whether the scheduler
            # backs the board off or demotes it to the 72-hour tier.
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("Greenhouse fetch failed for %s: %s", self.board_slug, e)
            return []

        payload = r.json()
        jobs: List[RawJob] = []
        for j in payload.get("jobs", []):
            # Coerce to "" — a posting can carry {"location": {"name": null}},
            # and .get("name", "") returns None (the key exists), so .lower()
            # below would crash and take down the WHOLE board's fetch.
            location = (j.get("location") or {}).get("name") or ""
            remote = "remote" in location.lower()
            # first_published is the only unfalsifiable date on the posting:
            # updated_at moves every time a recruiter touches or re-posts the
            # req, so an eight-month-old listing that was refreshed yesterday
            # used to read as brand new — and "freshest first" is the whole
            # product. Fall back to updated_at only when first_published is
            # absent. See docs/research/hiring-machine-2026-08.md §1.2.
            posted = j.get("first_published") or j.get("updated_at")
            try:
                posted_dt = datetime.fromisoformat(posted.replace("Z", "+00:00")) if posted else None
            except (AttributeError, ValueError):
                posted_dt = None
            # Everything below is already in this response — no extra request.
            ctx: dict = {}
            # `departments` is a list because Greenhouse allows a posting in
            # several; the first is the primary one shown on the board.
            deps = [d.get("name") for d in (j.get("departments") or [])
                    if isinstance(d, dict) and d.get("name")]
            if deps:
                put(ctx, "department", deps[0], EVIDENCE_TEAM_OR_DEPARTMENT,
                    "departments[0].name")
                # A second department is the closest thing this board gives to
                # a sub-team; label it as such rather than inventing one.
                if len(deps) > 1:
                    put(ctx, "team", deps[1], EVIDENCE_TEAM_OR_DEPARTMENT,
                        "departments[1].name")
            put(ctx, "requisition_id", j.get("requisition_id"),
                EVIDENCE_ORG_ENTITY, "requisition_id")
            put(ctx, "ats", "greenhouse", EVIDENCE_ORG_ENTITY, "scraper")
            for entry in (j.get("metadata") or []):
                if not isinstance(entry, dict):
                    continue
                key = "".join(ch for ch in str(entry.get("name") or "").lower()
                              if ch.isalnum() or ch.isspace()).strip()
                mapped = _METADATA_KEYS.get(key)
                if not mapped:
                    continue
                target, evidence = mapped
                put(ctx, target, _metadata_value(entry), evidence,
                    f"metadata[{entry.get('name')}]")
            jobs.append(
                RawJob(
                    source="greenhouse",
                    external_id=str(j["id"]),
                    company=self.board_slug.replace("-", " ").replace("_", " ").title(),
                    title=j.get("title", ""),
                    location=location,
                    remote=remote,
                    url=j.get("absolute_url", ""),
                    description=_strip_html(j.get("content", "")),
                    posted_at=posted_dt,
                    origin="greenhouse",
                    context=ctx,
                )
            )
        log.info("Greenhouse[%s]: %d jobs", self.board_slug, len(jobs))
        return jobs
