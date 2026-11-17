"""Rippling public ATS board API: https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs

Public JSON, no auth. Rippling's own ATS product; growing US SMB coverage.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import (
    EVIDENCE_TEAM_OR_DEPARTMENT,
    BoardBlockedError,
    BoardUnreadableError,
    GeoEvidence,
    RawJob,
    body_hint,
    listing_truncated,
)
from app.discovery import detail_memo
from app.discovery.hiring_context import put
from app.discovery.title_filter import is_obvious_non_tech

log = logging.getLogger(__name__)

_API = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
_HEADERS = {"Accept": "application/json"}
# The listing carries id, title, department and location — no description and
# no date. Both are on the posting's own record, fetched ONCE per posting
# (app/discovery/detail_memo.py).
_DETAIL = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{id}"


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def _context(item: dict) -> dict:
    """The department the listing names — a key the response already carries."""
    ctx: dict = {}
    dep = item.get("department")
    label = dep.get("label") if isinstance(dep, dict) else dep
    put(ctx, "department", label, EVIDENCE_TEAM_OR_DEPARTMENT, "department.label")
    return ctx


def _detail_text(detail: dict) -> str:
    """The posting's description: `description` is {company, role} HTML."""
    d = detail.get("description")
    if isinstance(d, dict):
        parts = [_strip_html(d.get(k) or "") for k in ("role", "company")]
        return "\n\n".join(p for p in parts if p)
    return _strip_html(d) if isinstance(d, str) else ""


def _detail_date(detail: dict):
    v = detail.get("createdOn") or detail.get("publishedAt")
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


