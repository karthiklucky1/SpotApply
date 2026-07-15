"""Free ghost-check / fit-check for any job URL — the acquisition wedge.

Given a job posting URL (no account needed), answer: is this job real and
still open, how stale is it, and — when the caller is signed in with a résumé
— how well do they match its keywords?

Verification strategy, cheapest-first:
  1. Recognized public-API ATS URL (Greenhouse / Lever / Ashby) → ask the ATS
     API whether that exact posting is still served. Authoritative.
  2. Anything else → fetch the page (logged-out, public): HTTP 404/410 or
     closed-posting phrases mean closed; JSON-LD JobPosting gives datePosted /
     validThrough for age; page text feeds the fit check.

Only public, logged-out endpoints are used — same compliance posture as
discovery. LinkedIn/Indeed URLs are answered from page-independent signals
only (no automation on their pages).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.matching.filters.ghost_detector import (
    _AGGREGATOR_REDIRECT_RE,
    _SALARY_SIGNAL_RE,
)

log = logging.getLogger(__name__)

_CLOSED_PHRASES_RE = re.compile(
    r"no longer accepting applications|this job is no longer available"
    r"|position has been filled|job not found|posting.{0,20}(closed|expired)"
    r"|this position is closed|vacancy.{0,20}closed|sorry.{0,30}(expired|removed)",
    re.IGNORECASE,
)

_GREENHOUSE_RE = re.compile(
    r"(?:boards|job-boards)\.(?:greenhouse\.io|eu\.greenhouse\.io)/([^/]+)/jobs/(\d+)", re.I)
_GREENHOUSE_EMBED_RE = re.compile(r"greenhouse\.io/embed/job_app\?.*?token=(\d+)", re.I)
_LEVER_RE = re.compile(r"jobs\.(?:eu\.)?lever\.co/([^/]+)/([0-9a-f-]{36})", re.I)
_ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", re.I)

_UA = {"User-Agent": "Mozilla/5.0 (SpotApply job-check; +https://app.spotapply.ai)"}


def _days_ago(iso: str) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return None


def _check_ats_api(url: str, client: httpx.Client) -> Optional[dict]:
    """Authoritative live-check via the posting's own public ATS API."""
    m = _GREENHOUSE_RE.search(url)
    if m:
        board, job_id = m.group(1), m.group(2)
        r = client.get(f"https://api.greenhouse.io/v1/boards/{board}/jobs/{job_id}")
        if r.status_code == 200:
            d = r.json()
            return {"live": True, "ats": "greenhouse", "title": d.get("title"),
                    "company": board, "posted_days_ago": _days_ago(d.get("updated_at") or ""),
                    "text": re.sub(r"<[^>]+>", " ", d.get("content") or "")}
        if r.status_code == 404:
            return {"live": False, "ats": "greenhouse", "company": board}
        return None

    m = _LEVER_RE.search(url)
    if m:
        board, posting = m.group(1), m.group(2)
        r = client.get(f"https://api.lever.co/v0/postings/{board}/{posting}")
        if r.status_code == 200:
            d = r.json()
            created = d.get("createdAt")
            days = None
            if isinstance(created, (int, float)):
                days = max(0, (datetime.now(timezone.utc)
                               - datetime.fromtimestamp(created / 1000, tz=timezone.utc)).days)
            return {"live": True, "ats": "lever", "title": d.get("text"),
                    "company": board, "posted_days_ago": days,
                    "text": re.sub(r"<[^>]+>", " ", d.get("descriptionPlain") or d.get("description") or "")}
        if r.status_code == 404:
            return {"live": False, "ats": "lever", "company": board}
        return None

    m = _ASHBY_RE.search(url)
    if m:
        board, posting = m.group(1), m.group(2)
        r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true")
        if r.status_code == 200:
            for j in (r.json().get("jobs") or []):
                if posting in (j.get("id") or "", j.get("jobUrl") or "", j.get("applyUrl") or ""):
                    return {"live": True, "ats": "ashby", "title": j.get("title"),
                            "company": board,
                            "posted_days_ago": _days_ago(j.get("publishedAt") or ""),
                            "text": re.sub(r"<[^>]+>", " ", j.get("descriptionPlain") or j.get("descriptionHtml") or "")}
            return {"live": False, "ats": "ashby", "company": board}
        return None
    return None


def _is_public_host(host: str) -> bool:
    """SSRF guard: only fetch hosts that resolve exclusively to public IPs.
    Without this, /api/public/job-check would fetch attacker-supplied URLs
    server-side (cloud metadata endpoints, internal services, localhost)."""
    import ipaddress
    import socket
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return bool(infos)


