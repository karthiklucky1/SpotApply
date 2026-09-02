"""join.com public jobs API (EU-heavy SMB ATS). Two-step:

    GET https://join.com/companies/{slug}                    → resolve company id
    GET https://join.com/api/public/companies/{id}/jobs      → paginated jobs JSON

Public, unauthenticated. join.com is the largest single ATS in the open dataset
(~23.5K companies) and is European-heavy — key coverage for a worldwide launch.

ERROR SEMANTICS ARE THE POINT HERE. This scraper used to answer every question
with an empty list: a 429, a 422, a 5xx, a connection timeout and a board that
genuinely has no openings were all `return []`. The pulse lane
(strategy/pulse_lane.py) then recorded a SUCCESSFUL poll of an empty board, and
the registry validator counted the same silence as a failed validation — seven
of which retire the board for thirty days. So being throttled looked exactly
like being dead, and enough throttling killed healthy boards.

Now the three cases are distinct:

  * a genuinely empty or absent board  → `[]`, a real answer
  * a throttle / transport / 5xx       → raises, so the lane records a failure
                                          and backs the board off instead of
                                          writing "zero jobs" over its state
  * a partial page walk                → `fetch_complete = False`, so postings
                                          we never saw are not ghost-closed
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import RawJob

log = logging.getLogger(__name__)

_BASE = "https://join.com"
_API = "https://join.com/api/public"
# The company object embedded in the Next.js page carries id + domain.
_COMPANY_RE = re.compile(r'"company"\s*:\s*\{[^{}]*?"id"\s*:\s*(\d+)[^{}]*?"domain"\s*:\s*"([^"]+)"')
_MAX_PAGES = 5

# Statuses that say "not now", never "not here". A board answering with any of
# these is not evidence about the board.
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class JoinTransientError(RuntimeError):
    """The board could not be read this time. Not a statement about the board.

    Raised rather than swallowed so the caller's existing failure path runs:
    the pulse lane marks the poll failed and applies exponential backoff, which
    is the correct response to a throttle. Returning `[]` instead is what wrote
    "this board has no jobs" into the registry every time join.com rate-limited
    us.
    """


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


class JoinScraper:
    name = "join"

    def __init__(self, board_slug: str):
        self.board_slug = board_slug
        # Partial-fetch contract shared with the other paginated scrapers
        # (smartrecruiters.py, workday.py). fetch_complete=False tells the
        # pipeline our list is a SUBSET of the board, so postings we never
        # loaded must not be ghost-closed.
        self.fetch_complete = True
        self.signature_entries: list[tuple[str, str]] = []
        self.signature_stable = True

    def _resolve_company_id(self, client: httpx.Client) -> Optional[str]:
        """The company id for this slug, or None when the slug has no company.

        Returns None ONLY for a conclusive answer (404, or a page carrying no
        company whose domain matches the slug we asked for). Anything that means
        "ask again later" raises.
        """
        try:
            r = client.get(f"{_BASE}/companies/{self.board_slug}")
        except httpx.HTTPError as e:
            raise JoinTransientError(f"join company page unreachable: {e}") from e
        if r.status_code == 404:
            return None                       # conclusive: no such company
        if r.status_code in _TRANSIENT_STATUS:
            raise JoinTransientError(f"join company page returned {r.status_code}")
        if r.status_code != 200:
            raise JoinTransientError(f"join company page returned {r.status_code}")

        # join.com serves a PLACEHOLDER company (id 233) for empty tenants, so
        # the embedded company domain must match the slug we asked for. There
        # used to be a fallback returning the first id on the page when no
        # domain matched — which handed back exactly the placeholder this check
        # exists to reject, and every empty tenant then scraped the placeholder's
        # jobs under the wrong company name. The guard is only a guard if
        # nothing routes around it.
        for cid, domain in _COMPANY_RE.findall(r.text):
            if domain.lower() == self.board_slug.lower():
                return cid
        return None

    def fetch(self) -> List[RawJob]:
        jobs: List[RawJob] = []
        seen: set[str] = set()
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            company_id = self._resolve_company_id(client)
            if not company_id:
                return []
            page = 1
            while page <= _MAX_PAGES:
                try:
                    r = client.get(
                        f"{_API}/companies/{company_id}/jobs",
                        params={"locale": "en-us", "page": page, "pageSize": 100},
                    )
                except httpx.HTTPError as e:
                    if page == 1:
                        raise JoinTransientError(f"join jobs API unreachable: {e}") from e
                    # Later pages: we already hold real postings for this board.
                    # Keep them, but say the walk was cut short.
                    self.fetch_complete = False
                    log.info("Join[%s]: page %d unreachable (%s) — partial result",
                             self.board_slug, page, e)
                    break

                if r.status_code in _TRANSIENT_STATUS:
                    if page == 1:
                        raise JoinTransientError(
                            f"join jobs API returned {r.status_code}")
                    self.fetch_complete = False
                    log.info("Join[%s]: page %d returned %d — partial result",
                             self.board_slug, page, r.status_code)
                    break
                if r.status_code == 404:
                    break                     # conclusive: nothing more to read
                if r.status_code != 200:
                    # A contract error (422 and friends). On page 1 this is not
                    # evidence the board is empty, so it must not be reported as
                    # such; later pages keep what we have.
                    if page == 1:
                        raise JoinTransientError(
                            f"join jobs API returned {r.status_code}")
                    self.fetch_complete = False
                    break

                try:
                    payload = r.json()
                except (json.JSONDecodeError, ValueError) as e:
                    if page == 1:
                        raise JoinTransientError(f"join jobs API sent unparseable JSON: {e}") from e
                    self.fetch_complete = False
                    break

                items = payload.get("items", []) if isinstance(payload, dict) else []
                if not items:
                    break                     # conclusive: the board ends here
                for j in items:
                    ext_id = str(j.get("id") or j.get("idParam") or "").strip()
                    if not ext_id:
                        continue
                    # join.com repeats a posting across pages when the board
                    # changes mid-walk, and a repeated external_id is a second
                    # upsert of the same row on every poll.
                    if ext_id in seen:
                        continue
                    seen.add(ext_id)
                    title = (j.get("name") or j.get("title") or "").strip()
                    self.signature_entries.append((ext_id, title))
                    loc = j.get("location") or {}
                    if isinstance(loc, dict):
                        location = ", ".join(p for p in (
                            (loc.get("cityName") or loc.get("city") or "").strip(),
                            (loc.get("countryName") or loc.get("country") or "").strip(),
                        ) if p)
                    else:
                        location = str(loc or "")
                    remote = (str(j.get("jobLocationType") or "").lower() == "remote"
                              or "remote" in location.lower())
                    posted_dt = None
                    published = j.get("publishedAt") or j.get("createdAt")
                    if published:
                        try:
                            posted_dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                        except ValueError:
                            pass
                    jobs.append(
                        RawJob(
                            source="join",
                            external_id=ext_id,
                            company=self.board_slug.replace("-", " ").title(),
                            title=title,
                            location=location,
                            remote=remote,
                            url=j.get("url") or f"{_BASE}/companies/{self.board_slug}/jobs/{ext_id}",
                            description=_strip_html(j.get("description") or ""),
                            posted_at=posted_dt,
                        )
                    )
                pagination = payload.get("pagination") or {}
                total_pages = pagination.get("totalPages", page)
                try:
                    total_pages = int(total_pages)
                except (TypeError, ValueError):
                    total_pages = page
                if page >= total_pages:
                    break
                if page >= _MAX_PAGES:
                    # More pages exist than we walk. Not an error — but the
                    # result IS a subset, and saying so is what stops the
                    # postings on page 6 from being ghost-closed.
                    self.fetch_complete = False
                    log.info("Join[%s]: %d pages available, walked %d — partial result",
                             self.board_slug, total_pages, _MAX_PAGES)
                    break
                page += 1
        log.info("Join[%s]: %d jobs (complete=%s)",
                 self.board_slug, len(jobs), self.fetch_complete)
        return jobs
