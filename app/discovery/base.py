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

    def fetch(self) -> List[RawJob]:
        ...
