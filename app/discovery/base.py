"""Base scraper protocol — every source returns a list of normalized Job dicts."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Protocol

# Allowed `ContextField.evidence` values. Kept as plain strings rather than
# importing app.db.models.EvidenceClass so that every scraper does not pull
# SQLModel in through this module; app/discovery/hiring_context.py asserts the
# two lists agree, and tests/test_hiring_context_capture.py pins that.
EVIDENCE_DIRECT_PERSON = "DIRECT_PERSON"
EVIDENCE_STRUCTURED_PERSON = "STRUCTURED_PERSON"
EVIDENCE_TITLE_ONLY = "TITLE_ONLY"
EVIDENCE_TEAM_OR_DEPARTMENT = "TEAM_OR_DEPARTMENT"
EVIDENCE_ORG_ENTITY = "ORG_ENTITY"
EVIDENCE_SELF_IDENTIFIED = "SELF_IDENTIFIED"
EVIDENCE_SUGGESTED = "SUGGESTED"
EVIDENCE_NONE = "NONE"


@dataclass
class ContextField:
    """One hiring-context value plus the proof that it is real.

    `field` names the upstream key it came from, verbatim, so a value can
    always be traced back to the exact response it was read out of. A value
    with no ContextField never reaches the database.
    """
    value: str
    evidence: str                 # one of the EVIDENCE_* constants above
    field: str = ""               # upstream key, e.g. "departments[0].name"
    quote: str = ""               # verbatim supporting text, for text evidence


@dataclass
class GeoEvidence:
    """What the SOURCE said about where a posting is — verbatim, untruncated.

    `RawJob.location` is the string a card shows, and it is allowed to be
    shortened for display (Ashby caps a 28-site posting at "+N more"). This is
    the record the eligibility gate reads instead, so a US site hidden behind
    the display cap still counts, and a structured country code the ATS
    emitted is not lost to a free-text guess. Every field names the upstream
    key it was read from so a verdict can be traced to the response.

    Nothing here is inferred: an adapter fills a field only from a value the
    response actually carried. A department, a board's home country, a
    currency or a title suffix are never evidence of location (Teamtailor
    stored "POS Integrations" as a city for months).
    """
    sites: List[str] = field(default_factory=list)      # every site, in source order
    sites_field: str = ""                                # e.g. "categories.allLocations"
    country: str = ""                                    # structured country, verbatim ("US", "Sweden")
    country_field: str = ""                              # e.g. "address.postalAddress.addressCountry"
    work_mode: str = ""                                  # remote | hybrid | onsite | "" (unknown)
    work_mode_field: str = ""                            # e.g. "workplaceType"
    remote_regions: str = ""                             # the source's own remote restriction text
    remote_regions_field: str = ""                       # e.g. "candidate_required_location"


@dataclass
class RawJob:
    """Normalized job representation before DB insertion."""
    source: str
    external_id: str
    company: str
    title: str
    location: str
    remote: bool
    url: str
    description: str
    posted_at: Optional[datetime] = None
    # Structured location evidence from the response (see GeoEvidence). None
    # for sources that expose nothing structured; the geography pass then
    # reads `location` alone, exactly as the gate always has.
    geo: Optional[GeoEvidence] = None
    # When THIS posting first entered SpotApply anywhere — not when this copy of
    # the row was written. Scrapers leave it None (they ARE the first sighting);
    # only the copiers set it: adoption and the pulse lane's per-user route move
    # a posting that already exists in the shared pool, and re-stamping
    # first_seen=now there restarted the whole KNOWN-age clock. A three-week-old
    # shared row entered a user's board labelled "New", inside the 5-day scoring
    # window, and the "be one of the first to apply" promise was measured from
    # the moment we copied a DB row. See app/common/freshness.py.
    first_seen: Optional[datetime] = None
    # ── provenance (see the comment on Job.origin) ──
    # The discovery module that produced this row, when it differs from the
    # `source` routing bucket. Defaults to `source` at write time.
    origin: Optional[str] = None
    # The board the posting actually lives on, when the producer is an
    # aggregator that knows it (SerpAPI's `via`).
    origin_provider: Optional[str] = None
    # Pay as the ATS itself states it, when the response carries a structured
    # field for it (Ashby's `compensation.compensationTierSummary`). None means
    # "the source has no such field", NOT "unpaid": `_build_job` then falls back
    # to the regex facet over the description (app/strategy/job_facets.py). A
    # value here WINS over that regex — the audit found five Ashby postings with
    # "$160-200K" on the page and nothing on the card because the salary lived
    # only in this field, which nobody read.
    salary_text: Optional[str] = None
    # ── detail fetched once (app/discovery/detail_memo.py) ──
    # `listing_only` = a re-sighting of a posting whose description the pool
    # already holds: the adapter did NOT fetch its detail again, so this row
    # carries no description and must never be written — the upsert refreshes
    # `last_seen` on the stored row and does nothing else. `listing_fp` rides on
    # a row that DOES carry its detail, so the upsert can record that the pool
    # now holds it.
    listing_only: bool = False
    listing_fp: Optional[str] = None
    # ── hiring context read straight out of the response, zero extra calls ──
    # Keys are JobHiringContext column names: department, team, division,
    # hiring_entity, recruiting_agency, requisition_id, reporting_title,
    # reporting_manager_name, recruiter_name, posting_creator_name,
    # contact_email. Empty for sources that expose nothing.
    context: Dict[str, ContextField] = field(default_factory=dict)


class Scraper(Protocol):
    name: str
    # Why the last fetch returned what it did. "" means the fetch succeeded —
    # an EMPTY list with an empty last_error is a genuinely empty board.
    #
    # This attribute exists because the two are not the same thing and the
    # lanes were treating them as one (2026-09-12 audit). The big ATS adapters
    # return [] for every httpx.HTTPError, so a 429 from Workday or a 503 from
    # Greenhouse arrived at the pulse lane indistinguishable from "this company
    # has no openings". The lane then wrote job_count=0, which is the value
    # that demotes a board to the 72-hour zero-yield cadence: five busy
    # afternoons could push a live employer off the schedule for three days.
    # Callers read it through ``fetch_error(scraper)``.
    last_error: str

    def fetch(self) -> List[RawJob]:
        ...


def fetch_error(scraper) -> str:
    """The reason the last fetch came back empty, or "" if it simply was.

    Tolerates adapters that predate the convention: no attribute means the
    adapter raises on failure, so an empty list from it is a real empty board.
    """
    return (getattr(scraper, "last_error", "") or "").strip()


# ── failures that say nothing about the BOARD ────────────────────────────────
# A board is retired after a handful of failed fetches (pipeline.
# BOARD_DEACTIVATE_AFTER_FAILURES), so what counts as a failed fetch OF THE
# BOARD matters. Two kinds of failure are about the SITE or about US, and the
# 2026-11-14 audit found both being read as facts about employers:
#
#   * the site REFUSED to answer. Rippling's API began answering 403 to
#     production's address (the same request succeeded from another network),
#     every refusal counted as a strike, and 960 boards holding 15,293 open
#     jobs were retired as "unreachable x5" in one day.
#   * the site answered in a SHAPE the adapter could not read. BambooHR's
#     widget turned from HTML into JSON in October; the HTML parser found no
#     <li> in it and reported "0 jobs", successfully, for 5,557 boards for
#     five weeks. join.com removed its REST listing; the 404 was read as "the
#     board ends here".
#
# Adapters raise these instead. The message carries a marker the failure
# recorders look for (`pipeline._is_throttled`), because errors cross the
# thread pool as strings: such a board is backed off and re-polled, its
# failure count untouched, and its `job_count` is never overwritten with zero.
BLOCKED_MARKER = "site refused the request"
UNREADABLE_MARKER = "unreadable response"


class BoardBlockedError(RuntimeError):
    """The site would not answer this request (edge block, rate limit).

    Not evidence about the board: the employer may be hiring exactly as before.
    """

    def __init__(self, source: str, status: int | str, detail: str = ""):
        tail = f": {detail}" if detail else ""
        super().__init__(f"{source}: {BLOCKED_MARKER} ({status}){tail}")


class BoardUnreadableError(RuntimeError):
    """The site answered, but not in a shape this adapter reads.

    Our bug or their change — never "this employer has no openings". Raising
    is what stops a format change from being recorded as thousands of empty
    boards.
    """

    def __init__(self, source: str, detail: str = ""):
        tail = f": {detail}" if detail else ""
        super().__init__(f"{source}: {UNREADABLE_MARKER}{tail}")


def listing_signature(entries) -> str:
    """Hash of a board's LISTING: its (stable id, title) pairs.

    The pulse lane stores this for a board once that listing has been fully
    ingested (`CompanyRegistry.poll_hash`). An adapter that pays a detail
    request per posting can compute the same hash from the listing alone and,
    when it equals the stored one, skip every detail request: nothing on the
    board changed since the poll whose details we already hold.
    """
    keys = sorted(f"{i}|{(t or '')[:80]}" for i, t in entries)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def unchanged_since_ingest(scraper) -> bool:
    """True when this adapter's listing is the one the lane already ingested.

    The lane hands the stored hash to the adapter as `known_signature` before
    the fetch (pulse lane only — the other lanes never set it, and always get
    the full fetch). Only a STABLE, non-empty listing can match.
    """
    known = getattr(scraper, "known_signature", None)
    entries = getattr(scraper, "signature_entries", None)
    if not known or not entries or getattr(scraper, "signature_stable", True) is False:
        return False
    return listing_signature(entries) == known


def swallowed(scraper, source: str, exc: Exception) -> list:
    """An adapter is about to answer ``[]`` for a fetch that FAILED: say why.

    Sets ``scraper.last_error`` (read through ``fetch_error``) and returns the
    empty list. Without the reason, a timeout, a 429 or a 503 is recorded as a
    successful poll of an employer with no openings — ``job_count = 0`` and the
    72-hour cadence. Six adapters still did exactly that on 2026-11-15. The
    status is written as digits so a 429 is recognised as a throttle.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        scraper.last_error = f"{source} returned {status}"
    else:
        scraper.last_error = f"{source} unreachable: {type(exc).__name__}: {exc}"[:300]
    return []


