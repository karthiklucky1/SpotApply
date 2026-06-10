"""External hiring signals — additive boosts to hire_probability_score.

Two signals:
  1. GitHub /hiring — check if company's GitHub org has HIRING.md / jobs.json
     (recently updated = strong active-hiring signal)
  2. Crunchbase Basic — free-tier search returns recent funding round dates;
     a round within 6 months is a strong hiring-surge signal.
     Falls back gracefully if no API key or rate-limited.

Design: signals only ADD to the score (0.0 if not found).
         Failures are silently swallowed — never break the scorer.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

import httpx  # noqa: F401 — imported so tests can patch app.matching.external_signals.httpx

log = logging.getLogger(__name__)

# ── GitHub hiring-file signal ──────────────────────────────────────────────────
_GH_HIRING_FILES = [
    "HIRING.md",
    "jobs.json",
    ".github/HIRING.md",
    "JOBS.md",
]
_GH_RECENT_DAYS = 90   # file updated within this window → full boost
_GH_STALE_DAYS = 180   # file exists but older → half boost
_GH_BOOST_FRESH = 0.15
_GH_BOOST_STALE = 0.07

# ── Crunchbase free (Basic) signal ────────────────────────────────────────────
# Crunchbase Basic allows ~200 free searches/month via their web autocomplete
# endpoint — no official API key needed for the public search.
# We extract the most-recent funding round date from the JSON response.
_CB_BOOST_6M  = 0.20   # funded within 6 months
_CB_BOOST_12M = 0.12   # funded within 12 months
_CB_BOOST_18M = 0.06   # funded within 18 months


def _days_since(iso_or_epoch, *, now: Optional[datetime] = None) -> Optional[int]:
    """Return days since a date string / epoch int, or None if unparseable."""
    if now is None:
        now = datetime.now(tz=timezone.utc)
    try:
        if isinstance(iso_or_epoch, (int, float)):
            dt = datetime.fromtimestamp(iso_or_epoch, tz=timezone.utc)
        else:
            # handles "2025-03-15", "2025-03-15T00:00:00Z", partial dates "2025-03"
            s = str(iso_or_epoch).strip().rstrip("Z")
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m", "%Y"):
                try:
                    dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                return None
        return max(0, (now - dt).days)
    except Exception:
        return None


def check_github_hiring(company_name: str) -> Tuple[float, str]:
    """
    Search the company's GitHub org for hiring files.
    Returns (boost, signal_label).
    Boost is 0.0 if GitHub token absent, org not found, or file not found.
    """
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return 0.0, ""

    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # 1. Find the org by company name (search users/orgs)
        slug = _company_to_github_slug(company_name)
        if not slug:
            return 0.0, ""

        # 2. Check each hiring-file path
        for path in _GH_HIRING_FILES:
            url = f"https://api.github.com/repos/{slug}/{path.replace('.github/', '')}"
            # Try the top-level repo first (some companies have a .github meta-repo)
            candidates = [
                f"https://api.github.com/repos/{slug}/.github/contents/{path}",
                f"https://api.github.com/repos/{slug}/hiring/contents/{path.split('/')[-1]}",
                f"https://api.github.com/repos/{slug}/.github/contents/HIRING.md",
            ]
            for cand in candidates:
                try:
                    r = httpx.get(cand, headers=headers, timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        pushed_at = data.get("commit", {}).get("committer", {}).get("date") or ""
                        days = _days_since(pushed_at) if pushed_at else _GH_STALE_DAYS + 1
                        if days is not None and days <= _GH_RECENT_DAYS:
                            return _GH_BOOST_FRESH, f"github_hiring_file_fresh_{days}d"
                        return _GH_BOOST_STALE, f"github_hiring_file_stale"
                except Exception:
                    continue
    except Exception as exc:
        log.debug("GitHub hiring check failed for %s: %s", company_name, exc)

    return 0.0, ""


def _company_to_github_slug(company_name: str) -> Optional[str]:
    """
    Convert company name → most-likely GitHub org slug via GitHub user search.
    Returns slug string or None.
    """
    token = os.getenv("GITHUB_TOKEN", "")
    try:
        q = re.sub(r"[^a-z0-9 ]", "", company_name.lower()).strip()
        r = httpx.get(
            "https://api.github.com/search/users",
            params={"q": f"{q} type:org", "per_page": 3},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=5,
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            if items:
                return items[0]["login"]
    except Exception:
        pass
    return None


def check_crunchbase_funding(company_name: str) -> Tuple[float, str]:
    """
    Query Crunchbase Basic (free, no key) autocomplete endpoint.
    Returns (boost, signal_label).

    Crunchbase Basic exposes a public /v4/data/autocompletes endpoint that
    returns the last funding round date without authentication.
    """
    try:
        # Crunchbase public autocomplete — used by their own website, no auth
        r = httpx.get(
            "https://www.crunchbase.com/v4/data/autocompletes",
            params={"query": company_name, "collection_ids": "organizations", "limit": 3},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; jobagent/1.0)",
                "Accept": "application/json",
            },
            timeout=8,
        )
        if r.status_code != 200:
            return 0.0, ""

        entities = r.json().get("entities", [])
        if not entities:
            return 0.0, ""

        # Pick best name match
        best = _best_name_match(company_name, entities)
        if best is None:
            return 0.0, ""

        # last_funding_at is exposed in the short entity card
        last_funded = (
            best.get("properties", {}).get("last_funding_at")
            or best.get("short_description", "")  # fallback: won't parse
        )
        days = _days_since(last_funded)
        if days is None:
            return 0.0, ""

        if days <= 180:
            return _CB_BOOST_6M, f"crunchbase_funded_{days}d_ago"
        elif days <= 365:
            return _CB_BOOST_12M, f"crunchbase_funded_{days}d_ago"
        elif days <= 548:
            return _CB_BOOST_18M, f"crunchbase_funded_{days}d_ago"

    except Exception as exc:
        log.debug("Crunchbase check failed for %s: %s", company_name, exc)

    return 0.0, ""


def _best_name_match(query: str, entities: list) -> Optional[dict]:
    """Return entity whose name most closely matches query (simple prefix/contains)."""
    q = query.lower()
    scored = []
    for e in entities:
        name = (e.get("properties", {}).get("name") or "").lower()
        if q in name or name in q:
            scored.append((len(name), e))
        elif any(w in name for w in q.split() if len(w) > 3):
            scored.append((999, e))
    if scored:
        scored.sort(key=lambda x: x[0])
        return scored[0][1]
    return entities[0] if entities else None
