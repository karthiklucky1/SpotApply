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


def find_champions(company: str, role: str, visa: bool = False, limit: int = 8,
                   school: str | None = None) -> dict:
    """X-Ray Google for public LinkedIn profiles at `company` matching `role`.

    When `school` is given, search alumni instead: people at the company who
    list that school — a warmer intro than a cold role-match."""
    from app.config import settings
    from app.intelligence import contact_research as _cr

    # Not configured is NOT a failure and NOT a zero-latency success: it is a
    # stage that never ran, and Phase 8's report has to be able to say so.
    if not settings.serpapi_key:
        _cr.record(_cr.CONTACT_SEARCH, _cr.NOT_CONFIGURED)
        return {"ok": False, "reason": "serpapi_key_not_set", "people": [],
                "note": "Set SERPAPI_KEY to enable LinkedIn champion search."}
    if not company:
        _cr.record(_cr.CONTACT_SEARCH, _cr.NOT_CONFIGURED)
        return {"ok": False, "reason": "no_company", "people": []}

    visa_clause = (" " + _VISA_TERMS) if visa else ""
    if school:
        query = f'site:linkedin.com/in/ "{company}" "{school}"'.strip()
    else:
        query = f'site:linkedin.com/in/ "{company}" "{role}"{visa_clause}'.strip()

    with _cr.timed(_cr.CONTACT_SEARCH, provider_call=True) as _t:
        return _search(query, company, role, limit, visa, school, _t)


def _search(query: str, company: str, role: str, limit: int, visa: bool,
            school: str | None, _t) -> dict:
    """The SerpAPI round trip. Split out so the caller's `timed` block measures
    exactly the provider call and nothing else."""
    from app.config import settings
    from app.intelligence import contact_research as _cr

    try:
        import httpx
        with httpx.Client(timeout=20.0) as client:
            r = client.get(_SERP_URL, params={
                "engine": "google", "q": query, "num": max(limit, 10),
                "hl": "en", "gl": "us", "api_key": settings.serpapi_key,
            })
        if r.status_code == 401:
            _t.outcome = _cr.NOT_CONFIGURED
            return {"ok": False, "reason": "serpapi_invalid_key", "people": []}
        if r.status_code == 429:
            _t.outcome = _cr.QUOTA
            return {"ok": False, "reason": "serpapi_quota", "people": [],
                    "note": "SerpAPI monthly quota reached."}
        if r.status_code != 200:
            _t.outcome = _cr.FAILED
            return {"ok": False, "reason": f"http_{r.status_code}", "people": []}
        data = r.json()
    except Exception as e:
        # The exception TYPE decides timeout vs failure — the message is the
        # provider's and can say anything.
        _t.outcome = _cr.TIMEOUT if "timeout" in type(e).__name__.lower() else _cr.FAILED
        log.warning("X-Ray search failed (%s)", type(e).__name__)
        return {"ok": False, "reason": type(e).__name__, "people": []}

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

    # "Ran fine, found nobody" is a real and common answer, and filing it as a
    # success is how a readiness report claims a search capability it lacks.
    _t.outcome = _cr.OK if people else _cr.EMPTY
    _t.results = len(people)
    return {"ok": True, "query": query, "company": company, "role": role,
            "visa_biased": visa and not school, "alumni": bool(school),
            "school": school or "", "people": people}
