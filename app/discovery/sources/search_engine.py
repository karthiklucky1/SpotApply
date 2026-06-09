from __future__ import annotations

import logging
from typing import List, Set
from urllib.parse import urlparse
import httpx

from app.config import settings
from app.discovery.sources.base import DiscoveredCompany

log = logging.getLogger(__name__)

class SearchEngineSource:
    def __init__(self, keywords: List[str]):
        self.keywords = keywords

    async def _query_tavily(self, query: str) -> List[str]:
        log.info("Search Engine: Querying Tavily for: '%s'", query)
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": settings.tavily_api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": 20
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, json=payload)
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    return [res.get("url", "") for res in results if res.get("url")]
        except Exception as e:
            log.warning("Search Engine: Tavily query failed: %s", e)
        return []

    async def _query_exa(self, query: str) -> List[str]:
        log.info("Search Engine: Querying Exa for: '%s'", query)
        url = "https://api.exa.ai/search"
        headers = {
            "x-api-key": settings.exa_api_key,
            "content-type": "application/json"
        }
        payload = {
            "query": query,
            "useAutoprompt": True,
            "numResults": 20
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(url, headers=headers, json=payload)
                if r.status_code == 200:
                    results = r.json().get("results", [])
                    return [res.get("url", "") for res in results if res.get("url")]
        except Exception as e:
            log.warning("Search Engine: Exa query failed: %s", e)
        return []

    async def _query_playwright(self, query: str) -> List[str]:
        log.info("Search Engine: Querying Google via Playwright for: '%s'", query)
        from playwright.async_api import async_playwright
        import urllib.parse
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://www.google.com/search?q={encoded_query}"
        links = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1500)
                hrefs = await page.evaluate('''() => {
                    return Array.from(document.querySelectorAll('a'))
                        .map(a => a.href)
                        .filter(h => h.startsWith('http'));
                }''')
                links = hrefs
                await browser.close()
            return links
        except Exception as e:
            log.warning("Search Engine: Playwright Google search failed: %s", e)
            return []

    async def discover(self) -> List[DiscoveredCompany]:
        queries = []
        kw_str = " OR ".join(f'"{k}"' for k in self.keywords[:3])
        queries.append(f'site:boards.greenhouse.io ({kw_str}) "United States"')
        queries.append(f'site:jobs.lever.co ({kw_str}) "United States"')
        queries.append(f'site:jobs.ashbyhq.com ({kw_str}) "United States"')
        
        import random
        random.shuffle(queries)
        
        urls = []
        for q in queries[:2]:
            if settings.tavily_api_key:
                res_urls = await self._query_tavily(q)
            elif settings.exa_api_key:
                natural_q = f"Greenhouse or Lever job listings for {self.keywords[0]} in United States"
                res_urls = await self._query_exa(natural_q)
            else:
                res_urls = await self._query_playwright(q)
            urls.extend(res_urls)

        discovered = []
        seen = set()
        for u in urls:
            parsed = urlparse(u)
            host = parsed.netloc.lower()
            
            ats_type = None
            slug = None
            
            if "boards.greenhouse.io" in host:
                ats_type = "greenhouse"
                parts = [p for p in parsed.path.split("/") if p]
                if parts and parts[0] not in ["search", "embed"]:
                    slug = parts[0]
            elif "jobs.lever.co" in host:
                ats_type = "lever"
                parts = [p for p in parsed.path.split("/") if p]
                if parts:
                    slug = parts[0]
            elif "jobs.ashbyhq.com" in host:
                ats_type = "ashby"
                parts = [p for p in parsed.path.split("/") if p]
                if parts:
                    slug = parts[0]
            
            if ats_type and slug:
                key = (ats_type, slug)
                if key not in seen:
                    seen.add(key)
                    discovered.append(DiscoveredCompany(
                        name=slug.capitalize(),
                        slug=slug,
                        ats=ats_type,
                        career_url=u,
                        source="search_engine"
                    ))
        
        return discovered
