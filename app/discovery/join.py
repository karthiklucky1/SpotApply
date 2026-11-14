"""join.com public job boards (EU-heavy SMB ATS). Two steps:

    GET  https://join.com/companies/{slug}        → resolve the company id
    POST https://join.com/candidate-api/graphql   → publicJobs(companyId), the
                                                     query join.com's own
                                                     company page runs

Public, unauthenticated. join.com is the largest single ATS in the open dataset
(~23.5K companies) and is European-heavy — key coverage for a worldwide launch.

WHY THE LISTING IS A GRAPHQL QUERY (2026-11-14). This adapter read
``GET /api/public/companies/{id}/jobs``. That endpoint accepted five jobs a
page, rate-limited by address at about two requests a second, and was then
REMOVED: by 2026-11-13 it answered ``404 Cannot GET``. The old code read a 404
on the jobs URL as "conclusive: nothing more to read", so every board that got
past the throttle was recorded as a successful poll of an empty board — 19,766
active boards showing 4 with jobs, 19,617 of them with "returned 429" as the
last thing the registry heard. The query below is one request for up to 100
postings (verified against the live site), so a board costs one POST once its
company id is known, not a page walk.

ERROR SEMANTICS ARE THE POINT HERE. Four outcomes, never folded together:

  * a genuinely empty or absent board  → ``[]``, a real answer
  * a throttle / transport / 5xx       → raises ``JoinTransientError``, so the
                                          lane backs the board off instead of
                                          writing "zero jobs" over its state
  * the site refusing us (401/403)     → raises ``BoardBlockedError``
  * an answer we cannot read — the     → raises ``BoardUnreadableError``. The
    endpoint gone, GraphQL ``errors``,    endpoint has no company in its path,
    a changed envelope                    so a 404 there is about the ENDPOINT
  * a partial page walk                → ``fetch_complete = False``, so postings
                                          we never saw are not ghost-closed

The last three are spared from board retirement (``pipeline._is_throttled``):
none of them is a statement about the employer.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import List, Optional

import httpx

from app.discovery.base import (
    BoardBlockedError,
    BoardUnreadableError,
    GeoEvidence,
    RawJob,
    body_hint,
)

log = logging.getLogger(__name__)

_BASE = "https://join.com"
_GRAPHQL = f"{_BASE}/candidate-api/graphql"
# The company object embedded in the Next.js page carries id + domain.
_COMPANY_RE = re.compile(r'"company"\s*:\s*\{[^{}]*?"id"\s*:\s*(\d+)[^{}]*?"domain"\s*:\s*"([^"]+)"')

# One page holds 100 postings; a board is walked to this many and no further.
# The walk stops as soon as the envelope says there are no more pages, so a
# board with three openings costs one request — only genuinely large boards pay
# for depth, and past the cap the result says it is a subset.
_PAGE_SIZE = 100
_MAX_JOBS_PER_BOARD = 300
_MAX_PAGES = max(1, -(-_MAX_JOBS_PER_BOARD // _PAGE_SIZE))

# Only the fields this adapter reads: a GraphQL query fails WHOLE when one
# selected field leaves the schema, so every extra field is a way to break.
# `id` breaks title ties so offset pages keep a stable order.
_JOBS_QUERY = (
    "query PublicJobsList($input: PublicJobsQueryInput!) {"
    " publicJobs(input: $input) {"
    " items { id idParam title createdAt workplaceType city { cityName countryName } }"
    " pageInfo { pageCount rowCount } } }"
)
_SORT = [{"name": "title", "direction": "ASC"}, {"name": "id", "direction": "ASC"}]
# Localises category / country names in the answer.
_HEADERS = {"content-language": "en-us"}

# Statuses that say "not now", never "not here". A board answering with any of
# these is not evidence about the board.
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_WORK_MODES = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}

# slug → company id, process-wide. The id never changes, and resolving it is a
# ~115 KB page per board: paying that on every poll doubled the requests we
# sent join.com and was most of the bytes. Only POSITIVE answers are kept — a
# slug with no company is asked again (it costs one request per zero-yield
# cadence), because a company can appear.
_COMPANY_IDS: dict[str, str] = {}
_COMPANY_IDS_MAX = 60_000


def _reset_company_ids() -> None:
    """Tests only — forget what a previous test resolved."""
    _COMPANY_IDS.clear()


# The name the tests and older callers know.
_reset_learned_shape = _reset_company_ids


class JoinTransientError(RuntimeError):
    """The board could not be read this time. Not a statement about the board.

    Raised rather than swallowed so the caller's existing failure path runs:
    the pulse lane marks the poll failed and applies exponential backoff, which
    is the correct response to a throttle. Returning `[]` instead is what wrote
    "this board has no jobs" into the registry every time join.com rate-limited
    us.
    """


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _read_listing(payload) -> tuple[list, int, int, Optional[int]]:
    """``(items, raw item count, pageCount, rowCount)`` from a publicJobs answer.

    ``items`` has null entries removed (join.com's search index returns them
    when it is out of sync; its own frontend drops them too); the raw count
    includes them. Anything that is not the envelope we read raises — an
    answer we cannot read is never an empty board.
    """
    errors = payload.get("errors") if isinstance(payload, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    listing = data.get("publicJobs") if isinstance(data, dict) else None
    page_info = listing.get("pageInfo") if isinstance(listing, dict) else None
    items = listing.get("items") if isinstance(listing, dict) else None
    page_count = page_info.get("pageCount") if isinstance(page_info, dict) else None
    row_count = page_info.get("rowCount") if isinstance(page_info, dict) else None
    if errors or not isinstance(items, list) or not isinstance(page_count, int) \
            or isinstance(page_count, bool):
        try:
            detail = json.dumps(errors or payload)[:240]
        except (TypeError, ValueError):
            detail = "<unprintable>"
        raise BoardUnreadableError("join", f"publicJobs answer is not the shape we read: {detail}")
    usable = [it for it in items if isinstance(it, dict) and it.get("id") is not None]
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        row_count = None
    return usable, len(items), page_count, row_count


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
        key = self.board_slug.lower()
        known = _COMPANY_IDS.get(key)
        if known:
            return known
        try:
            r = client.get(f"{_BASE}/companies/{self.board_slug}")
        except httpx.HTTPError as e:
            raise JoinTransientError(f"join company page unreachable: {e}") from e
        if r.status_code == 404:
            return None                       # conclusive: no such company
        if r.status_code in (401, 403):
            raise BoardBlockedError("join", r.status_code, "company page")
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
            if domain.lower() == key:
                if len(_COMPANY_IDS) < _COMPANY_IDS_MAX:
                    _COMPANY_IDS[key] = cid
                return cid
        return None

    def _post_page(self, client: httpx.Client, company_id: str, page: int):
        return client.post(
            _GRAPHQL,
            json={"query": _JOBS_QUERY, "variables": {"input": {
                "companyId": int(company_id),
                "paginationPage": {"page": page, "pageSize": _PAGE_SIZE},
                "sort": _SORT,
            }}},
            headers=_HEADERS,
        )

    def _job(self, it: dict) -> Optional[RawJob]:
        ext_id = str(it.get("id")).strip()
        title = _text(it.get("title")) or _text(it.get("name"))
        if not ext_id or not title:
            return None
        city = it.get("city") if isinstance(it.get("city"), dict) else {}
        country = _text(city.get("countryName"))
        location = ", ".join(p for p in (_text(city.get("cityName")), country) if p)
        # `workplaceType` is join.com's work mode (REMOTE / HYBRID / ONSITE).
        work_mode = _WORK_MODES.get(_text(it.get("workplaceType")).lower(), "")
        remote = work_mode == "remote" or "remote" in location.lower()
        geo = GeoEvidence(
            sites=[location] if location else [], sites_field="city.cityName+countryName",
            country=country, country_field="city.countryName" if country else "",
            work_mode=work_mode, work_mode_field="workplaceType" if work_mode else "",
        ) if (location or country or work_mode) else None
        posted_dt = None
        created = it.get("createdAt") or it.get("publishedAt")
        if created:
            try:
                posted_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            except ValueError:
                pass
        # The page a person opens is addressed by `idParam` ("<number>-<title
        # slug>"); the numeric `id` is the API's key and stays the external id,
        # which is what every row stored since the REST days is keyed by.
        page_key = _text(it.get("idParam")) or ext_id
        return RawJob(
            source="join",
            external_id=ext_id,
            company=self.board_slug.replace("-", " ").title(),
            title=title,
            location=location,
            remote=remote,
            url=_text(it.get("url")) or f"{_BASE}/companies/{self.board_slug}/jobs/{page_key}",
            description="",
            posted_at=posted_dt,
            origin="join",
            geo=geo,
        )

    def fetch(self) -> List[RawJob]:
        try:
            return self._fetch()
        except BoardUnreadableError:
            # An answer we could not read may be about the ID we remembered (a
            # company that was removed or re-keyed). Forget it, so the next
            # poll asks the company page again — whose 404 is the conclusive
            # "no such company" a remembered id would otherwise never reach.
            _COMPANY_IDS.pop(self.board_slug.lower(), None)
            raise

    def _fetch(self) -> List[RawJob]:
        jobs: List[RawJob] = []
        seen: set[str] = set()
        self.signature_entries = []
        self.fetch_complete = True
        partial = False
        row_count: Optional[int] = None
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            company_id = self._resolve_company_id(client)
            if not company_id:
                return []
            page = 1
            while True:
                first = page == 1
                try:
                    r = self._post_page(client, company_id, page)
                except httpx.HTTPError as e:
                    if first:
                        raise JoinTransientError(f"join jobs API unreachable: {e}") from e
                    # Later pages: we already hold real postings for this board.
                    # Keep them, but say the walk was cut short.
                    partial = True
                    log.info("Join[%s]: page %d unreachable (%s) — partial result",
                             self.board_slug, page, e)
                    break

                if r.status_code != 200:
                    if not first:
                        partial = True
                        log.info("Join[%s]: page %d returned %d — partial result",
                                 self.board_slug, page, r.status_code)
                        break
                    if r.status_code in (401, 403):
                        raise BoardBlockedError("join", r.status_code, "jobs query")
                    if r.status_code == 404:
                        # No company in this URL: a 404 is the ENDPOINT, never
                        # "this company has no jobs". Reading it as empty is
                        # how the REST endpoint's removal went unnoticed.
                        raise BoardUnreadableError(
                            "join", "jobs query endpoint not found (moved or removed)")
                    if r.status_code in _TRANSIENT_STATUS:
                        raise JoinTransientError(f"join jobs API returned {r.status_code}")
                    # A contract error. The BODY goes in the message: join.com
                    # names the offending field, and that one line is worth more
                    # than any amount of guessing about what it dislikes.
                    raise JoinTransientError(
                        f"join jobs API returned {r.status_code}: {body_hint(r.text, 300)}")

                try:
                    items, raw_len, page_count, row_count = _read_listing(r.json())
                except (json.JSONDecodeError, ValueError) as e:
                    if first:
                        raise BoardUnreadableError(
                            "join", f"jobs query sent unparseable JSON: {e}") from e
                    partial = True
                    break
                except BoardUnreadableError:
                    if first:
                        raise
                    partial = True
                    break

                for it in items:
                    job = self._job(it)
                    if job is None:
                        continue
                    # join.com repeats a posting across pages when the board
                    # changes mid-walk, and a repeated external_id is a second
                    # upsert of the same row on every poll.
                    if job.external_id in seen:
                        continue
                    seen.add(job.external_id)
                    self.signature_entries.append((job.external_id, job.title))
                    jobs.append(job)

                if page >= page_count:
                    break
                if raw_len == 0:
                    # The envelope says more pages remain and this one is
                    # empty: stopping here is a subset, not the board.
                    partial = True
                    break
                if page >= _MAX_PAGES:
                    # More pages exist than we walk. Not an error — but the
                    # result IS a subset, and saying so is what stops the
                    # postings past the cap from being ghost-closed.
                    partial = True
                    log.info("Join[%s]: %d pages available, walked %d — partial result",
                             self.board_slug, page_count, _MAX_PAGES)
                    break
                page += 1

        if row_count and not jobs and not partial:
            raise BoardUnreadableError(
                "join", f"publicJobs reported {row_count} postings and returned none we could read")
        # Whole only when the walk finished AND we hold as many postings as the
        # envelope counted (null index entries make the two differ).
        self.fetch_complete = not partial and (row_count is None or len(jobs) >= row_count)
        log.info("Join[%s]: %d jobs (complete=%s)",
                 self.board_slug, len(jobs), self.fetch_complete)
        return jobs
