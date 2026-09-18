"""Ashby public job board API.

Ashby exposes a public JSON endpoint at:
  https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true

(Some Ashby orgs use a GraphQL endpoint; the REST one is more stable.)

Per posting the response carries: id, title, department, team, employmentType,
location (string), secondaryLocations [{location, address}], address
{postalAddress {addressLocality, addressRegion, addressCountry}}, isRemote,
isListed, publishedAt, jobUrl, applyUrl, descriptionHtml, descriptionPlain and
(with includeCompensation) compensation {compensationTierSummary,
scrapeableCompensationSalarySummary, compensationTiers, summaryComponents}.

There is NO `locationName`. This scraper read that key for months, so every
Ashby posting was stored with a BLANK location and `remote = isRemote`: in a
31-card audit sample (2026-09-17) 11 cards were Ashby rows with no location,
while every one of those postings named a city on the page (OpenAI San
Francisco hybrid, Skydio San Mateo hybrid — both stored as "remote, location
unknown"). A blank location passes every country gate, so a US user was
delivered Warsaw, London, Vilnius/Kaunas and "Remote - Poland" postings, and
the scoring prompt saw an empty "Location:" line and never mentioned it.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from app.common.geo import detect_country
from app.discovery.base import (
    EVIDENCE_ORG_ENTITY,
    EVIDENCE_TEAM_OR_DEPARTMENT,
    GeoEvidence,
    RawJob,
)
from app.discovery.hiring_context import put

log = logging.getLogger(__name__)

BASE = "https://api.ashbyhq.com/posting-api/job-board"

# A multi-site posting names every site so the country gate sees every
# country; past this many secondary sites the string says "+N more" instead of
# growing without bound (the audit saw one posting with 28).
_SECONDARY_LOCATION_CAP = 4
# Job.salary_text is rendered at 44 chars; 120 keeps the whole Ashby summary
# ("$160K – $200K • Offers Equity") without letting a free-text tier list in.
_SALARY_TEXT_CAP = 120


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def _postal_text(address) -> str:
    """'Locality, Region, Country' from an Ashby `address`, or ''.

    Accepts the documented `{postalAddress: {...}}` wrapper and, defensively,
    a flattened `{addressLocality: ...}` object. Repeated values (a city-state
    like Singapore lists the same name three times) appear once.
    """
    if not isinstance(address, dict):
        return ""
    postal = address.get("postalAddress")
    if not isinstance(postal, dict):
        postal = address
    parts: List[str] = []
    for key in ("addressLocality", "addressRegion", "addressCountry"):
        value = postal.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if value.lower() not in {p.lower() for p in parts}:
                parts.append(value)
    return ", ".join(parts)


def _site_text(node) -> str:
    """Location text for one site node (the posting itself, or one entry of
    `secondaryLocations` — both are shaped `{location, address}`).

    The `location` string is what Ashby shows on the page, so it leads. When it
    is blank the postal address stands in for it (Skydio's postings carry only
    the address). When it is present but names no country we can detect
    ("San Francisco"), the region and country from the postal address are
    appended so the gate — and the scoring prompt — see the country too. A
    string that already resolves ("Warsaw", "London, UK") is left as written:
    appending "England, United Kingdom" to "London, UK" adds nothing but noise.
    """
    if not isinstance(node, dict):
        return ""
    name = node.get("location")
    name = name.strip() if isinstance(name, str) else ""
    postal = _postal_text(node.get("address"))
    if not name:
        return postal
    if postal and not detect_country(name):
        lowered = name.lower()
        extra = [p for p in postal.split(", ") if p.lower() not in lowered]
        if extra:
            name = ", ".join([name] + extra)
    return name


def _sites_of(j: dict) -> List[str]:
    """Every distinct site of the posting, primary first, UNTRUNCATED."""
    primary = _site_text(j)
    if not primary:
        # Not an Ashby field, but harmless as a last resort should a payload
        # from another shape (GraphQL, a cached copy) ever carry it.
        legacy = j.get("locationName")
        primary = legacy.strip() if isinstance(legacy, str) else ""
    secondary = j.get("secondaryLocations")
    secondary = secondary if isinstance(secondary, list) else []
    parts: List[str] = []
    seen: set = set()
    for text in [primary] + [_site_text(s) for s in secondary]:
        if text and text.lower() not in seen:
            parts.append(text)
            seen.add(text.lower())
    return parts


def _location_of(j: dict) -> str:
    """The DISPLAY string: ' · '-joined sites, capped with "+N more". The gate
    does not read this — it reads `_geo_of`, which keeps every site — so a US
    site hidden past the display cap still counts."""
    parts = list(_sites_of(j))
    if len(parts) > 1 + _SECONDARY_LOCATION_CAP:
        hidden = len(parts) - (1 + _SECONDARY_LOCATION_CAP)
        parts = parts[: 1 + _SECONDARY_LOCATION_CAP]
        parts[-1] = f"{parts[-1]} +{hidden} more"
    return " · ".join(parts)


def _country_of(node) -> str:
    """The postal country of one site node, verbatim ('US', 'Poland'), or ''."""
    if not isinstance(node, dict):
        return ""
    address = node.get("address")
    if not isinstance(address, dict):
        return ""
    postal = address.get("postalAddress")
    postal = postal if isinstance(postal, dict) else address
    value = postal.get("addressCountry")
    return value.strip() if isinstance(value, str) else ""


def _geo_of(j: dict) -> Optional[GeoEvidence]:
    """Structured evidence for the eligibility gate: every site (no cap), the
    primary site's postal country, and `isRemote` as the work mode."""
    sites = _sites_of(j)
    country = _country_of(j)
    remote = bool(j.get("isRemote"))
    if not (sites or country or remote):
        return None
    return GeoEvidence(
        sites=sites, sites_field="location+secondaryLocations[].location",
        country=country, country_field="address.postalAddress.addressCountry" if country else "",
        work_mode="remote" if remote else "", work_mode_field="isRemote" if remote else "",
    )


