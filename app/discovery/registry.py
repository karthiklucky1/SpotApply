from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import List, Set
import httpx
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import CompanyRegistry, JobSource

log = logging.getLogger(__name__)

# Pre-curated top AI and tech startup boards list to bootstrap and serve as fallback
BOOTSTRAP_GREENHOUSE = [
    "anthropic", "databricks", "huggingface", "scale", "stripe", "airbnb", "pinterest", "dropbox", "twitch", "discord",
    "figma", "notion", "openai", "cohere", "plaid", "brex", "ramp", "rippling", "gusto", "cloudflare", "doordash",
    "instacart", "reddit", "vimeo", "wayfair", "zillow", "asana", "affirm", "chime", "coinbase", "kraken", "gemini",
    "canva", "atlassian", "hashicorp", "datadog", "snowflake", "confluent", "mongodb", "elastic", "twilio", "okta",
    "crowdstrike", "zscaler", "fortinet", "splunk", "newrelic", "dynatrace", "appdynamics", "mulesoft", "workday",
    "servicenow", "salesforce", "hubspot", "zendesk", "intercom", "gong", "outreach", "box", "smartsheet", "airtable",
    "monday", "clickup", "zoom", "ringcentral", "docusign", "lyft", "uber", "snap", "roku", "peloton", "robinhood",
    "sofi", "wealthfront", "betterment", "klarna", "afterpay", "block", "square", "character", "perplexity", "midjourney",
    "elevenlabs", "mistral", "suno", "runway", "stability", "pinecone", "weaviate", "chroma", "qdrant", "milvus",
    "luma", "replicate", "together", "anyscale", "modal", "baseten", "deepgram", "assemblyai", "cursor", "sourcegraph",
    "codeium", "supermaven", "fireworksai", "lightningai", "cerebrassystems"
]

BOOTSTRAP_LEVER = [
    "shieldai", "zoox", "highspot", "immuta", "sonatype", "sysdig", "wandb",
]

BOOTSTRAP_ASHBY = [
    "linear", "vercel", "replit", "supabase", "railway", "render", "vultr", "loom", "mural", "miro",
    "glide", "bubble", "ramp", "notion", "plaid", "cohere", "writer", "langchain", "browserbase",
    "abridge", "anyscale", "baseten", "openai", "perplexity", "cursor", "pinecone", "weaviate",
    "neon", "prefect", "airbyte", "hightouch", "posthog", "fullstory", "statsig", "sanity",
]

def seed_registry() -> int:
    """Read the .env configured boards and the bootstrap lists, and insert them if not present."""
    count = 0
    # Collect all sources
    greenhouse_slugs = set(settings.greenhouse_boards_list + BOOTSTRAP_GREENHOUSE)
    lever_slugs = set(settings.lever_boards_list + BOOTSTRAP_LEVER)
    ashby_slugs = set(settings.ashby_boards_list + BOOTSTRAP_ASHBY)

    with get_session() as session:
        # Greenhouse
        for slug in greenhouse_slugs:
            slug = slug.strip().lower()
            if not slug:
                continue
            existing = session.exec(
                select(CompanyRegistry).where(
                    CompanyRegistry.slug == slug,
                    CompanyRegistry.ats == JobSource.GREENHOUSE
                )
            ).first()
            if not existing:
                session.add(CompanyRegistry(slug=slug, ats=JobSource.GREENHOUSE, source="seed"))
                count += 1

        # Lever
        for slug in lever_slugs:
            slug = slug.strip().lower()
            if not slug:
                continue
            existing = session.exec(
                select(CompanyRegistry).where(
                    CompanyRegistry.slug == slug,
                    CompanyRegistry.ats == JobSource.LEVER
                )
            ).first()
            if not existing:
                session.add(CompanyRegistry(slug=slug, ats=JobSource.LEVER, source="seed"))
                count += 1

        # Ashby
        for slug in ashby_slugs:
            slug = slug.strip().lower()
            if not slug:
                continue
            existing = session.exec(
                select(CompanyRegistry).where(
                    CompanyRegistry.slug == slug,
                    CompanyRegistry.ats == JobSource.ASHBY
                )
            ).first()
            if not existing:
                session.add(CompanyRegistry(slug=slug, ats=JobSource.ASHBY, source="seed"))
                count += 1

        session.commit()
    log.info("Registry seeded with %d new board entries.", count)
    return count

