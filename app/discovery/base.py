"""Base scraper protocol — every source returns a list of normalized Job dicts."""
from __future__ import annotations

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
