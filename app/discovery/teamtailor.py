"""Teamtailor public RSS: https://{slug}.teamtailor.com/jobs.rss

Teamtailor is the leading Nordics ATS. Every careers site can expose a public
jobs.rss feed with pubDate — cheap near-real-time freshness for Scandinavian /
European coverage. (RSS must be enabled by the tenant; a 404 raises so the
pipeline retires the dead slug — the registry validator can revive false positives.)

LOCATION IS NOT GUESSED FROM THE TITLE. The feed's `<title>` is often
"Role - Something", and "Something" is whatever the tenant put there: a city
for some boards, a DEPARTMENT for others. This scraper used to split on the
dash and store the suffix as the location, so a "Backend Engineer - POS
Integrations" posting was filed under a city called "POS Integrations", and a
US user was shown Paris. Now the feed's own location elements are read when
present (`<location>` in any namespace, as some tenants emit), and otherwise
the location is left BLANK — which the geography pass treats as "not stated"
and resolves from the posting's public page (schema.org JobPosting carries
`jobLocation`), never from a title.
"""
from __future__ import annotations

import logging
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import List
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import GeoEvidence, RawJob

log = logging.getLogger(__name__)


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def _local(tag: str) -> str:
    """'location' from '{http://…}location' or 'teamtailor:location'."""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _feed_geo(item) -> GeoEvidence | None:
    """Only what the feed ITEM states about location. Nothing is derived."""
    sites: List[str] = []
    country = ""
    work_mode = ""
    fields: List[str] = []
    for child in list(item):
        name = _local(child.tag)
        text = (child.text or "").strip()
        if not text:
            continue
        if name in ("location", "locations", "city", "office"):
            for part in re.split(r"\s*[|;/·]\s*|\s*,\s*(?=[A-Z])", text):
                p = part.strip()
                if p and p not in sites:
                    sites.append(p)
            fields.append(name)
        elif name in ("country", "countryname"):
            country = text
        elif name in ("remotestatus", "remote_status", "workplace", "workplacetype"):
            low = text.lower()
            work_mode = ("hybrid" if "hybrid" in low else "remote" if "remote" in low
                         else "onsite" if ("site" in low or "office" in low) else "")
    if not (sites or country or work_mode):
        return None
    return GeoEvidence(sites=sites, sites_field="+".join(dict.fromkeys(fields)) or "",
                       country=country, country_field="country" if country else "",
                       work_mode=work_mode, work_mode_field="remoteStatus" if work_mode else "")


class TeamtailorScraper:
    name = "teamtailor"

    def __init__(self, board_slug: str):
        self.board_slug = board_slug

    def fetch(self) -> List[RawJob]:
        url = f"https://{self.board_slug}.teamtailor.com/jobs.rss"
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Permanent statuses mean the slug is wrong, gone, or private — let
            # the exception propagate so the discovery pipeline's dead-board
            # recorder retires the registry row (these long-tail ATSes are NOT
            # covered by the Greenhouse/Lever/Ashby validation loop, so a junk
            # slug would otherwise 404 on every cycle forever).
            if e.response is not None and e.response.status_code in (401, 403, 404, 410):
                raise
            log.warning("Teamtailor fetch failed for %s: %s", self.board_slug, e)
            return []
        except httpx.HTTPError as e:
            log.warning("Teamtailor fetch failed for %s: %s", self.board_slug, e)
            return []

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            log.warning("Teamtailor[%s] bad RSS: %s", self.board_slug, e)
            return []

        jobs: List[RawJob] = []
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            if not link or not title:
                continue
            # external id = trailing numeric/slug segment of the job URL
            ext_id = re.sub(r"[^a-zA-Z0-9_-]", "", link.rstrip("/").split("/")[-1]) or link
            posted_dt = None
            pub = item.findtext("pubDate")
            if pub:
                try:
                    dt = parsedate_to_datetime(pub)
                    posted_dt = dt.astimezone(timezone.utc) if dt else None
                except (TypeError, ValueError):
                    pass
            desc = _strip_html(item.findtext("description") or "")
            geo = _feed_geo(item)
            location = " · ".join(geo.sites) if geo and geo.sites else ""
            # `remote` is a flag beside the location: a stated remote status,
            # or the word in the location the feed gave. The title and the
            # description are NOT consulted — "remote" in a JD paragraph
            # ("occasional remote work") is not a remote posting.
            remote = bool(geo and geo.work_mode == "remote") or "remote" in location.lower()
            jobs.append(
                RawJob(
                    source="teamtailor",
                    external_id=str(ext_id),
                    company=self.board_slug.replace("-", " ").title(),
                    title=title,
                    location=location,
                    remote=remote,
                    url=link,
                    description=desc,
                    posted_at=posted_dt,
                    origin="teamtailor",
                    geo=geo,
                )
            )
        log.info("Teamtailor[%s]: %d jobs", self.board_slug, len(jobs))
        return jobs