async def harvest_common_crawl(limit: int = 1000) -> int:
    """Harvest slugs from Common Crawl index. Fall back gracefully if index down."""
    log.info("Starting Common Crawl Harvester...")
    patterns = {
        JobSource.GREENHOUSE: ("boards.greenhouse.io/*", r"boards\.greenhouse\.io/([^/?#\s]+)"),
        JobSource.LEVER:      ("jobs.lever.co/*",        r"jobs\.lever\.co/([^/?#\s]+)"),
        JobSource.ASHBY:      ("jobs.ashbyhq.com/*",      r"jobs\.ashbyhq\.com/([^/?#\s]+)"),
    }
    
    # We query the latest monthly index, which is usually CC-MAIN-YYYY-WW-index
    # We use index.commoncrawl.org to find available index collections first, or default to a stable one
    index_collection = "CC-MAIN-2025-44-index"
    client = httpx.AsyncClient(timeout=15.0)
    
    # Attempt to find the latest index collection
    try:
        r = await client.get("https://index.commoncrawl.org/collinfo.json")
        if r.status_code == 200:
            cols = r.json()
            if cols:
                index_collection = cols[0]["id"]
                log.info("Using latest Common Crawl index: %s", index_collection)
    except Exception as e:
        log.warning("Could not fetch latest CC index info, using default %s: %s", index_collection, e)

    new_slugs_count = 0
    for ats, (url_pattern, rx) in patterns.items():
        cc_url = f"https://index.commoncrawl.org/{index_collection}?url={url_pattern}&output=json&limit={limit}"
        log.info("Querying Common Crawl index for %s pattern: %s", ats.value, cc_url)
        try:
            r = await client.get(cc_url)
            if r.status_code != 200:
                log.warning("CC CDX server returned status %d for %s", r.status_code, ats.value)
                continue
            
            # The output is newline-separated JSON objects
            lines = r.text.strip().split("\n")
            slugs_found: Set[str] = set()
            rx_compiled = re.compile(rx)
            for line in lines:
                if not line.strip():
                    continue
                try:
                    import json
                    obj = json.loads(line)
                    orig_url = obj.get("url", "")
                    m = rx_compiled.search(orig_url)
                    if m:
                        slug = m.group(1).lower().strip()
                        # Clean common garbage suffixes
                        slug = re.split(r'[?/&#\.]', slug)[0]
                        if slug and len(slug) > 2 and len(slug) < 50:
                            slugs_found.add(slug)
                except Exception:
                    continue
            
            # Upsert found slugs
            if slugs_found:
                log.info("Found %d candidate slugs for %s in CC", len(slugs_found), ats.value)
                with get_session() as session:
                    for slug in slugs_found:
                        existing = session.exec(
                            select(CompanyRegistry).where(
                                CompanyRegistry.slug == slug,
                                CompanyRegistry.ats == ats
                            )
                        ).first()
                        if not existing:
                            session.add(CompanyRegistry(slug=slug, ats=ats, source="common_crawl"))
                            new_slugs_count += 1
                    session.commit()
        except Exception as e:
            log.warning("Common Crawl harvesting failed for %s: %s", ats.value, e)
            
    await client.aclose()
    log.info("Common Crawl Harvester finished. Added %d new candidate slugs.", new_slugs_count)
    return new_slugs_count

async def validate_slug(slug: str, ats: JobSource) -> tuple[bool, int]:
    """Hits public ATS API once to verify if it is active and retrieve the number of jobs.
    Returns (is_active, job_count).
    """
    client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    is_active = False
    job_count = 0
    
    try:
        if ats == JobSource.GREENHOUSE:
            url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
            r = await client.get(url)
            if r.status_code == 200:
                payload = r.json()
                jobs = payload.get("jobs", [])
                is_active = len(jobs) > 0
                job_count = len(jobs)
        elif ats == JobSource.LEVER:
            url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
            r = await client.get(url)
            if r.status_code == 200:
                payload = r.json()
                is_active = len(payload) > 0
                job_count = len(payload)
        elif ats == JobSource.ASHBY:
            url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
            r = await client.get(url)
            if r.status_code == 200:
                payload = r.json()
                jobs = payload.get("jobs", [])
                is_active = len(jobs) > 0
                job_count = len(jobs)
    except Exception as e:
        log.debug("Probing slug '%s' (%s) failed: %s", slug, ats.value, e)
    finally:
        await client.aclose()
        
    return is_active, job_count

async def run_validation_loop(limit: int = 200) -> int:
    """Selects unvalidated or oldest last-seen slugs and validates them.
    Updates is_active, job_count, and last_seen.
    """
    log.info("Starting Company Registry Validator Job...")
    with get_session() as session:
        # Check entries that haven't been validated in the last 2 days (or never)
        threshold = datetime.utcnow() - timedelta(days=2)
        candidates = session.exec(
            select(CompanyRegistry)
            .where((CompanyRegistry.last_seen < threshold) | (CompanyRegistry.last_seen.is_(None)))
            .order_by(CompanyRegistry.last_seen.asc())
            .limit(limit)
        ).all()
        
    if not candidates:
        log.info("No companies require validation at this time.")
        return 0
        
    log.info("Validating %d company board slugs...", len(candidates))
    validated_count = 0
    
    for comp in candidates:
        is_active, job_count = await validate_slug(comp.slug, comp.ats)
        
        with get_session() as session:
            db_comp = session.get(CompanyRegistry, comp.id)
            if db_comp:
                db_comp.is_active = is_active
                db_comp.job_count = job_count
                db_comp.last_seen = datetime.utcnow()
                session.add(db_comp)
                session.commit()
                validated_count += 1
                
        # Small sleep to be a polite crawler
        import asyncio
        await asyncio.sleep(0.5)
        
    log.info("Validation cycle complete. Successfully validated %d companies.", validated_count)
    return validated_count
