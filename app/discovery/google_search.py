"""Search Google for Greenhouse/Lever/Ashby job boards matching user keywords.
Uses Playwright to bypass simple bot detection.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Set
from urllib.parse import urlparse

# No direct Playwright import — see app.common.browser_client.
from bs4 import BeautifulSoup

from app.discovery.base import RawJob
from app.discovery.greenhouse import GreenhouseScraper
from app.discovery.lever import LeverScraper
from app.discovery.ashby import AshbyScraper

log = logging.getLogger(__name__)

class GoogleSearchDiscovery:
    """Discovers new job boards by searching Google for keywords using Playwright."""
    
    def __init__(self, keywords: List[str], experience_level: str = "4 years"):
        self.keywords = keywords
        self.experience_level = experience_level

    async def _search_google_playwright(self, query: str) -> List[str]:
        """Returns a list of URLs found on Google for the query using Playwright."""
        # Routed through app.common.browser_client — the browser service when
        # BROWSER_SERVICE_URL is set, otherwise a local launch behind the
        # browser_slot gate. search_links() already returns [] on failure
        # (a bot-walled search engine is "found nothing this pass", not an error).
        from app.common.browser_client import search_links
        return await search_links(query, engine="google",
                                  timeout_ms=30000, settle_ms=2000)

    async def discover_slugs(self) -> dict[str, Set[str]]:
        """Finds board slugs for Greenhouse, Lever, and Ashby."""
        found = {
            "greenhouse": set(),
            "lever": set(),
            "ashby": set()
        }
        
        # Search queries: combined queries search Greenhouse, Lever, and Ashby simultaneously
        base_queries = []
        for k in self.keywords:
            base_queries.append(f'(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com) "{k}" "United States"')
            base_queries.append(f'(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com) "{k}" "remote"')
        base_queries.append('(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com) "AI Engineer" OR "Machine Learning" "US"')

        # Sample a subset of queries to be polite to Google and avoid rate limiting
        import random
        random.shuffle(base_queries)
        queries = base_queries[:5]

        for q in queries:
            urls = await self._search_google_playwright(q)
            for u in urls:
                if "boards.greenhouse.io/" in u:
                    # https://boards.greenhouse.io/company/jobs/123
                    path = u.split("boards.greenhouse.io/")[1]
                    slug = path.split("/")[0]
                    if slug and slug not in ["search", "embed"]:
                        found["greenhouse"].add(slug)
                elif "jobs.lever.co/" in u:
                    # https://jobs.lever.co/company/uuid
                    path = u.split("jobs.lever.co/")[1]
                    slug = path.split("/")[0]
                    if slug:
                        found["lever"].add(slug)
                elif "jobs.ashbyhq.com/" in u:
                    path = u.split("jobs.ashbyhq.com/")[1]
                    slug = path.split("/")[0]
                    if slug:
                        found["ashby"].add(slug)
        
        return found

    async def fetch_all_discovered(self) -> List[RawJob]:
        """Discovers slugs, saves them to CompanyRegistry, and then fetches jobs."""
        slugs = await self.discover_slugs()
        all_jobs = []
        
        from app.db.init_db import get_session
        from app.db.models import CompanyRegistry, JobSource
        from sqlmodel import select

        # Save discovered slugs to database
        with get_session() as session:
            for board_type, board_slugs in slugs.items():
                try:
                    ats_source = JobSource(board_type)
                except ValueError:
                    continue
                for slug in board_slugs:
                    slug = slug.strip().lower()
                    if not slug:
                        continue
                    existing = session.exec(
                        select(CompanyRegistry).where(
                            CompanyRegistry.slug == slug,
                            CompanyRegistry.ats == ats_source
                        )
                    ).first()
                    if not existing:
                        log.info("Registering newly discovered company board: %s (%s)", slug, board_type)
                        session.add(
                            CompanyRegistry(
                                slug=slug,
                                ats=ats_source,
                                is_active=True,
                                source="google_discovery"
                            )
                        )
            session.commit()
        
        for board_type, board_slugs in slugs.items():
            log.info("Scraping discovered %d %s boards: %s", len(board_slugs), board_type, list(board_slugs))
            for slug in board_slugs:
                try:
                    if board_type == "greenhouse":
                        scraper = GreenhouseScraper(slug)
                    elif board_type == "lever":
                        scraper = LeverScraper(slug)
                    else:
                        scraper = AshbyScraper(slug)
                    
                    jobs = scraper.fetch()
                    all_jobs.extend(jobs)
                except Exception as e:
                    log.warning("Failed to fetch from discovered board %s/%s: %s", board_type, slug, e)
                    
        return all_jobs