def body_hint(text: str, limit: int = 160) -> str:
    """A short, single-line slice of a response body for an error message."""
    try:
        return " ".join((text or "")[:limit].split())
    except Exception:
        return ""


# ── completeness: "this list is the WHOLE board" ─────────────────────────────
# An adapter sets ``fetch_complete = True`` only once it has proof the list it
# returns covers every posting the board lists: the request succeeded, the
# response had the shape we read, no page/item cap was hit and no pagination
# was left unwalked. ANY doubt is ``False``. The pulse lane closes shared rows
# that are absent from a complete fetch (app/discovery/board_absence.py), so a
# complete flag on a capped or failed fetch would close live postings — the
# failure mode is silent and permanent for the user who loses the job.
#
# Two optional attributes refine it for adapters that FILTER or ENRICH the
# listing before returning it (Workday, SmartRecruiters skip non-tech titles
# and fetch each posting's detail): ``listed_ids`` / ``listed_urls`` name every
# posting the listing carried, and ``listing_complete`` says that LISTING was
# whole even when a detail fetch failed. Absence is judged against those, never
# against the filtered list.

_TOTAL_KEYS = ("total", "totalCount", "total_count", "totalFound",
               "totalResults", "total_results")
_MORE_KEYS = ("next", "next_page", "nextPage", "next_url", "nextUrl",
              "hasMore", "has_more", "hasNextPage", "has_next_page")


def listing_truncated(payload, n_items: int) -> bool:
    """True when a listing ENVELOPE says it holds more than it returned.

    Reads the envelope itself and the usual pagination sub-objects
    (``meta``/``pagination``/``links``/``page_info``): a stated total above the
    items we got, or a "next page" marker. A bare list has no envelope and
    says nothing. Used by adapters that request ONE page and assume it is the
    whole board — the assumption this makes checkable rather than silent.
    """
    if not isinstance(payload, dict):
        return False
    scopes = [payload] + [payload[k] for k in ("meta", "pagination", "links",
                                                "page_info", "pageInfo")
                          if isinstance(payload.get(k), dict)]
    for d in scopes:
        for key in _TOTAL_KEYS:
            v = d.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v > n_items:
                return True
        for key in _MORE_KEYS:
            if d.get(key):
                return True
    return False
