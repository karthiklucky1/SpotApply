"""LinkedIn 'champion finder' via Google X-Ray search (SerpAPI).

We never scrape or log into LinkedIn. We ask Google — which already indexes
public LinkedIn profiles — via the SerpAPI pipeline you already use. Zero
LinkedIn auth, zero account-ban risk, no residential proxies. Returns public
profile URL + name + headline from Google's result snippets.

Used to surface potential referrers / internal champions at a target company
(optionally biased toward people who went through the visa process themselves).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_SERP_URL = "https://serpapi.com/search.json"
_VISA_TERMS = '("F-1" OR "OPT" OR "STEM OPT" OR "H-1B" OR "international student" OR "MS in")'


def _parse_name(title: str) -> str:
    # SerpAPI organic title looks like "Jane Doe - Senior Engineer - Stripe | LinkedIn"
    t = (title or "").split(" | ")[0]
    return t.split(" - ")[0].strip() or "LinkedIn member"


def find_champions(company: str, role: str, visa: bool = False, limit: int = 8) -> dict:
    """X-Ray Google for public LinkedIn profiles at `company` matching `role`."""
    from app.config import settings
    if not settings.serpapi_key:
        return {"ok": False, "reason": "serpapi_key_not_set", "people": [],
                "note": "Set SERPAPI_KEY to enable LinkedIn champion search."}
    if not company:
        return {"ok": False, "reason": "no_company", "people": []}

    visa_clause = (" " + _VISA_TERMS) if visa else ""
    query = f'site:linkedin.com/in/ "{company}" "{role}"{visa_clause}'.strip()

    try:
        import httpx
        with httpx.Client(timeout=20.0) as client:
            r = client.get(_SERP_URL, params={
                "engine": "google", "q": query, "num": max(limit, 10),
                "hl": "en", "gl": "us", "api_key": settings.serpapi_key,
            })
        if r.status_code == 401:
            return {"ok": False, "reason": "serpapi_invalid_key", "people": []}
        if r.status_code == 429:
            return {"ok": False, "reason": "serpapi_quota", "people": [],
                    "note": "SerpAPI monthly quota reached."}
        if r.status_code != 200:
            return {"ok": False, "reason": f"http_{r.status_code}", "people": []}
        data = r.json()
    except Exception as e:
        log.warning("X-Ray search failed: %s", e)
        return {"ok": False, "reason": str(e), "people": []}

    people, seen = [], set()
    for item in data.get("organic_results", []):
        link = item.get("link", "")
        if "linkedin.com/in/" not in link or link in seen:
            continue
        seen.add(link)
        people.append({
            "name": _parse_name(item.get("title", "")),
            "headline": (item.get("snippet", "") or "").strip()[:200],
            "url": link,
        })
        if len(people) >= limit:
            break

    return {"ok": True, "query": query, "company": company, "role": role,
            "visa_biased": visa, "people": people}
