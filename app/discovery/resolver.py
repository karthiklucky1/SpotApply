from __future__ import annotations

import logging
import re
from typing import Optional, Tuple, List
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

ATS_PATTERNS = [
    ("greenhouse", r"boards\.greenhouse\.io/([^/?#\s]+)"),
    ("lever", r"jobs\.lever\.co/([^/?#\s]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([^/?#\s]+)"),
    ("workday", r"(?:https?:)?(?://)?([^.]+)\.myworkdayjobs\.com"),
    ("smartrecruiters", r"smartrecruiters\.com/([^/?#\s]+)"),
    ("workable", r"apply\.workable\.com/([^/?#\s]+)"),
    ("recruitee", r"(?:https?:)?(?://)?([^.]+)\.recruitee\.com"),
    ("personio", r"(?:https?:)?(?://)?([^.]+)\.jobs\.personio\.(?:de|com)"),
    ("bamboohr", r"(?:https?:)?(?://)?([^.]+)\.bamboohr\.com/jobs"),
    ("icims", r"(?:https?:)?(?://)?([^.]+)\.icims\.com"),
    ("jobvite", r"jobvite\.com/([^/?#\s]+)"),
    ("comeet", r"comeet\.co/([^/?#\s]+)"),
    ("teamtailor", r"(?:https?:)?(?://)?([^.]+)\.teamtailor\.com")
]

class ATSDetector:
    @staticmethod
    def detect_from_url(url: str) -> Optional[Tuple[str, str]]:
        """Check if the URL itself points directly to a known ATS system."""
        url_lower = url.lower()
        for ats_name, pattern in ATS_PATTERNS:
            match = re.search(pattern, url_lower)
            if match:
                slug = match.group(1).strip()
                if slug and slug not in ["search", "embed", "careers", "jobs", "v1", "postings", "job-board"]:
                    return ats_name, slug
        return None

    @staticmethod
    def detect_from_html(html_content: str, base_url: str) -> Optional[Tuple[str, str]]:
        """Scan HTML content for outbound links pointing to known ATS endpoints."""
        try:
            soup = BeautifulSoup(html_content, "html.parser")
        except Exception:
            return None
        
        # Check all anchor links
        for a in soup.find_all("a", href=True):
            href = a["href"]
            absolute_url = urljoin(base_url, href)
            detected = ATSDetector.detect_from_url(absolute_url)
            if detected:
                return detected
                
        # Check iframes
        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"]
            absolute_src = urljoin(base_url, src)
            detected = ATSDetector.detect_from_url(absolute_src)
            if detected:
                return detected
                
        # Fallback regex search on raw HTML text
        for ats_name, pattern in ATS_PATTERNS:
            match = re.search(pattern, html_content.lower())
            if match:
                slug = match.group(1).strip()
                if slug and slug not in ["search", "embed", "careers", "jobs", "v1", "postings", "job-board"]:
                    return ats_name, slug
                    
        return None

class CareerResolver:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )

    async def resolve_careers_url(self, homepage_url: str) -> List[str]:
        """Verify the homepage and generate candidate career paths."""
        if not homepage_url.startswith("http"):
            homepage_url = "https://" + homepage_url
            
        parsed = urlparse(homepage_url)
        base_origin = f"{parsed.scheme}://{parsed.netloc}"
        
        # Candidate career links to try
        candidates = [
            homepage_url,
            urljoin(base_origin, "/careers"),
            urljoin(base_origin, "/jobs"),
            urljoin(base_origin, "/join"),
            urljoin(base_origin, "/open-positions")
        ]
        return candidates

    async def resolve_ats(self, homepage_url: str) -> Optional[Tuple[str, str, str]]:
        """Probe a company homepage / careers pages and return (ats_type, slug, resolved_url)."""
        candidates = await self.resolve_careers_url(homepage_url)
        
        # 1. First, check if any of the candidate URLs directly contain the ATS info
        for url in candidates:
            detected = ATSDetector.detect_from_url(url)
            if detected:
                return detected[0], detected[1], url
                
        # 2. Query each candidate page and inspect HTML content
        for url in candidates:
            try:
                log.debug("Probing candidate careers page: %s", url)
                r = await self.client.get(url)
                if r.status_code == 200:
                    # Check if the final redirected URL points directly to an ATS
                    final_url = str(r.url)
                    detected = ATSDetector.detect_from_url(final_url)
                    if detected:
                        return detected[0], detected[1], final_url
                        
                    # Parse HTML content
                    detected = ATSDetector.detect_from_html(r.text, final_url)
                    if detected:
                        return detected[0], detected[1], final_url
            except Exception as e:
                log.debug("Failed to probe URL %s: %s", url, e)
                
        return None

    async def close(self):
        await self.client.aclose()
