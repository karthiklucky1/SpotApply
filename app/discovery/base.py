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
    # ── provenance (see the comment on Job.origin) ──
    # The discovery module that produced this row, when it differs from the
    # `source` routing bucket. Defaults to `source` at write time.
    origin: Optional[str] = None
    # The board the posting actually lives on, when the producer is an
    # aggregator that knows it (SerpAPI's `via`).
    origin_provider: Optional[str] = None
    # ── hiring context read straight out of the response, zero extra calls ──
    # Keys are JobHiringContext column names: department, team, division,
    # hiring_entity, recruiting_agency, requisition_id, reporting_title,
    # reporting_manager_name, recruiter_name, posting_creator_name,
    # contact_email. Empty for sources that expose nothing.
    context: Dict[str, ContextField] = field(default_factory=dict)


class Scraper(Protocol):
    name: str

    def fetch(self) -> List[RawJob]:
        ...
