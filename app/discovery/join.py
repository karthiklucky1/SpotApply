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

# ── The request shape join.com will actually accept ──────────────────────────
# Production measurement (2026-09-02): EVERY join jobs-API request that was not
# rate-limited came back 422. The 429s were masking a total failure — which is
# why every join board had been reporting "0 jobs" rather than some of them.
#
# A 422 is the API rejecting a parameter VALUE, and the obvious suspect is
# pageSize=100. But the right page size is a fact about join.com, not a thing to
# guess: pick 5 because a note said so and you have replaced a broken constant
# with an unverified one, and the failure mode (silently fewer jobs per board)
# looks exactly like success.
#
# So the scraper discovers it instead. On a 422 it walks a descending ladder
# until the API accepts one, MEMOIZES the winner process-wide, and logs it. The
# probe therefore runs about once per process, not once per board, and the
# answer ends up in the logs where it can be pinned via JOIN_PAGE_SIZE and this
# code deleted.
_PAGE_SIZE_LADDER = (100, 50, 25, 10, 5, 1)

# Learned at runtime; None until the first successful request. A module global
# on purpose — the discovery pool builds one JoinScraper per board, and paying
# the probe 23,547 times would be its own outage.
_LEARNED_PAGE_SIZE: Optional[int] = None
_LEARNED_DROP_LOCALE: bool = False


def _reset_learned_shape() -> None:
    """Tests only — clear what a previous test taught the module."""
    global _LEARNED_PAGE_SIZE, _LEARNED_DROP_LOCALE
    _LEARNED_PAGE_SIZE, _LEARNED_DROP_LOCALE = None, False


def _configured_page_size() -> Optional[int]:
    """A pinned JOIN_PAGE_SIZE from settings, if the answer is already known."""
    try:
        from app.config import settings
        value = int(getattr(settings, "join_page_size", 0) or 0)
        return value if value > 0 else None
    except Exception:
        return None


def _candidate_page_sizes() -> tuple[int, ...]:
    """Page sizes to try, best-known first."""
    pinned = _configured_page_size()
    if pinned:
        return (pinned,)
    if _LEARNED_PAGE_SIZE:
        return (_LEARNED_PAGE_SIZE,)
    return _PAGE_SIZE_LADDER


def _params(page: int, page_size: int, drop_locale: bool) -> dict:
    p = {"page": page, "pageSize": page_size}
    if not drop_locale:
        p["locale"] = "en-us"
    return p


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


def _body_hint(response) -> str:
    """A short slice of an error response, for the log line that explains it.

    "returned 422" tells you something broke; "returned 422: pageSize must be
    between 1 and 25" tells you what to change. The whole reason join.com's
    total failure went unnoticed is that nobody could see what it was saying.
    """
    try:
        return " ".join((response.text or "")[:300].split())
    except Exception:
        return "<no body>"


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

    def _get_page(self, client: httpx.Client, company_id: str, page: int):
        """One jobs-API page, discovering a request shape the API will accept.

        Normal case, and the case that runs 23,547 times: one request using the
        pinned or already-learned shape. The ladder only unrolls when the API
        answers 422 AND nothing has been learned yet, which is once per process.

        Deliberately does NOT probe on 429 — being throttled says nothing about
        our parameters, and hammering a rate-limited API with six variants to
        find out is how a throttle becomes a ban.
        """
        global _LEARNED_PAGE_SIZE, _LEARNED_DROP_LOCALE
        url = f"{_API}/companies/{company_id}/jobs"

        last = None
        for page_size in _candidate_page_sizes():
            for drop_locale in ((_LEARNED_DROP_LOCALE,) if _LEARNED_PAGE_SIZE
                                else (False, True)):
                r = client.get(url, params=_params(page, page_size, drop_locale))
                if r.status_code != 422:
                    if r.status_code == 200 and (
                            _LEARNED_PAGE_SIZE != page_size
                            or _LEARNED_DROP_LOCALE != drop_locale):
                        _LEARNED_PAGE_SIZE = page_size
                        _LEARNED_DROP_LOCALE = drop_locale
                        log.warning(
                            "Join: jobs API accepts pageSize=%d locale=%s — "
                            "learned from a 422 probe and reused process-wide. "
                            "Pin it with JOIN_PAGE_SIZE=%d to skip the probe.",
                            page_size, "omitted" if drop_locale else "en-us",
                            page_size)
                    return r
                last = r
                if _configured_page_size() or _LEARNED_PAGE_SIZE:
                    # A known-good shape just 422'd. That is a contract CHANGE,
                    # not something to re-probe per board — surface it.
                    return r
        log.warning("Join[%s]: every request shape returned 422 (%s)",
                    self.board_slug, _body_hint(last) if last is not None else "")
        return last

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
                    r = self._get_page(client, company_id, page)
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
                    # A contract error the shape probe could not talk the API out
                    # of. On page 1 this is not evidence the board is empty, so it
                    # must not be reported as such; later pages keep what we have.
                    # The BODY goes in the message: join.com names the offending
                    # field, and that one line is worth more than any amount of
                    # guessing about which parameter it dislikes.
                    if page == 1:
                        raise JoinTransientError(
                            f"join jobs API returned {r.status_code}: {_body_hint(r)}")
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
                # The envelope's page-count field: read whichever spelling is
                # present rather than betting on one. `totalPages` was the
                # original guess and `pageCount` the claimed correction; nobody
                # could check, and getting it wrong is silent — the walk simply
                # stops after page 1 on every board and looks like a small board.
                # Accepting both costs nothing and cannot be wrong.
                pagination = payload.get("pagination") or {}
                total_pages = page
                for key in ("pageCount", "totalPages", "totalPageCount", "pages"):
                    if key in pagination:
                        try:
                            total_pages = int(pagination[key])
                        except (TypeError, ValueError):
                            total_pages = page
                        break
                else:
                    if page == 1 and pagination:
                        log.info("Join[%s]: pagination envelope keys=%s — no known "
                                 "page-count field", self.board_slug,
                                 sorted(pagination)[:8])
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
