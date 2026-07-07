"""Automated ATS Registry Harvester.

Fetches hiring startups from YC directory, detects their ATS boards (Greenhouse,
Lever, Ashby) via CareerResolver, and registers active boards in CompanyRegistry.
"""
from __future__ import annotations

import logging
import asyncio
from typing import List

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import CompanyRegistry, JobSource
from app.discovery.sources.yc_companies import YCCompanySource
from app.discovery.resolver import CareerResolver

log = logging.getLogger(__name__)


async def run_harvester(limit: int = 15) -> int:
    """Queries active hiring startups, detects their ATS boards, and registers them.

    Limited to `limit` resolutions per run to keep network overhead low.
    """
    log.info("Starting Automated ATS Registry Harvester...")
    
    # 1. Fetch YC hiring companies
    source = YCCompanySource()
    discovered = await source.discover()
    if not discovered:
        log.info("Registry Harvester: No companies discovered.")
        return 0

    # 2. Filter out already registered slugs or domains
    active_slugs = set()
    with get_session() as session:
        registered = session.exec(select(CompanyRegistry)).all()
        for r in registered:
            active_slugs.add(r.slug.strip().lower())

    to_probe = []
    for c in discovered:
        slug = c.slug.strip().lower()
        if slug in active_slugs:
            continue
        to_probe.append(c)

    log.info("Registry Harvester: Found %d new startups to probe.", len(to_probe))
    if not to_probe:
        return 0

    # 3. Limit concurrency to prevent hitting rate limits
    to_probe = to_probe[:limit]
    
    resolver = CareerResolver()
    added_count = 0
    
    try:
        for comp in to_probe:
            homepage = comp.career_url or ""
            if not homepage or not homepage.startswith("http"):
                homepage = f"https://{comp.slug}.com" # fallback
                
            log.info("Registry Harvester: Probing ATS for '%s' (%s)...", comp.name, homepage)
            
            try:
                res = await resolver.resolve_ats(homepage)
                if res:
                    ats_type, slug, final_url = res
                    # Verify it matches our supported job sources
                    try:
                        ats_enum = JobSource(ats_type.lower().strip())
                        
                        # Double-check it doesn't already exist
                        with get_session() as session:
                            existing = session.exec(
                                select(CompanyRegistry).where(
                                    CompanyRegistry.slug == slug.lower().strip(),
                                    CompanyRegistry.ats == ats_enum
                                )
                            ).first()
                            
                            if not existing:
                                session.add(CompanyRegistry(
                                    company_name=comp.name,
                                    slug=slug.lower().strip(),
                                    ats=ats_enum,
                                    career_url=final_url,
                                    source="harvester_discovered",
                                    is_active=True
                                ))
                                session.commit()
                                log.info("Registry Harvester: Successfully registered '%s' on %s (slug: %s)", comp.name, ats_type, slug)
                                added_count += 1
                    except ValueError:
                        log.debug("Registry Harvester: Unsupported ATS '%s' discovered for %s", ats_type, comp.name)
            except Exception as e:
                log.debug("Registry Harvester: Resolution failed for %s: %s", comp.name, e)
                
    finally:
        await resolver.close()

    log.info("Registry Harvester: Completed. Registered %d new company boards.", added_count)
    return added_count
