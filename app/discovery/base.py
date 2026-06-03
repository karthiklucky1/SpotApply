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


class Scraper(Protocol):
    name: str

    def fetch(self) -> List[RawJob]:
        ...