def _check_generic_page(url: str, client: httpx.Client) -> dict:
    """Fallback: fetch the public page and read status/JSON-LD/closed phrases.
    Redirects are followed manually so every hop is SSRF-checked."""
    out: dict = {"live": True, "ats": None, "title": None, "company": None,
                 "posted_days_ago": None, "text": ""}
    try:
        current, r, history = url, None, []
        for _ in range(4):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not _is_public_host(parsed.hostname or ""):
                return {**out, "live": None, "error": "blocked_host"}
            r = client.get(current, headers=_UA, follow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                history.append(r)
                current = str(httpx.URL(current).join(r.headers["location"]))
                continue
            break
        r.history = history
    except Exception as e:
        return {**out, "live": None, "error": f"fetch_failed: {e}"}
    if r.status_code in (404, 410):
        return {**out, "live": False}
    # Redirected off the posting to a careers root → very likely removed.
    final_path = urlparse(str(r.url)).path.rstrip("/")
    if r.history and final_path in ("", "/careers", "/jobs", "/career"):
        return {**out, "live": False}
    html = r.text[:800_000]
    if _CLOSED_PHRASES_RE.search(html):
        return {**out, "live": False}
    # JSON-LD JobPosting: title/company/dates
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict) or item.get("@type") not in ("JobPosting", ["JobPosting"]):
                continue
            out["title"] = out["title"] or item.get("title")
            org = item.get("hiringOrganization") or {}
            out["company"] = out["company"] or (org.get("name") if isinstance(org, dict) else None)
            if item.get("datePosted"):
                out["posted_days_ago"] = _days_ago(str(item["datePosted"]))
            vt = item.get("validThrough")
            if vt:
                exp = _days_ago(str(vt))
                # validThrough in the past → expired posting still being served
                if exp is not None and _days_ago(str(vt)) is not None:
                    try:
                        vt_dt = datetime.fromisoformat(str(vt).replace("Z", "+00:00"))
                        if vt_dt.tzinfo is None:
                            vt_dt = vt_dt.replace(tzinfo=timezone.utc)
                        if vt_dt < datetime.now(timezone.utc):
                            out["live"] = False
                    except Exception:
                        pass
    out["text"] = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    out["text"] = re.sub(r"<[^>]+>", " ", out["text"])[:30_000]
    return out


def check_job_url(url: str, resume_text: Optional[str] = None) -> dict:
    """Ghost-check (and optional fit-check) one job URL. Pure function — no DB."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return {"ok": False, "error": "invalid_url"}

    signals: list[str] = []
    ghost = 0.0

    if _AGGREGATOR_REDIRECT_RE.search(url):
        signals.append("aggregator_redirect_domain")
        ghost += 0.5

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        info = _check_ats_api(url, client)
        if info is None:
            host = urlparse(url).hostname or ""
            if "linkedin.com" in host or "indeed.com" in host:
                # Hands-off domains: no page automation — answer from URL only.
                info = {"live": None, "ats": host.split(".")[-2], "title": None,
                        "company": None, "posted_days_ago": None, "text": ""}
                signals.append("hands_off_domain_no_page_check")
            else:
                info = _check_generic_page(url, client)

    if info.get("live") is False:
        signals.append("posting_closed_or_removed")
        ghost = 1.0
    days = info.get("posted_days_ago")
    if isinstance(days, int):
        if days >= 60:
            signals.append(f"stale_posting_{days}d")
            ghost += 0.5
        elif days >= 45:
            signals.append(f"aging_posting_{days}d")
            ghost += 0.3
        elif days <= 3:
            signals.append(f"fresh_posting_{days}d")
    text = info.get("text") or ""
    if info.get("live") and text:
        if not _SALARY_SIGNAL_RE.search(text):
            signals.append("no_salary_listed")
            ghost += 0.1
        if len(text.split()) < 150:
            signals.append("thin_description")
            ghost += 0.2
    ghost = min(1.0, round(ghost, 2))

    fit = None
    if resume_text and text and info.get("live"):
        try:
            from app.tailoring.ats_keywords import analyze
            report = analyze(text, resume_text)
            fit = {
                "score_pct": round(report.coverage_pct * 100),
                "matched": report.matched[:15],
                "missing": report.missing[:15],
            }
        except Exception as e:
            log.debug("fit-check failed: %s", e)

    return {
        "ok": True,
        "live": info.get("live"),
        "ghost_score": ghost,
        "signals": signals,
        "posted_days_ago": days,
        "ats": info.get("ats"),
        "title": info.get("title"),
        "company": info.get("company"),
        "fit": fit,
    }
