"""Extractor to scrape, parse, and score job descriptions from any job portal URL."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Dict, Any, Tuple
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from anthropic import Anthropic
import numpy as np
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Job, JobSource, Application, ApplicationStatus
from app.matching.matcher import Matcher
from app.matching.reranker import Reranker
from app.matching.pipeline import _load_resume

log = logging.getLogger(__name__)

SYSTEM_PARSING = """You are a job posting parser. Given the raw text content of a job page, extract the job details in a clean JSON format.

Return ONLY a JSON object:
{
  "company": "<Company Name>",
  "title": "<Job Title>",
  "location": "<Location (e.g. Cincinnati, OH, Remote, San Francisco, CA)>",
  "description": "<Cleaned, structured full job description text with sections, responsibilities, and requirements>",
  "apply_url": "<The URL to apply directly, or null if it cannot be found>"
}

Ensure the description contains all relevant requirements, technologies, and responsibilities. Keep it clean without raw HTML, header navigation links, or footer links from the webpage.
Return JSON only. No explanation, no markdown wrap."""


def make_external_id(url: str) -> str:
    """Generate a deterministic external ID from the URL."""
    return hashlib.md5(url.strip().lower().encode("utf-8")).hexdigest()


def map_url_to_source(url: str) -> JobSource:
    """Map URL domain to our JobSource enum."""
    host = urlparse(url).netloc.lower()
    if "greenhouse.io" in host:
        return JobSource.GREENHOUSE
    elif "lever.co" in host:
        return JobSource.LEVER
    elif "ashbyhq.com" in host:
        return JobSource.ASHBY
    else:
        return JobSource.MANUAL


async def scrape_linkedin_job(url: str) -> str:
    """Fetch a LinkedIn job page via plain HTTP (no login needed for public view pages)."""
    import re
    import httpx

    # Extract job ID from URL like /jobs/view/1234567890/ or currentJobId=1234567890
    m = re.search(r"/jobs/view/(\d+)", url)
    if not m:
        m = re.search(r"currentJobId=(\d+)", url)
    job_id = m.group(1) if m else None

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            # Try the public guest jobs page first (no auth needed)
            if job_id:
                guest_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
                resp = await client.get(guest_url, headers=headers)
            else:
                resp = await client.get(url, headers=headers)

            if resp.status_code == 200:
                # Strip HTML tags to get plain text
                text = re.sub(r"<[^>]+>", " ", resp.text)
                text = re.sub(r"\s{3,}", "\n\n", text)
                if len(text.strip()) > 200:
                    log.info("LinkedIn page fetched via HTTP (%d chars)", len(text))
                    return text[:15000]
    except Exception as e:
        log.warning("LinkedIn scraper HTTP request failed: %s", e)

    raise ValueError(
        "Could not fetch the LinkedIn job page. The posting may be private or expired. "
        "Try the direct company ATS link (Greenhouse/Lever/Ashby) if available."
    )


async def scrape_job_page(url: str) -> str:
    """Use Playwright to render the page and extract all text content."""
    log.info("Scraping job page URL: %s", url)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)  # Wait for JS hydration
            body_text = await page.evaluate("() => document.body.innerText")
            return body_text
        finally:
            await browser.close()


def parse_job_text_with_llm(text: str) -> Dict[str, Any]:
    """Use Claude to parse the raw text into structured job fields."""
    log.info("Sending job text to Claude for structured parsing...")
    client = Anthropic(api_key=settings.anthropic_api_key)
    prompt = f"Scraped Job Page Text:\n---\n{text[:12000]}\n---"
    
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=[{"type": "text", "text": SYSTEM_PARSING}],
        messages=[{"role": "user", "content": prompt}],
    )
    
    raw_content = resp.content[0].text.strip()
    raw_content = raw_content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw_content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned unparseable JSON for job page: {e}") from e


async def extract_and_rank_job(url: str, user_id: str | None = None) -> int:
    """Scrapes URL, parses details, runs matcher & reranker, and creates Application.

    Returns the created Application ID.
    """
    external_id = make_external_id(url)
    source = map_url_to_source(url)

    # 1. Check if job already exists (scoped to this user)
    with get_session() as session:
        eq = select(Job).where(Job.external_id == external_id, Job.source == source)
        if user_id:
            eq = eq.where(Job.user_id == user_id)
        existing_job = session.exec(eq).first()
        if existing_job:
            log.info("Job already exists in DB (ID: %d). Re-checking application...", existing_job.id)
            existing_app = session.exec(
                select(Application).where(Application.job_id == existing_job.id)
            ).first()
            if existing_app:
                return existing_app.id

            # Create application for existing job
            new_app = Application(
                job_id=existing_job.id,
                status=ApplicationStatus.SHORTLISTED,
                apply_url=existing_job.url,
                user_id=user_id,
            )
            session.add(new_app)
            session.commit()
            session.refresh(new_app)
            return new_app.id

    # 2. Scrape & Parse Job Page
    from urllib.parse import urlparse as _up
    _host = _up(url).netloc.lower()
    if "linkedin.com" in _host:
        raw_text = await scrape_linkedin_job(url)
    else:
        raw_text = await scrape_job_page(url)
    parsed = parse_job_text_with_llm(raw_text)

    # Format company name nicely if extracted
    company_name = (parsed.get("company") or "Unknown Company").strip()
    company_name = company_name.replace("-", " ").replace("_", " ").title()
    title = (parsed.get("title") or "Unknown Title").strip()
    description = (parsed.get("description") or "").strip()

    # Fail fast if we scraped a login wall, captcha, or corrupted page
    is_placeholder_desc = any(sig in description.lower() for sig in [
        "could not be parsed", "binary or corrupted", "login wall", "captcha", "security check"
    ])
    if company_name == "Unknown Company" or title == "Unknown Title" or len(description) < 150 or is_placeholder_desc:
        raise ValueError(
            "Could not extract valid job details. The page may be behind a login wall, CAPTCHA, "
            "or contains unreadable text. Try pasting the direct Greenhouse/Lever/Ashby ATS URL."
        )

    # 3. Create Job model
    job = Job(
        source=source,
        external_id=external_id,
        company=company_name,
        title=title,
        location=(parsed.get("location") or "Remote").strip(),
        url=url,
        description=description,
        remote="remote" in parsed.get("location", "").lower(),
        user_id=user_id,
    )

    # 4. Compute similarity and match scores
    resume = _load_resume(user_id=user_id)
    
    # Calculate similarity score
    try:
        matcher = Matcher()
        q = matcher.encode([resume])
        job_text = matcher._job_text(job)
        emb = matcher.encode([job_text])
        similarity = float(np.dot(q[0], emb[0]))
        job.similarity_score = similarity
    except Exception as e:
        log.warning("Could not calculate similarity: %s", e)
        job.similarity_score = None  # None signals "not scored" — not a perfect match

    # Calculate rerank score
    try:
        import json as _json
        _prof = None
        try:
            from app.autofill.answer_pack import _get_or_create_profile
            _prof = _get_or_create_profile(user_id=user_id if user_id and user_id != "local" else None)
        except Exception:
            _prof = None
        reranker = Reranker(profile=_prof)
        score, reason, concerns, breakdown = reranker.score(resume, job)
        job.rerank_score = score
        job.rerank_reasoning = reason + (
            ("\nConcerns: " + "; ".join(concerns)) if concerns else ""
        )
        job.rerank_breakdown = _json.dumps(breakdown) if breakdown else None
    except Exception as e:
        log.warning("Could not calculate rerank score: %s", e)
        job.rerank_score = 70.0
        job.rerank_reasoning = f"Manually imported via link. Reranker failed: {e}"

    with get_session() as session:
        session.add(job)
        session.commit()
        session.refresh(job)

        # 5. Create Application in SHORTLISTED state
        apply_url = parsed.get("apply_url") or url
        app = Application(
            job_id=job.id,
            status=ApplicationStatus.SHORTLISTED,
            apply_url=apply_url,
            user_id=user_id,
        )
        session.add(app)
        session.commit()
        session.refresh(app)
        
        log.info("Created Application ID: %d in SHORTLISTED state for job '%s' at '%s'", app.id, job.title, job.company)
        return app.id
