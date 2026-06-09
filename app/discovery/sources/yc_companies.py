from __future__ import annotations

import logging
from typing import List
import httpx

from app.discovery.sources.base import DiscoveredCompany

log = logging.getLogger(__name__)

class YCCompanySource:
    async def discover(self) -> List[DiscoveredCompany]:
        log.info("YC Company Source: Fetching active hiring startups from YC Directory...")
        url = "https://45n19db2qm-dsn.algolia.net/1/indexes/yc_companies/query"
        params = {
            "x-algolia-agent": "Algolia for JavaScript (4.0.0)",
            "x-algolia-application-id": "45N19DB2QM",
            "x-algolia-api-key": "81c15f02cd8b89ab410d922119eb47fa"
        }
        
        query_body = {
            "query": "",
            "hitsPerPage": 250,
            "facetFilters": ["isHiring:true"]
        }
        
        companies = []
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, params=params, json=query_body)
                if r.status_code == 200:
                    payload = r.json()
                    hits = payload.get("hits", [])
                    log.info("YC Company Source: Algolia returned %d startups.", len(hits))
                    for hit in hits:
                        name = hit.get("name", "")
                        domain = hit.get("website", "")
                        careers_url = hit.get("careers_url", "")
                        if not careers_url and domain:
                            careers_url = f"{domain.rstrip('/')}/careers"
                        
                        if name and domain:
                            companies.append(DiscoveredCompany(
                                name=name,
                                slug=hit.get("slug", name.lower().replace(" ", "")),
                                ats="yc_domain",  # mark as YC domain to be resolved by ATS detector
                                career_url=careers_url,
                                source="yc_directory"
                            ))
                else:
                    log.warning("YC Company Source: Algolia API returned status %d", r.status_code)
        except Exception as e:
            log.warning("YC Company Source: Directory query failed: %s", e)
            
        return companies
