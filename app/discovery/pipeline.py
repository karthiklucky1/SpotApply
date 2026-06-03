"""Pipeline that runs all configured scrapers and upserts into the DB."""
from __future__ import annotations

import logging
from typing import List

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Job, JobSource, CompanyRegistry
from app.discovery.ashby import AshbyScraper
from app.discovery.base import RawJob
from app.discovery.greenhouse import GreenhouseScraper
from app.discovery.lever import LeverScraper
from app.discovery.google_search import GoogleSearchDiscovery

log = logging.getLogger(__name__)


def _all_scrapers():
    scrapers = []
    
    # 1. Query active boards from the DB registry
    try:
        with get_session() as session:
            db_companies = session.exec(
                select(CompanyRegistry).where(CompanyRegistry.is_active == True)
            ).all()
            
            for comp in db_companies:
                if comp.ats == JobSource.GREENHOUSE:
                    scrapers.append(GreenhouseScraper(comp.slug))
                elif comp.ats == JobSource.LEVER:
                    scrapers.append(LeverScraper(comp.slug))
                elif comp.ats == JobSource.ASHBY:
                    scrapers.append(AshbyScraper(comp.slug))
    except Exception as e:
        log.warning("Could not load scrapers from CompanyRegistry database: %s. Falling back to .env", e)

    # 2. Fallback / Merge static list from .env if the database returned nothing
    if not scrapers:
        log.info("Registry empty or failed. Seeding scrapers from .env config lists.")
        for slug in settings.greenhouse_boards_list:
            scrapers.append(GreenhouseScraper(slug))
        for slug in settings.lever_boards_list:
            scrapers.append(LeverScraper(slug))
        for slug in settings.ashby_boards_list:
            scrapers.append(AshbyScraper(slug))

    # Deduplicate scrapers by type + slug
    seen = set()
    deduped = []
    for s in scrapers:
        slug_attr = getattr(s, "board_slug", None) or getattr(s, "company_slug", None) or getattr(s, "org_slug", None)
        if slug_attr:
            key = (s.name, slug_attr.lower().strip())
            if key not in seen:
                seen.add(key)
                deduped.append(s)
            
    return deduped


def _upsert(raw_jobs: List[RawJob]) -> int:
    """Insert new jobs; skip duplicates by (source, external_id)."""
    inserted = 0
    with get_session() as session:
        for r in raw_jobs:
            existing = session.exec(
                select(Job).where(
                    Job.source == JobSource(r.source),
                    Job.external_id == r.external_id,
                )
            ).first()
            if existing:
                continue
            session.add(
                Job(
                    source=JobSource(r.source),
                    external_id=r.external_id,
                    company=r.company,
                    title=r.title,
                    location=r.location,
                    remote=r.remote,
                    url=r.url,
                    description=r.description,
                    posted_at=r.posted_at,
                )
            )
            inserted += 1
        session.commit()
    return inserted


def run_discovery() -> int:
    """Run every configured scraper, return total newly inserted jobs."""
    total_new = 0
    
    # 1. Run static scrapers
    for scraper in _all_scrapers():
        try:
            raw = scraper.fetch()
            new = _upsert(raw)
            total_new += new
            log.info("%s: %d new (%d total fetched)", scraper.name, new, len(raw))
        except Exception as e:
            log.exception("Scraper %s failed: %s", scraper.name, e)

    # 2. Run Dynamic Google Search Discovery
    log.info("Starting Dynamic Google Search Discovery (Playwright)...")
    discovery = GoogleSearchDiscovery(keywords=settings.jobs_keywords_list, experience_level="2 years")
    try:
        # Since run_discovery is sync, we use asyncio.run or similar if needed, 
        # but the pipeline is called in a way that allows us to manage the loop.
        import asyncio
        raw = asyncio.run(discovery.fetch_all_discovered())
        new = _upsert(raw)
        total_new += new
        log.info("Google Discovery: %d new jobs found from the web", new)
    except Exception as e:
        log.error("Google Discovery failed: %s", e)

    return total_new


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    n = run_discovery()
    print(f"Inserted {n} new jobs.")
