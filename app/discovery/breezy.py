"""Breezy HR public JSON: https://{slug}.breezy.hr/json

One public, unauthenticated JSON endpoint per tenant. The list carries the
canonical job URL and publish date; descriptions are per-posting but the list
gives enough (title/location/date) for freshness + matching.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

from app.discovery import detail_memo
from app.discovery.base import (
    BoardBlockedError,
    BoardUnreadableError,
    GeoEvidence,
    RawJob,
    body_hint,
    listing_truncated,
    swallowed,
)

from app.discovery.title_filter import is_obvious_non_tech  # noqa: E402

log = logging.getLogger(__name__)


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def _text(val) -> str:
    """Coerce an API field to a clean string; Breezy sometimes nests
    city/state/country as {"name": ..., "id": ...} objects."""
    if isinstance(val, dict):
        val = val.get("name") or val.get("label") or ""
    return str(val or "").strip()


def _description_from_page(html: str) -> str:
    """The posting body on a Breezy position page: `<div class="description">`.

    `/json` lists every position WITHOUT its description; the page is
    server-rendered HTML, fetched ONCE per posting (app/discovery/detail_memo.py).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    node = soup.select_one("div.description")
    return node.get_text(separator="\n").strip() if node else ""


class BreezyScraper:
    name = "breezy"

    def __init__(self, board_slug: str):
        self.board_slug = board_slug

    def fetch(self) -> List[RawJob]:
        url = f"https://{self.board_slug}.breezy.hr/json"
        # Complete only once the whole listing parsed (app/discovery/base.py).
        self.fetch_complete = False
        # "" = the fetch succeeded; an empty list WITH a reason is not an
        # empty board (app/discovery/base.py `fetch_error`, `swallowed`).
        self.last_error = ""
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
            log.warning("Breezy fetch failed for %s: %s", self.board_slug, e)
            return swallowed(self, "breezy", e)
        except httpx.HTTPError as e:
            log.warning("Breezy fetch failed for %s: %s", self.board_slug, e)
            return swallowed(self, "breezy", e)

        try:
            payload = r.json()
        except Exception as e:
            # A 200 we cannot read is not "no openings".
            raise BoardUnreadableError(
                "breezy", f"board did not send JSON: {body_hint(r.text)}") from e
        items = payload if isinstance(payload, list) else payload.get("positions", [])
        jobs: List[RawJob] = []
        poll = detail_memo.Poll("breezy")
        # Every position the listing named, whatever we do with it below: what
        # board absence is judged against (app/discovery/base.py).
        self.listed_ids: set = set()
        if poll.on:
            self.signature_entries = []
            self.signature_stable = True
        for j in items or []:
            ext_id = str(j.get("_id") or j.get("id") or "").strip()
            if not ext_id:
                continue
            self.listed_ids.add(ext_id)
            title = _text(j.get("name"))
            if poll.on and is_obvious_non_tech(title):
                continue    # nobody is looking for it: no page fetch, no row
            loc = j.get("location") or {}
            country = ""
            if isinstance(loc, dict):
                country = _text(loc.get("country"))
                location = ", ".join(p for p in (
                    _text(loc.get("city")),
                    _text(loc.get("state")),
                    country,
                ) if p) or _text(loc.get("name"))
                is_remote = bool(loc.get("is_remote"))
            else:
                location = str(loc)
                is_remote = False
            # The flag AND the word: a dict location with is_remote absent
            # used to make a location literally reading "Remote" non-remote.
            remote = is_remote or "remote" in location.lower()
            geo = GeoEvidence(
                sites=[location] if location else [], sites_field="location.city+state+country",
                country=country, country_field="location.country" if country else "",
                work_mode="remote" if is_remote else "", work_mode_field="location.is_remote" if is_remote else "",
            ) if (location or country or is_remote) else None
            posted_dt = None
            published = j.get("published_date") or j.get("creation_date")
            if published:
                try:
                    posted_dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                except ValueError:
                    pass
            job_url = j.get("url") or f"https://{self.board_slug}.breezy.hr/p/{ext_id}"
            description = _strip_html(j.get("description") or "")
            if poll.on:
                self.signature_entries.append((ext_id, title))
            action, fp = poll.decide(ext_id, title, location)
            if action == "skip":
                continue    # no budget this poll; it comes back on the next
            if action == "fetch":
                text = self._detail(job_url, poll)
                if not text:
                    poll.failed()
                    continue
                poll.fetched()
                description = text
            jobs.append(
                RawJob(
                    source="breezy",
                    external_id=ext_id,
                    company=self.board_slug.replace("-", " ").title(),
                    title=title,
                    location=location,
                    remote=remote,
                    url=job_url,
                    description="" if action == "known" else description,
                    posted_at=posted_dt,
                    origin="breezy",
                    geo=geo,
                    listing_only=action == "known",
                    listing_fp=fp if action == "fetch" else None,
                )
            )
        self.listing_complete = isinstance(payload, list) or (
            isinstance(payload, dict) and isinstance(items, list)
            and "positions" in payload and not listing_truncated(payload, len(items)))
        # Whole only when every position we wanted is in the list we return.
        self.fetch_complete = self.listing_complete and poll.n_skipped == 0
        self.ingest_required = poll.ingest_required
        log.info("Breezy[%s]: %d jobs (%d descriptions fetched, %d waiting)",
                 self.board_slug, len(jobs), poll.n_fetched, poll.n_skipped)
        return jobs

    def _detail(self, url: str, poll) -> str:
        """A position's description from its own page, or "" when it did not
        arrive. Being refused or throttled stops the poll (the board is backed
        off); a page that will not load turns detail requests off for the rest
        of this poll."""
        try:
            r = httpx.get(url, timeout=20.0, follow_redirects=True)
        except httpx.HTTPError:
            poll.off = True
            return ""
        if r.status_code == 429:
            raise BoardBlockedError("breezy", 429, "position page")
        if r.status_code != 200:
            return ""
        return _description_from_page(r.text)
