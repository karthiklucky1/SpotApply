from __future__ import annotations

import json
import logging
from typing import List

from app.config import settings
from app.discovery.sources.base import DiscoveredCompany

log = logging.getLogger(__name__)

class BuiltinFallbackSource:
    async def discover(self) -> List[DiscoveredCompany]:
        log.info("Builtin Fallback Source: Loading seed lists from JSON...")
        discovered = []
        if not settings.bootstrap_path.exists():
            log.warning("Builtin Fallback Source: %s does not exist", settings.bootstrap_path)
            return []
            
        try:
            with open(settings.bootstrap_path, "r", encoding="utf-8") as f:
                bootstrap = json.load(f)
            for ats_type, slugs in bootstrap.items():
                for slug in slugs:
                    slug = slug.strip().lower()
                    if not slug:
                        continue
                    career_url = None
                    if ats_type == "greenhouse":
                        career_url = f"https://boards.greenhouse.io/{slug}"
                    elif ats_type == "lever":
                        career_url = f"https://jobs.lever.co/{slug}"
                    elif ats_type == "ashby":
                        career_url = f"https://jobs.ashbyhq.com/{slug}"
                        
                    discovered.append(DiscoveredCompany(
                        name=slug.capitalize(),
                        slug=slug,
                        ats=ats_type,
                        career_url=career_url,
                        source="seed"
                    ))
        except Exception as e:
            log.error("Builtin Fallback Source: Failed to load bootstrap JSON: %s", e)
            
        return discovered