class RipplingScraper:
    name = "rippling"

    def __init__(self, board_slug: str):
        self.board_slug = board_slug

    def fetch(self) -> List[RawJob]:
        # Complete only once the whole listing parsed (app/discovery/base.py).
        self.fetch_complete = False
        # "" = the fetch succeeded; an empty list WITH a reason here is not an
        # empty board (app/discovery/base.py `fetch_error`).
        self.last_error = ""
        try:
            r = httpx.get(_API.format(slug=self.board_slug), timeout=30.0,
                          follow_redirects=True, headers=_HEADERS)
        except httpx.HTTPError as e:
            log.warning("Rippling fetch failed for %s: %s", self.board_slug, e)
            self.last_error = f"rippling API unreachable: {e}"
            return []
        status = r.status_code
        if status in (403, 429):
            # EVERY board is served by one host behind one edge, so a refusal is
            # about OUR ADDRESS, not this employer. On 2026-11-14 that host
            # answered 403 to production for every board while the identical
            # request succeeded from another network — and because 403 sat in
            # the "permanent" list below, each refusal was a strike: 960 boards
            # holding 15,293 open jobs were retired as "unreachable x5" in one
            # day. A refusal is backed off, never counted (base.py).
            raise BoardBlockedError("rippling", status)
        if status in (401, 404, 410):
            # Permanent statuses mean the slug is wrong, gone, or private — let
            # the exception propagate so the discovery pipeline's dead-board
            # recorder retires the registry row (these long-tail ATSes are NOT
            # covered by the Greenhouse/Lever/Ashby validation loop, so a junk
            # slug would otherwise 404 on every cycle forever).
            r.raise_for_status()
        if status != 200:
            log.warning("Rippling fetch failed for %s: HTTP %d", self.board_slug, status)
            self.last_error = f"rippling API returned {status}"
            return []

        try:
            payload = r.json()
        except ValueError as e:
            raise BoardUnreadableError(
                "rippling", f"board API did not send JSON: {body_hint(r.text)}") from e
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(
                payload.get("items", payload.get("jobs")), list):
            items = payload.get("items", payload.get("jobs"))
        else:
            # A 200 we cannot read is not "no openings".
            raise BoardUnreadableError(
                "rippling", f"board API answer is not a job list: {body_hint(r.text)}")
        jobs: List[RawJob] = []
        poll = detail_memo.Poll("rippling")
        # Every posting the listing named, whatever we do with it below: what
        # board absence is judged against (app/discovery/base.py).
        self.listed_ids: set = set()
        if poll.on:
            # Listing-phase identity: a detail that fails, or a posting that
            # waits for budget, must not read as the board having changed.
            self.signature_entries = []
            self.signature_stable = True
        for j in items or []:
            ext_id = str(j.get("id") or j.get("uuid") or "").strip()
            if not ext_id:
                continue
            self.listed_ids.add(ext_id)
            title = (j.get("name") or j.get("title") or "").strip()
            if poll.on and is_obvious_non_tech(title):
                continue    # nobody is looking for it: no detail request, no row
            loc = j.get("workLocation") or {}
            location = (loc.get("label") or j.get("location") or "").strip() if isinstance(loc, dict) else str(loc)
            # `workplaceType` (REMOTE / HYBRID / ONSITE) and the plural
            # `locations[]` were never read; the gate saw one label.
            sites = [location] if location else []
            for extra in (j.get("locations") or []):
                v = extra.get("label") if isinstance(extra, dict) else extra
                if isinstance(v, str) and v.strip() and v.strip() not in sites:
                    sites.append(v.strip())
            wpt = str(j.get("workplaceType") or "").strip().lower()
            work_mode = ("remote" if wpt == "remote" else "hybrid" if wpt == "hybrid"
                         else "onsite" if wpt in ("onsite", "on_site", "on-site", "in_office") else "")
            if not work_mode and j.get("isRemote"):
                work_mode = "remote"
            remote = "remote" in location.lower() or work_mode == "remote"
            geo = GeoEvidence(
                sites=sites, sites_field="workLocation.label+locations[].label",
                work_mode=work_mode, work_mode_field=("workplaceType" if wpt else "isRemote") if work_mode else "",
            ) if (sites or work_mode) else None
            posted_dt = None
            for key in ("publishedAt", "createdAt", "postedDate"):
                v = j.get(key)
                if not v:
                    continue
                try:
                    if isinstance(v, (int, float)):
                        posted_dt = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
                    else:
                        posted_dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                    break
                except (ValueError, OSError):
                    pass
            description = _strip_html(j.get("description") or "")
            if poll.on:
                self.signature_entries.append((ext_id, title))
            action, fp = poll.decide(ext_id, title, location)
            if action == "skip":
                continue    # no budget this poll; it comes back on the next
            if action == "fetch":
                detail = self._detail(ext_id)
                if detail is None:
                    poll.failed()
                    continue
                poll.fetched()
                description = _detail_text(detail) or description
                posted_dt = _detail_date(detail) or posted_dt
                for extra in (detail.get("workLocations") or []):
                    v = extra.get("label") if isinstance(extra, dict) else extra
                    if isinstance(v, str) and v.strip() and v.strip() not in sites:
                        sites.append(v.strip())
                if sites and geo is not None:
                    geo.sites = sites
            jobs.append(
                RawJob(
                    source="rippling",
                    external_id=ext_id,
                    company=(j.get("companyName") or self.board_slug.replace("-", " ").title()).strip(),
                    title=title,
                    location=location,
                    remote=remote,
                    url=j.get("url") or j.get("applyUrl")
                        or f"https://app.rippling.com/jobs/{self.board_slug}/{ext_id}",
                    description="" if action == "known" else description,
                    posted_at=posted_dt,
                    origin="rippling",
                    geo=geo,
                    context=_context(j),
                    listing_only=action == "known",
                    listing_fp=fp if action == "fetch" else None,
                )
            )
        # A bare array is the whole board; an envelope is whole only when it
        # carried a list we read and says nothing about further pages.
        self.listing_complete = isinstance(payload, list) or (
            isinstance(payload, dict) and isinstance(items, list)
            and ("items" in payload or "jobs" in payload)
            and not listing_truncated(payload, len(items)))
        # Whole only when every posting we wanted is in the list we return: one
        # that is still waiting for its detail is listed, not parsed.
        self.fetch_complete = self.listing_complete and poll.n_skipped == 0
        self.ingest_required = poll.ingest_required
        log.info("Rippling[%s]: %d jobs (%d details fetched, %d waiting)",
                 self.board_slug, len(jobs), poll.n_fetched, poll.n_skipped)
        return jobs

    def _detail(self, ext_id: str):
        """One posting's own record, or None when it did not arrive.

        A refusal here is the same refusal as on the listing — the same host —
        and stops the poll: asking for the next posting's detail after being
        told no is how a rate limit becomes a block.
        """
        try:
            r = httpx.get(_DETAIL.format(slug=self.board_slug, id=ext_id), timeout=20.0,
                          follow_redirects=True, headers=_HEADERS)
        except httpx.HTTPError:
            return None
        if r.status_code in (403, 429):
            raise BoardBlockedError("rippling", r.status_code, "job detail")
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
