"""Search Google for Greenhouse/Lever/Ashby job boards matching user keywords.
Uses Playwright to bypass simple bot detection.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Set
from urllib.parse import urlparse

from playwright.async_api import async_playwright
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
        import urllib.parse
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://www.google.com/search?q={encoded_query}"
        links = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                # Set a real user agent at context level
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000) # wait for results
                
                # Extract all result links
                hrefs = await page.evaluate('''() => {
                    return Array.from(document.querySelectorAll('a'))
                        .map(a => a.href)
                        .filter(h => h.startsWith('http'));
                }''')
                links = hrefs
                await browser.close()
            return links
        except Exception as e:
            log.warning("Google Playwright search failed for query '%s': %s", query, e)
            return []

    async def discover_slugs(self) -> dict[str, Set[str]]:
        """Finds board slugs for Greenhouse, Lever, and Ashby."""
        found = {
            "greenhouse": set(),
            "lever": set(),
            "ashby": set()
        }
        
        # Search queries: combined queries search Greenhouse, Lever, and Ashby simultaneously
        queries = [
            f'(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com) "{k}" "{self.experience_level}"'
            for k in self.keywords
        ]

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
        """Discovers slugs and then fetches jobs from all of them."""
        slugs = await self.discover_slugs()
        all_jobs = []
        
        for board_type, board_slugs in slugs.items():
            log.info("Discovered %d %s boards: %s", len(board_slugs), board_type, list(board_slugs))
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
