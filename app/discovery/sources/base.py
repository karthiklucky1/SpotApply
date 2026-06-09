from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

@dataclass
class DiscoveredCompany:
    name: str
    slug: str
    ats: str  # greenhouse, lever, ashby, workday, workable, etc.
    career_url: Optional[str] = None
    source: str = "unknown"
