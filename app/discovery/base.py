"""Base scraper protocol — every source returns a list of normalized Job dicts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Protocol


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
    # When THIS posting first entered SpotApply anywhere — not when this copy of
    # the row was written. Scrapers leave it None (they ARE the first sighting);
    # only the copiers set it: adoption and the pulse lane's per-user route move
    # a posting that already exists in the shared pool, and re-stamping
    # first_seen=now there restarted the whole KNOWN-age clock. A three-week-old
    # shared row entered a user's board labelled "New", inside the 5-day scoring
    # window, and the "be one of the first to apply" promise was measured from
    # the moment we copied a DB row. See app/common/freshness.py.
    first_seen: Optional[datetime] = None


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