def _salary_of(j: dict) -> Optional[str]:
    """The pay summary Ashby publishes, or None when the org shows none.

    `compensationTierSummary` is the human summary shown on the posting
    ("$160K – $200K • Offers Equity"); `scrapeableCompensationSalarySummary`
    is the machine-readable form Ashby publishes for aggregators. Either is
    the employer's own statement, which is why it wins over the regex facet
    that guesses from the description (app/discovery/pipeline.py `_build_job`).
    """
    comp = j.get("compensation")
    if not isinstance(comp, dict):
        return None
    for key in ("compensationTierSummary", "scrapeableCompensationSalarySummary"):
        value = comp.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:_SALARY_TEXT_CAP]
    return None


class AshbyScraper:
    name = "ashby"

    def __init__(self, org_slug: str):
        self.org_slug = org_slug

    def fetch(self) -> List[RawJob]:
        url = f"{BASE}/{self.org_slug}?includeCompensation=true"
        self.last_error = ""
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as e:
            # See app/discovery/base.py: an empty list with no last_error is a
            # genuinely empty board; one with a last_error is a failed poll.
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("Ashby fetch failed for %s: %s", self.org_slug, e)
            return []

        payload = r.json()
        jobs: List[RawJob] = []
        unlisted = 0
        for j in payload.get("jobs", []):
            # `isListed: false` is a posting the org has taken off its public
            # board (kept for direct-link applicants). Not public → not ours.
            # Absent means listed: the field is newer than some tenants.
            if j.get("isListed") is False:
                unlisted += 1
                continue
            location = _location_of(j)
            # `isRemote` is a flag BESIDE the location, never instead of it: a
            # remote-friendly posting anchored to a city ("San Francisco,
            # hybrid") is still a San Francisco posting for the country gate.
            remote = bool(j.get("isRemote")) or "remote" in location.lower()
            # Ashby's posting API documents this as publishedAt, and our own
            # job-check consumer already reads publishedAt
            # (app/intelligence/job_check.py). This scraper alone read
            # publishedDate, so it was silently producing posted_at=None and
            # every Ashby job fell back to discovered_at for freshness ranking.
            # Try both rather than trading one guess for another.
            published = j.get("publishedAt") or j.get("publishedDate")
            try:
                posted_dt = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
            except Exception:
                posted_dt = None
            # Org unit, already in this response — no extra request. Ashby names
            # both, and they are genuinely different levels: `department` is the
            # function ("Engineering"), `team` the squad ("Applied AI").
            ctx: dict = {}
            put(ctx, "department", j.get("department"),
                EVIDENCE_TEAM_OR_DEPARTMENT, "department")
            put(ctx, "team", j.get("team"), EVIDENCE_TEAM_OR_DEPARTMENT, "team")
            put(ctx, "ats", "ashby", EVIDENCE_ORG_ENTITY, "scraper")
            jobs.append(
                RawJob(
                    source="ashby",
                    external_id=j["id"],
                    company=self.org_slug.replace("-", " ").replace("_", " ").title(),
                    title=j.get("title", ""),
                    location=location,
                    remote=remote,
                    url=j.get("jobUrl") or j.get("applyUrl") or "",
                    description=(_strip_html(j.get("descriptionHtml") or "")
                                 or (j.get("descriptionPlain") or "").strip()),
                    posted_at=posted_dt,
                    origin="ashby",
                    context=ctx,
                    salary_text=_salary_of(j),
                    geo=_geo_of(j),
                )
            )
        log.info("Ashby[%s]: %d jobs (%d unlisted skipped)",
                 self.org_slug, len(jobs), unlisted)
        return jobs
