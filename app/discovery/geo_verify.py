"""Establish WHERE a new posting is — once per posting, shared by every user.

This is the cheap verification sequence behind `app/common/eligibility.py`:

  A. what the ATS response already said (structured country, postal address,
     every site, workplace type) plus explicit restrictions written in the
     description ("Germany residents only", "must be located in the US");
  B. if that leaves the posting unplaced: the posting's own public page or its
     official ATS detail endpoint — one SSRF-guarded, bounded GET, read for
     schema.org JobPosting (`jobLocation`, `jobLocationType`,
     `applicantLocationRequirements`) and the ATS's structured fields;
  C. only if location-related TEXT exists that the rules could not interpret:
     one small structured-extraction call on the cheapest configured model,
     sent nothing but those excerpts. The model must quote what it relied on,
     and the quote is checked against the text it was given — an answer with
     no valid quote is discarded. No excerpts means no call: a posting that
     says nothing is UNKNOWN, and we do not pay a model to guess.

Everything is keyed by (source, external_id) — the identity per-user copies
preserve — and cached in `JobGeography`, unresolved results included, with
bounded retry, so twelve users adopting one posting cost one fetch and at most
one model call, and a posting nobody can place does not become a per-tick bill.

Scope: NEW postings only. A row is created for a posting the shared pool has
never seen (or one that reaches a user's pool straight from a scraper). A
copy of an older shared posting never creates one — copying does not make a
posting new — and rows that predate this module keep the string gate they
always had. No résumé text ever enters this module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from app.common.eligibility import (
    CONFLICT, ELIGIBLE, INELIGIBLE, RESOLVED, STATUS_UNKNOWN,
    UNKNOWN, WORLDWIDE, Decision, Geography, decide,
)
from app.common.geo import (
    _SITE_SPLIT, detect_country, detect_region, resolve_country_value,
)
from app.config import settings
from app.discovery.base import GeoEvidence, RawJob

log = logging.getLogger(__name__)

#: Bumped when extraction rules change so stored rows can be told apart.
VERIFIER_VERSION = 1

#: The score an INELIGIBLE copy is stamped with as it leaves the queue — the
#: same number the rule filter uses, so every reader that already understands
#: a rule stamp understands this one. It is a verdict (scored_at is set), not
#: an expiry, and `Job.eligibility` says which kind.
INELIGIBLE_STAMP_SCORE = 10.0
INELIGIBLE_REASON_PREFIX = "Location filtered: "

#: Description prefix the restriction scanner reads. Restrictions sit in the
#: "requirements" or "location" paragraph, rarely past this.
TEXT_SCAN_CHARS = 20000

# ── aggregate metrics only (never a job id, external id or URL) ──────────────
_METRICS: Counter = Counter()
_metrics_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    with _metrics_lock:
        _METRICS[key] += n


def metrics_snapshot(reset: bool = False) -> dict:
    """Counters since process start (or the last reset), plus derived cost per
    posting so the log line answers "what did verification cost?" directly."""
    with _metrics_lock:
        data = dict(_METRICS)
        if reset:
            _METRICS.clear()
    verified = data.get("verified_postings", 0)
    cost_micro = data.get("llm_cost_microusd", 0)
    data["llm_cost_usd"] = round(cost_micro / 1_000_000.0, 5)
    data["cost_per_verified_posting_usd"] = (
        round(cost_micro / 1_000_000.0 / verified, 6) if verified else 0.0)
    return data


# ── daily LLM cap (platform-wide, in-process like the card-mint cap) ─────────
_llm_today = {"day": "", "count": 0}
_llm_lock = threading.Lock()


def _llm_calls_today() -> int:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _llm_lock:
        if _llm_today["day"] != today:
            _llm_today["day"], _llm_today["count"] = today, 0
        return _llm_today["count"]


def _register_llm_call() -> None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _llm_lock:
        if _llm_today["day"] != today:
            _llm_today["day"], _llm_today["count"] = today, 0
        _llm_today["count"] += 1


def reset_state() -> None:
    """Tests only: clear the counters and the daily cap."""
    with _metrics_lock:
        _METRICS.clear()
    with _llm_lock:
        _llm_today["day"], _llm_today["count"] = "", 0


# ═════════════════════════════════════════════════════════════════════════════
# Step A — deterministic evidence from what we already hold
# ═════════════════════════════════════════════════════════════════════════════

_PLUS_MORE = re.compile(r"\s*\+\d+\s+more\s*$", re.I)
_WORLDWIDE_RE = re.compile(
    r"\b(anywhere|worldwide|world-?wide|globally|global|any (?:country|location|time ?zone))\b", re.I)

_PLACE = r"(?P<place>[A-Z][A-Za-z.&'\- ]{1,40}(?:,\s*[A-Z][A-Za-z.&'\- ]{1,30})?)"
# Only phrases that restrict WHERE THE PERSON MUST BE. "Authorized to work in"
# is work authorization — a separate check — and is deliberately not here.
_RESTRICTION_RES = [re.compile(p, re.I) for p in (
    r"(?:must|need(?:s)? to|required to|have to|has to|should|will need to)\s+(?:be\s+)?"
    r"(?:physically\s+)?(?:located|based|residing|reside|living|live)\s+(?:in|within)\s+(?:the\s+)?" + _PLACE,
    r"(?:must\s+be\s+(?:a\s+)?|be\s+a\s+)?residents?\s+of\s+(?:the\s+)?" + _PLACE,
    r"(?P<place>[A-Z][A-Za-z.&'\- ]{1,40})\s+(?:residents?|based\s+(?:candidates|applicants))\s+only",
    r"only\s+(?:open\s+to|available\s+to|considering|accepting|hiring)\s+"
    r"(?:candidates|applicants|applications|people)?\s*(?:who\s+are\s+)?"
    r"(?:in|from|based\s+in|located\s+in|residing\s+in|living\s+in|within)\s+(?:the\s+)?" + _PLACE,
    r"(?:open|available)\s+(?:only\s+)?to\s+(?:candidates|applicants)\s+(?:who\s+are\s+)?"
    r"(?:in|from|based\s+in|located\s+in|residing\s+in|living\s+in|within)\s+(?:the\s+)?" + _PLACE,
    r"(?:remote|available|open)\s+(?:only\s+)?(?:for|to)\s+(?:candidates|applicants|residents|those)\s+"
    r"(?:located|based|residing|living)?\s*(?:in|within)\s+(?:the\s+)?" + _PLACE,
    r"(?:must\s+be\s+|only\s+)?(?P<place>[A-Z][A-Za-z.]{1,30})[- ]based(?:\s+(?:candidates|applicants|only))",
    r"\b(?P<place>US|U\.S\.|USA|United States|EU|Europe|UK|Canada|Germany|India|Australia|LATAM|EMEA|APAC)[- ]only\b",
)]

# Sentences worth handing to the model when the rules could not read them.
_LOCATION_WORDS = re.compile(
    r"\b(location|located|based|remote|hybrid|on-?site|in-?office|office|relocat\w*|"
    r"resid\w*|countr(?:y|ies)|time ?zones?|region|work from|home ?office|homeoffice|"
    r"headquarter\w*|\bhq\b|anywhere|worldwide|global\w*|within|city|citizen\w*)\b", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+")

_WORK_MODE_RES = (
    ("hybrid", re.compile(r"\bhybrid\b", re.I)),
    ("onsite", re.compile(r"\b(?:on-?site|in-?office|in office|office-based)\b", re.I)),
    ("remote", re.compile(r"\b(?:remote|home ?office|homeoffice|work from home|wfh|telecommut\w*)\b", re.I)),
)


def _clean_site(site: str) -> str:
    return _PLUS_MORE.sub("", (site or "").strip()).strip(" ·|;/")


def _sites_from_location(location: str) -> List[str]:
    return [_clean_site(s) for s in _SITE_SPLIT.split(location or "") if _clean_site(s)]


def _resolve_place(text: str) -> Tuple[str, str]:
    """('country', '') / ('', 'region') / ('', WORLDWIDE) / ('', '') for a phrase."""
    if _WORLDWIDE_RE.search(text or ""):
        return "", WORLDWIDE
    c = detect_country(text)
    if c:
        return c, ""
    r = detect_region(text)
    return "", r


def _sentence_around(text: str, start: int, end: int, width: int = 160) -> str:
    lo = max(0, start - width)
    hi = min(len(text), end + width)
    chunk = text[lo:hi]
    # Trim to sentence boundaries on both sides where there are any.
    left = max(chunk.rfind(". ", 0, start - lo), chunk.rfind("\n", 0, start - lo))
    right_candidates = [i for i in (chunk.find(". ", end - lo), chunk.find("\n", end - lo)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(chunk)
    return " ".join(chunk[left + 1 if left != -1 else 0:right].split())[:300]


def scan_restrictions(text: str) -> Tuple[List[str], List[str], List[str], List[str]]:
    """Explicit residence restrictions in prose.

    Returns (countries, regions, quotes, unresolved_excerpts): the first three
    are what the rules could read; the fourth is restriction-shaped text that
    named a place the tables do not know — the "useful evidence exists but
    rules cannot interpret it" case that alone justifies a model call.
    """
    body = (text or "")[:TEXT_SCAN_CHARS]
    countries: List[str] = []
    regions: List[str] = []
    quotes: List[str] = []
    unresolved: List[str] = []
    for rx in _RESTRICTION_RES:
        for m in rx.finditer(body):
            place = (m.group("place") or "").strip(" .,;:")
            if not place:
                continue
            country, region = _resolve_place(place)
            quote = _sentence_around(body, m.start(), m.end())
            if country and country not in countries:
                countries.append(country)
                quotes.append(quote)
            elif region and region not in regions:
                regions.append(region)
                quotes.append(quote)
            elif not country and not region:
                if quote not in unresolved:
                    unresolved.append(quote)
    return countries, regions, quotes, unresolved


def location_excerpts(text: str, limit: int) -> str:
    """The sentences of `text` that talk about location, joined, cut at `limit`.
    This — and only this — is what a model may be shown."""
    body = " ".join((text or "")[:TEXT_SCAN_CHARS].split())
    out: List[str] = []
    used = 0
    for sent in _SENTENCE_SPLIT.split(body):
        s = sent.strip()
        if len(s) < 12 or not _LOCATION_WORDS.search(s):
            continue
        s = s[:400]
        if used + len(s) + 1 > limit:
            break
        out.append(s)
        used += len(s) + 1
    return "\n".join(out)


def _work_mode_from_text(texts: Iterable[str]) -> str:
    joined = " ".join(t for t in texts if t)
    for mode, rx in _WORK_MODE_RES:
        if rx.search(joined):
            return mode
    return ""


def _admits(country: str, countries: List[str], regions: List[str]) -> bool:
    if country in countries:
        return True
    from app.common.geo import _REGION_MEMBERS
    for r in regions:
        if r == WORLDWIDE or (r in _REGION_MEMBERS and country in _REGION_MEMBERS[r]):
            return True
    return False


def derive(raw: RawJob) -> Geography:
    """Step A. Pure — reads the RawJob only."""
    ev: Optional[GeoEvidence] = getattr(raw, "geo", None)
    sites: List[str] = []
    if ev and ev.sites:
        sites = [_clean_site(s) for s in ev.sites if _clean_site(s)]
    if not sites:
        sites = _sites_from_location(raw.location)

    countries: List[str] = []
    evidence_source, evidence_field, evidence_quote = "none", "", ""

    if ev and ev.country:
        c = resolve_country_value(ev.country)
        if c:
            countries.append(c)
            evidence_source, evidence_field = "ats_structured", ev.country_field or "country"
    for site in sites:
        c = detect_country(site)
        if c and c not in countries:
            countries.append(c)
            if evidence_source == "none":
                evidence_source = "ats_location"
                evidence_field = (ev.sites_field if ev and ev.sites_field else "location")

    # Region anchors in the sites themselves ("Remote (Europe)", "EMEA").
    regions: List[str] = []
    for site in sites:
        r = detect_region(site)
        if r and r not in regions:
            regions.append(r)
        if _WORLDWIDE_RE.search(site) and WORLDWIDE not in regions:
            regions.append(WORLDWIDE)

    # The source's own remote-restriction field (Remotive, Jobicy, WWR …).
    if ev and ev.remote_regions:
        c, r = _resolve_place(ev.remote_regions)
        if c and c not in countries:
            countries.append(c)
        if r and r not in regions:
            regions.append(r)
        if (c or r) and evidence_source == "none":
            evidence_source, evidence_field = "ats_structured", ev.remote_regions_field or "remote_regions"

    # Explicit restrictions in the description.
    text_countries, text_regions, quotes, _unresolved = scan_restrictions(raw.description)
    conflicts: List[str] = []
    site_countries = list(countries)
    if (text_countries or text_regions) and site_countries and not any(
            _admits(c, text_countries, text_regions) for c in site_countries):
        conflicts.append(
            f"posting sites say {', '.join(site_countries[:3])} but the text restricts "
            f"to {', '.join((text_countries + text_regions)[:3])}")
    for c in text_countries:
        if c not in countries:
            countries.append(c)
    for r in text_regions:
        if r not in regions:
            regions.append(r)
    if quotes:
        evidence_quote = quotes[0]
        if evidence_source == "none":
            evidence_source, evidence_field = "description", "description"

    # A text restriction on a remote posting is the remote track's region list;
    # the restriction countries are also "countries" so the country check can
    # pass on them. Only text/field restrictions count as remote regions — a
    # city site never does.
    for c in text_countries:
        if c not in regions:
            regions.append(c)
    if ev and ev.remote_regions:
        c, _r = _resolve_place(ev.remote_regions)
        if c and c not in regions:
            regions.append(c)

    work_mode = (ev.work_mode if ev and ev.work_mode in ("remote", "hybrid", "onsite") else "")
    if not work_mode:
        work_mode = _work_mode_from_text(sites)
    if not work_mode and raw.remote and not any(detect_country(s) for s in sites):
        work_mode = "remote"

    if conflicts:
        status = CONFLICT
    elif countries or regions:
        status = RESOLVED
    else:
        status = STATUS_UNKNOWN
    return Geography(status=status, countries=countries, sites=sites, work_mode=work_mode,
                     remote_regions=regions, conflicts=conflicts,
                     evidence_source=evidence_source, evidence_field=evidence_field,
                     evidence_quote=evidence_quote)


def location_hash(raw: RawJob, content_hash: str = "") -> str:
    """Names the location evidence a geography row was derived from. Any
    change to it (a new site, a country code, an edited description) makes a
    later sighting re-derive the row and re-decide every copy."""
    ev = getattr(raw, "geo", None)
    payload = json.dumps([
        sorted(ev.sites) if ev else [], (ev.country if ev else ""),
        (ev.work_mode if ev else ""), (ev.remote_regions if ev else ""),
        raw.location or "", bool(raw.remote), content_hash or "",
    ], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
# Persistence — one row per posting
# ═════════════════════════════════════════════════════════════════════════════

def _row_to_geo(row) -> Geography:
    def _loads(s):
        try:
            v = json.loads(s or "[]")
            return v if isinstance(v, list) else []
        except (TypeError, ValueError):
            return []
    return Geography(status=row.status or STATUS_UNKNOWN,
                     countries=_loads(row.countries_json), sites=_loads(row.sites_json),
                     work_mode=row.work_mode or "", remote_regions=_loads(row.remote_regions_json),
                     conflicts=_loads(row.conflicts_json),
                     evidence_source=row.evidence_source or "none",
                     evidence_field=row.evidence_field or "", evidence_quote=row.evidence_quote or "")


def _apply_geo(row, geo: Geography) -> None:
    row.status = geo.status
    row.countries_json = json.dumps(geo.countries)
    row.sites_json = json.dumps(geo.sites[:64], ensure_ascii=False)
    row.work_mode = geo.work_mode or None
    row.remote_regions_json = json.dumps(geo.remote_regions)
    row.conflicts_json = json.dumps(geo.conflicts[:8], ensure_ascii=False)
    row.evidence_source = geo.evidence_source or "none"
    row.evidence_field = (geo.evidence_field or None) and geo.evidence_field[:200]
    row.evidence_quote = (geo.evidence_quote or None) and geo.evidence_quote[:300]


def load_geographies(keys: Iterable[tuple]) -> Dict[tuple, Geography]:
    """{(source, external_id): Geography} for the keys that have a row."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobGeography

    wanted = sorted({(k[0], str(k[1])) for k in keys if k and k[1]})
    out: Dict[tuple, Geography] = {}
    if not wanted:
        return out
    try:
        with get_session() as session:
            for start in range(0, len(wanted), 300):
                chunk = wanted[start:start + 300]
                rows = session.exec(
                    select(JobGeography).where(
                        JobGeography.source.in_({k[0] for k in chunk}),
                        JobGeography.external_id.in_([k[1] for k in chunk]))
                ).all()
                for row in rows:
                    key = (row.source, row.external_id)
                    if key in set(chunk):
                        out[key] = _row_to_geo(row)
    except Exception as e:
        log.debug("geography lookup failed: %s", e)
    return out


def capture(raw_jobs: List[RawJob], *, new_keys: set, changed_keys: set,
            content_hashes: Optional[Dict[tuple, str]] = None) -> Dict[tuple, Geography]:
    """Record geography for postings that are NEW (never in the shared pool) and
    re-derive it for ones whose location evidence CHANGED. Returns the
    geography of every posting in `raw_jobs` that now has a row, so the
    per-user doors that follow in the same tick need no lookup.

    Never raises: geography must never stop discovery from storing jobs.
    """
    if not settings.geo_verify_enabled:
        return {}
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobGeography

    todo: Dict[tuple, RawJob] = {}
    for r in raw_jobs:
        if not r.external_id:
            continue
        key = (r.source, str(r.external_id))
        if key in new_keys or key in changed_keys:
            todo[key] = r      # last sighting in the batch wins, like the row itself
    if not todo:
        return {}
    keys = sorted(todo)
    hashes = {k: location_hash(todo[k], (content_hashes or {}).get(k, "")) for k in keys}
    out: Dict[tuple, Geography] = {}
    now = datetime.utcnow()
    to_redecide: List[tuple] = []
    try:
        with get_session() as session:
            existing: Dict[tuple, JobGeography] = {}
            for start in range(0, len(keys), 300):
                chunk = keys[start:start + 300]
                for row in session.exec(
                        select(JobGeography).where(
                            JobGeography.source.in_({k[0] for k in chunk}),
                            JobGeography.external_id.in_([k[1] for k in chunk]))).all():
                    existing[(row.source, row.external_id)] = row
            for key in keys:
                raw = todo[key]
                row = existing.get(key)
                if row is not None and row.location_hash == hashes[key] \
                        and row.verifier_version == VERIFIER_VERSION:
                    row.cache_hits = (row.cache_hits or 0) + 1
                    session.add(row)
                    out[key] = _row_to_geo(row)
                    _bump("cache_hit")
                    continue
                if row is not None and key not in changed_keys and key not in new_keys:
                    out[key] = _row_to_geo(row)
                    continue
                geo = derive(raw)
                if row is None:
                    if key not in new_keys:
                        # Not new, and never recorded: a pre-rollout posting.
                        # Copying it must not make it new.
                        continue
                    row = JobGeography(source=key[0], external_id=key[1], created_at=now)
                    _bump("new_postings")
                elif (row.status == RESOLVED and row.last_step in ("page", "llm")
                      and not geo.resolved and geo.status != CONFLICT):
                    # The evidence moved (an edited description, say) but the
                    # source STILL states no location. What the page or the
                    # model established stands — nothing contradicts it — and
                    # only the hash advances, so the row is not re-derived on
                    # every later sighting. A restriction the new text now
                    # states (resolved) or contradicts (conflict) does win.
                    row.location_hash = hashes[key]
                    row.verifier_version = VERIFIER_VERSION
                    row.updated_at = now
                    session.add(row)
                    out[key] = _row_to_geo(row)
                    _bump("verified_kept_on_change")
                    continue
                else:
                    _bump("evidence_changed")
                    to_redecide.append(key)
                _apply_geo(row, geo)
                row.location_hash = hashes[key]
                row.verifier_version = VERIFIER_VERSION
                row.attempts = 0
                row.last_step = "intake"
                row.last_error = None
                row.next_attempt_at = None if geo.resolved else now
                row.source_url = (raw.url or None) and raw.url[:500]
                row.updated_at = now
                row.verified_at = now if geo.resolved else None
                session.add(row)
                out[key] = geo
                _bump(f"intake:{geo.status}")
            session.commit()
    except Exception as e:
        log.warning("geography capture failed for %d posting(s): %s", len(keys), e)
        # Still answer from memory so the per-user door decides correctly.
        for key in keys:
            if key not in out:
                out[key] = derive(todo[key])
    for key in to_redecide:
        try:
            redecide_copies(key[0], key[1], out[key])
        except Exception as e:
            log.debug("redecide after evidence change failed: %s", e)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Per-user verdicts on the copies of one posting
# ═════════════════════════════════════════════════════════════════════════════

def stamp_ineligible(job, decision: Decision, now: Optional[datetime] = None) -> None:
    """Write the verdict onto an UNSCORED copy so it leaves the queue with no
    LLM call. Callers hold the session and commit."""
    now = now or datetime.utcnow()
    job.eligibility = INELIGIBLE
    job.eligibility_reason = decision.reason[:200]
    if job.rerank_score is None:
        job.rerank_score = INELIGIBLE_STAMP_SCORE
        job.rerank_reasoning = (INELIGIBLE_REASON_PREFIX + decision.reason)[:500]
        job.scored_at = now


def redecide_copies(source: str, external_id: str, geo: Geography,
                    prefs_cache: Optional[dict] = None) -> int:
    """Re-decide every user copy of one posting from its (new) geography.

    Unscored copies are stamped out when INELIGIBLE and released from the
    hold when ELIGIBLE. Copies already scored keep their score but carry the
    new verdict, which `slate.place()` refuses at delivery — a fit score never
    overrides a hard eligibility failure. Applications already on a board are
    left alone (see docs/GEO_ELIGIBILITY.md, limitations).
    """
    from sqlalchemy import or_
    from sqlmodel import select
    from app.common.tenant_prefs import geo_prefs_for_user
    from app.db.init_db import get_session
    from app.db.models import Job, JobSource
    from app.discovery.pipeline import SHARED_POOL_USER

    try:
        src_enum = JobSource(source)
    except ValueError:
        return 0
    cache = prefs_cache if prefs_cache is not None else {}
    changed = 0
    now = datetime.utcnow()
    try:
        with get_session() as session:
            rows = session.exec(
                select(Job).where(
                    Job.source == src_enum, Job.external_id == str(external_id),
                    or_(Job.user_id.is_(None), Job.user_id != SHARED_POOL_USER))
            ).all()
            for job in rows:
                uid = job.user_id or "local"
                prefs = cache.get(uid)
                if prefs is None:
                    prefs = cache[uid] = geo_prefs_for_user(job.user_id)
                d = decide(geo, prefs)
                if d.status == INELIGIBLE:
                    if job.eligibility != INELIGIBLE or job.eligibility_reason != d.reason[:200]:
                        stamp_ineligible(job, d, now)
                        session.add(job)
                        changed += 1
                        _bump("copies_stamped_ineligible")
                else:
                    if job.eligibility != d.status or job.eligibility_reason != d.reason[:200]:
                        job.eligibility = d.status
                        job.eligibility_reason = d.reason[:200]
                        session.add(job)
                        changed += 1
                        _bump("copies_released" if d.status == ELIGIBLE else "copies_still_unknown")
            if changed:
                session.commit()
    except Exception as e:
        log.debug("redecide_copies failed for %s: %s", source, e)
    return changed


# ═════════════════════════════════════════════════════════════════════════════
# Step B — the posting's own page / official ATS detail endpoint
# ═════════════════════════════════════════════════════════════════════════════

# Boards whose Job.url is a permalink to ONE posting on a public page we may
# read. Aggregators are excluded: their url is a redirect or a search link,
# and LinkedIn/Indeed are hands-off by policy (discovery-only links).
_FETCHABLE_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable",
    "recruitee", "personio", "bamboohr", "breezy", "pinpoint", "rippling",
    "join", "teamtailor", "remotive", "themuse", "arbeitnow", "jobicy",
    "weworkremotely",
})
_HANDS_OFF_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "google.com")

_GREENHOUSE_RE = re.compile(
    r"(?:boards|job-boards)\.(?:greenhouse\.io|eu\.greenhouse\.io)/([^/]+)/jobs/(\d+)", re.I)
_LEVER_RE = re.compile(r"jobs\.(?:eu\.)?lever\.co/([^/]+)/([0-9a-f-]{36})", re.I)
_JSONLD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.S | re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_PAGE_BYTES = 400_000


@dataclass
class PageEvidence:
    geo: Optional[Geography] = None    # the resolved geography, when the page decided it
    text: str = ""                     # bounded page text for step C
    how: str = ""                      # ats_detail | page_jsonld | page_text | error:<why> | skipped:<why>
    field: str = ""                    # URL that decided it


def _fetch(url: str, timeout: float) -> Tuple[Optional[int], str, Optional[str]]:
    """One bounded, SSRF-guarded GET. Returns (status, body, error)."""
    import httpx
    from app.common.ssrf import guarded_request
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False,
                          headers={"User-Agent": settings.liveness_user_agent}) as client:
            r, err = guarded_request(client, "GET", url)
            if r is None:
                return None, "", err or "blocked"
            ctype = (r.headers.get("content-type") or "").lower()
            if not (ctype.startswith("text") or "json" in ctype):
                return r.status_code, "", None
            return r.status_code, r.text[:_PAGE_BYTES], None
    except Exception as e:
        return None, "", type(e).__name__


def _geo_from_jsonld(html: str) -> Tuple[Optional[GeoEvidence], str]:
    """GeoEvidence from a schema.org JobPosting block, or None."""
    for m in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if "@graph" in item and isinstance(item["@graph"], list):
                items.extend(x for x in item["@graph"] if isinstance(x, dict))
                continue
            t = item.get("@type")
            if t != "JobPosting" and not (isinstance(t, list) and "JobPosting" in t):
                continue
            ev = GeoEvidence()
            locs = item.get("jobLocation") or []
            locs = locs if isinstance(locs, list) else [locs]
            for loc in locs:
                addr = loc.get("address") if isinstance(loc, dict) else None
                if isinstance(addr, str):
                    ev.sites.append(addr.strip())
                    continue
                if not isinstance(addr, dict):
                    continue
                parts = [str(addr.get(k) or "").strip() for k in
                         ("addressLocality", "addressRegion", "addressCountry")]
                site = ", ".join(p for p in parts if p)
                if site:
                    ev.sites.append(site)
                if parts[2] and not ev.country:
                    ev.country, ev.country_field = parts[2], "jobLocation.address.addressCountry"
            ev.sites_field = "jobLocation.address"
            if str(item.get("jobLocationType") or "").upper() == "TELECOMMUTE":
                ev.work_mode, ev.work_mode_field = "remote", "jobLocationType"
            req = item.get("applicantLocationRequirements") or []
            req = req if isinstance(req, list) else [req]
            names = [str(r.get("name") or "").strip() for r in req if isinstance(r, dict)]
            names = [n for n in names if n]
            if names:
                ev.remote_regions = "; ".join(names)
                ev.remote_regions_field = "applicantLocationRequirements"
            if ev.sites or ev.country or ev.work_mode or ev.remote_regions:
                return ev, "page_jsonld"
    return None, ""


def _ats_detail(url: str, timeout: float) -> Tuple[Optional[GeoEvidence], str, str]:
    """Official detail endpoints for the ATSes that expose one per posting."""
    m = _GREENHOUSE_RE.search(url or "")
    if m:
        api = f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}"
        status, body, err = _fetch(api, timeout)
        if status == 200 and body:
            try:
                d = json.loads(body)
            except ValueError:
                return None, "", api
            ev = GeoEvidence(sites_field="location.name+offices[].location")
            name = (d.get("location") or {}).get("name") if isinstance(d.get("location"), dict) else None
            if isinstance(name, str) and name.strip():
                ev.sites.extend(_sites_from_location(name))
            for off in (d.get("offices") or []):
                if isinstance(off, dict):
                    v = off.get("location") or off.get("name")
                    if isinstance(v, str) and v.strip() and v.strip() not in ev.sites:
                        ev.sites.append(v.strip())
            for entry in (d.get("metadata") or []):
                if isinstance(entry, dict) and "workplace" in str(entry.get("name") or "").lower():
                    v = str(entry.get("value") or "").lower()
                    mode = "hybrid" if "hybrid" in v else "remote" if "remote" in v else \
                        "onsite" if ("site" in v or "office" in v) else ""
                    if mode:
                        ev.work_mode, ev.work_mode_field = mode, f"metadata[{entry.get('name')}]"
            return (ev if (ev.sites or ev.work_mode) else None), "ats_detail", api
        return None, f"error:{err or status}", api
    m = _LEVER_RE.search(url or "")
    if m:
        api = f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}"
        status, body, err = _fetch(api, timeout)
        if status == 200 and body:
            try:
                d = json.loads(body)
            except ValueError:
                return None, "", api
            ev = lever_geo(d)
            return (ev if (ev.sites or ev.country or ev.work_mode) else None), "ats_detail", api
        return None, f"error:{err or status}", api
    return None, "", ""


def lever_geo(j: dict) -> GeoEvidence:
    """Lever posting → GeoEvidence (shared with the board scraper). Every
    site in `allLocations`, the structured `country` code and `workplaceType`."""
    cats = j.get("categories") or {}
    ev = GeoEvidence()
    all_locs = cats.get("allLocations")
    if isinstance(all_locs, list) and all_locs:
        ev.sites = [str(s).strip() for s in all_locs if isinstance(s, str) and str(s).strip()]
        ev.sites_field = "categories.allLocations"
    elif isinstance(cats.get("location"), str) and cats.get("location").strip():
        ev.sites = [cats["location"].strip()]
        ev.sites_field = "categories.location"
    country = j.get("country")
    if isinstance(country, str) and country.strip():
        ev.country, ev.country_field = country.strip(), "country"
    wt = str(j.get("workplaceType") or "").strip().lower()
    if wt:
        ev.work_mode = "remote" if wt == "remote" else "hybrid" if wt == "hybrid" else \
            "onsite" if wt in ("onsite", "on-site", "on_site", "unspecified_onsite") else ""
        ev.work_mode_field = "workplaceType"
    return ev


def page_evidence(url: str, source: str, timeout: Optional[float] = None) -> PageEvidence:
    """Step B for one posting. Never raises; a failure is `how="error:…"` and
    inconclusive — it establishes nothing and closes nothing."""
    timeout = float(settings.geo_verify_fetch_timeout_seconds if timeout is None else timeout)
    src = source.value if hasattr(source, "value") else str(source)
    if not settings.geo_verify_fetch_enabled:
        return PageEvidence(how="skipped:disabled")
    host = (urlparse(url or "").hostname or "").lower()
    if not url or src not in _FETCHABLE_SOURCES or any(h in host for h in _HANDS_OFF_HOSTS):
        _bump("fetch_skipped_source")
        return PageEvidence(how="skipped:source")

    _bump("fetch_attempted")
    started = time.monotonic()
    ev, how, api = _ats_detail(url, timeout)
    if ev is not None:
        _bump("fetch_resolved_ats_detail")
        probe = RawJob(source=src, external_id="", company="", title="", location="",
                       remote=(ev.work_mode == "remote"), url=url, description="", geo=ev)
        geo = derive(probe)
        geo.evidence_source = "ats_detail"
        geo.evidence_field = api
        return PageEvidence(geo=geo if geo.resolved else None, how=how, field=api)
    if how.startswith("error:"):
        _bump("fetch_failed")
        return PageEvidence(how=how, field=api)

    status, body, err = _fetch(url, timeout)
    _bump("fetch_latency_ms_total", int((time.monotonic() - started) * 1000))
    if err or status is None:
        _bump("fetch_failed")
        return PageEvidence(how=f"error:{err or 'no_response'}", field=url)
    if status != 200 or not body:
        _bump("fetch_failed")
        return PageEvidence(how=f"error:http_{status}", field=url)
    text = _ANY_TAG_RE.sub(" ", _TAG_RE.sub(" ", body))
    text = " ".join(text.split())[:30_000]
    ev, how = _geo_from_jsonld(body)
    if ev is not None:
        probe = RawJob(source=src, external_id="", company="", title="", location="",
                       remote=(ev.work_mode == "remote"), url=url, description=text, geo=ev)
        geo = derive(probe)
        if geo.resolved or geo.status == CONFLICT:
            _bump("fetch_resolved_jsonld")
            geo.evidence_source = "page_jsonld"
            geo.evidence_field = url
            return PageEvidence(geo=geo, text=text, how="page_jsonld", field=url)
    _bump("fetch_no_structured_evidence")
    return PageEvidence(text=text, how="page_text", field=url)


# ═════════════════════════════════════════════════════════════════════════════
# Step C — one bounded structured-extraction call
# ═════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = (
    "You extract WHERE a job is performed from excerpts of a job posting.\n"
    "Use ONLY the excerpts. Never infer a country from a company name, a currency, "
    "a department, a time zone alone, or the job board.\n"
    "Return one JSON object: {\"country\": <country name or null>, "
    "\"locations\": [<city or site strings named in the excerpts>], "
    "\"work_mode\": \"remote\"|\"hybrid\"|\"onsite\"|\"unknown\", "
    "\"permitted_regions\": [<countries or regions candidates must live in, e.g. "
    "\"United States\", \"EU\", \"worldwide\">], "
    "\"evidence_quote\": <a VERBATIM excerpt that states the country or region, or null>, "
    "\"conflicts\": [<contradictions between excerpts, if any>]}.\n"
    "If the excerpts do not state a country or region, return country null, "
    "permitted_regions [] and evidence_quote null. Do not guess."
)


@dataclass
class LlmResult:
    geo: Optional[Geography] = None
    provider: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    how: str = ""                      # llm | rejected_quote | no_evidence | skipped:<why> | error:<why>


def _cheapest_backend() -> Optional[Tuple[str, str, object]]:
    """(provider, model, client) — the cheapest CONFIGURED and AVAILABLE model
    for ~1.2k input / 120 output tokens, priced from the spend table. A model
    with no price row sorts last: we do not pick what we cannot cost."""
    from app.analytics.spend import estimate_cost
    from app.common.llm import shared_anthropic, shared_openai
    from app.matching.reranker import provider_available
    timeout = float(settings.geo_verify_llm_timeout_seconds)
    candidates = []
    for provider, model, client in (
            ("openai", settings.geo_verify_model_openai, shared_openai(timeout=timeout, max_retries=0)),
            ("anthropic", settings.geo_verify_model_anthropic, shared_anthropic(timeout=timeout, max_retries=0))):
        if client is None or not model or not provider_available(provider):
            continue
        est = estimate_cost(model, {"input": 1200, "output": 120})
        candidates.append((est if est is not None else 9e9, provider, model, client))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, provider, model, client = candidates[0]
    return provider, model, client


def _call_llm(provider: str, model: str, client, excerpts: str) -> Tuple[str, dict, str]:
    """Returns (text, usage, served_model). Raises on provider failure."""
    from app.common.llm import sampling
    from app.matching.reranker import _usage_from_anthropic, _usage_from_openai
    user = "Excerpts:\n" + excerpts + "\n\nReturn the JSON object."
    if provider == "openai":
        resp = client.chat.completions.create(
            model=model, max_tokens=220, temperature=0,
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"})
        return (resp.choices[0].message.content or "", _usage_from_openai(resp) or {},
                getattr(resp, "model", None) or model)
    resp = client.messages.create(
        model=model, max_tokens=220, system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}], **sampling(model, 0.0))
    return (resp.content[0].text or "", _usage_from_anthropic(resp) or {},
            getattr(resp, "model", None) or model)


def _norm_text(s: str) -> str:
    return " ".join((s or "").casefold().replace("’", "'").split())


def quote_is_verbatim(quote: str, supplied: str) -> bool:
    """The model's evidence must appear in what it was shown. A 40-char prefix
    is accepted too, since models trim long sentences; anything shorter is not
    evidence of anything."""
    q, s = _norm_text(quote), _norm_text(supplied)
    if len(q) < 8 or not s:
        return False
    return q in s or (len(q) >= 40 and q[:40] in s)


def interpret_llm_answer(text: str, supplied: str) -> Tuple[Optional[Geography], str]:
    """Parse + validate one answer. Returns (Geography or None, how)."""
    try:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1]) if start != -1 and end != -1 else {}
    except (ValueError, TypeError):
        return None, "error:unparseable"
    if not isinstance(data, dict):
        return None, "error:unparseable"
    country_raw = data.get("country")
    regions_raw = data.get("permitted_regions") or []
    regions_raw = regions_raw if isinstance(regions_raw, list) else [regions_raw]
    quote = data.get("evidence_quote")
    conflicts = [str(c)[:160] for c in (data.get("conflicts") or []) if c][:4] \
        if isinstance(data.get("conflicts"), list) else []
    claims = bool(country_raw) or any(r for r in regions_raw)
    if not claims and not conflicts:
        return None, "no_evidence"
    if claims and not (isinstance(quote, str) and quote_is_verbatim(quote, supplied)):
        return None, "rejected_quote"

    countries: List[str] = []
    c = resolve_country_value(country_raw) if isinstance(country_raw, str) else ""
    if c:
        countries.append(c)
    regions: List[str] = []
    for r in regions_raw:
        if not isinstance(r, str):
            continue
        cc, rr = _resolve_place(r)
        if cc and cc not in countries:
            countries.append(cc)
        if cc and cc not in regions:
            regions.append(cc)
        if rr and rr not in regions:
            regions.append(rr)
    if claims and not countries and not regions:
        # It named a place the tables do not know: unknown stays unknown.
        return None, "no_evidence"
    mode = str(data.get("work_mode") or "").lower()
    mode = mode if mode in ("remote", "hybrid", "onsite") else ""
    sites = [str(s).strip() for s in (data.get("locations") or []) if isinstance(s, str) and str(s).strip()][:16] \
        if isinstance(data.get("locations"), list) else []
    status = CONFLICT if conflicts else (RESOLVED if (countries or regions) else STATUS_UNKNOWN)
    geo = Geography(status=status, countries=countries, sites=sites, work_mode=mode,
                    remote_regions=regions, conflicts=conflicts, evidence_source="llm",
                    evidence_quote=(quote or "")[:300])
    return geo, "llm"


def llm_extract(excerpts: str) -> LlmResult:
    """Step C. Every skip reason is named so a zero can be explained."""
    if not settings.geo_verify_llm_enabled:
        return LlmResult(how="skipped:disabled")
    if not excerpts or len(excerpts.strip()) < 40:
        _bump("llm_skipped_no_evidence")
        return LlmResult(how="no_evidence")
    from app.matching.reranker import (
        _is_exhaustion_error, _mark_provider_down, _note_provider_ok, llm_budget_exhausted,
    )
    if llm_budget_exhausted():
        _bump("llm_skipped_platform_budget")
        return LlmResult(how="skipped:platform_budget")
    cap = int(settings.geo_verify_llm_daily_cap or 0)
    if cap > 0 and _llm_calls_today() >= cap:
        _bump("llm_skipped_daily_cap")
        return LlmResult(how="skipped:daily_cap")
    backend = _cheapest_backend()
    if backend is None:
        _bump("llm_skipped_no_provider")
        return LlmResult(how="skipped:no_provider")
    provider, model, client = backend
    try:
        _register_llm_call()
        _bump("llm_attempted")
        text, usage, served = _call_llm(provider, model, client, excerpts)
        _note_provider_ok(provider)
    except Exception as e:
        if _is_exhaustion_error(str(e).lower()):
            _mark_provider_down(provider, error=str(e))
        _bump("llm_failed")
        log.debug("geo llm call failed on %s: %s", provider, e)
        return LlmResult(provider=provider, model=model, how=f"error:{type(e).__name__}")
    # Spend is recorded for the call that happened, whatever the answer says.
    from app.analytics.spend import buffer_llm_spend, estimate_cost
    from app.discovery.pipeline import SHARED_POOL_USER
    try:
        buffer_llm_spend(SHARED_POOL_USER, "geo_verify", provider=provider, model=served, usage=usage)
    except Exception:
        pass
    cost = estimate_cost(served, usage) or 0.0
    _bump("llm_cost_microusd", int(round(cost * 1_000_000)))
    _bump("llm_input_tokens", int((usage or {}).get("input", 0) or 0))
    _bump("llm_output_tokens", int((usage or {}).get("output", 0) or 0))
    geo, how = interpret_llm_answer(text, excerpts)
    _bump(f"llm_{how.split(':')[0]}")
    return LlmResult(geo=geo, provider=provider, model=served, usage=usage or {}, cost_usd=cost, how=how)


# ═════════════════════════════════════════════════════════════════════════════
# The bounded sweep — runs inside the scoring cycle, before the work list
# ═════════════════════════════════════════════════════════════════════════════

class Budget:
    """Wall-clock and count allowance for one sweep."""

    def __init__(self, seconds: Optional[float] = None, items: Optional[int] = None,
                 deadline: Optional[float] = None):
        self.seconds = float(settings.geo_verify_budget_seconds_per_cycle if seconds is None else seconds)
        self.items = int(settings.geo_verify_max_per_cycle if items is None else items)
        self.deadline = deadline
        self.started = time.monotonic()
        self.done = 0

    @property
    def exhausted(self) -> bool:
        if self.items and self.done >= self.items:
            return True
        if self.seconds > 0 and time.monotonic() - self.started >= self.seconds:
            return True
        # Leave the cycle a margin: never start a fetch the deadline would cut.
        return bool(self.deadline and time.monotonic() >= self.deadline - 5.0)


def _pending_rows(limit: int) -> list:
    """Unresolved geography rows whose retry is due AND that still have an open,
    unscored, held copy somewhere. Two queries on purpose: Job.source is an
    enum column and a cross-table join on it needs a cast on Postgres."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job, JobGeography
    from app.discovery.pipeline import SHARED_POOL_USER
    from sqlalchemy import or_

    now = datetime.utcnow()
    max_attempts = int(settings.geo_verify_max_attempts or 0)
    with get_session() as session:
        q = (select(JobGeography)
             .where(JobGeography.status != RESOLVED,
                    or_(JobGeography.next_attempt_at.is_(None), JobGeography.next_attempt_at <= now)))
        if max_attempts > 0:
            q = q.where(JobGeography.attempts < max_attempts)
        rows = session.exec(q.order_by(JobGeography.next_attempt_at, JobGeography.id)
                            .limit(max(1, limit) * 4)).all()
        if not rows:
            return []
        ext_ids = [r.external_id for r in rows]
        live: set = set()
        for start in range(0, len(ext_ids), 300):
            chunk = ext_ids[start:start + 300]
            for src, ext in session.exec(
                    select(Job.source, Job.external_id).where(
                        Job.external_id.in_(chunk), Job.eligibility == UNKNOWN,
                        Job.rerank_score.is_(None), Job.is_closed == False,  # noqa: E712
                        or_(Job.user_id.is_(None), Job.user_id != SHARED_POOL_USER))).all():
                live.add((src.value if hasattr(src, "value") else str(src), ext))
        out = [r for r in rows if (r.source, r.external_id) in live]
        for r in out:
            session.expunge(r)
        return out[:max(1, limit)]


def _posting_text(source: str, external_id: str) -> Tuple[str, str]:
    """(url, description[:TEXT_SCAN_CHARS]) from any copy of the posting —
    shared row first. Projected: the description is truncated in SQL."""
    from sqlalchemy import func
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job, JobSource
    from app.discovery.pipeline import SHARED_POOL_USER
    try:
        src_enum = JobSource(source)
    except ValueError:
        return "", ""
    with get_session() as session:
        q = (select(Job.url, func.substr(Job.description, 1, TEXT_SCAN_CHARS), Job.user_id)
             .where(Job.source == src_enum, Job.external_id == str(external_id)).limit(6))
        rows = session.exec(q).all()
    if not rows:
        return "", ""
    rows.sort(key=lambda r: 0 if r[2] == SHARED_POOL_USER else 1)
    return rows[0][0] or "", rows[0][1] or ""


def _record_attempt(row_id: int, geo: Optional[Geography], *, step: str, error: str,
                    llm: Optional[LlmResult], duration_ms: int) -> Optional[Geography]:
    """Write one verification outcome. Returns the geography now on the row."""
    from app.db.init_db import get_session
    from app.db.models import JobGeography
    now = datetime.utcnow()
    with get_session() as session:
        row = session.get(JobGeography, row_id)
        if row is None:
            return None
        row.attempts = (row.attempts or 0) + 1
        row.last_step = step
        row.last_error = (error or None) and error[:200]
        row.duration_ms = int(duration_ms)
        row.updated_at = now
        if llm is not None and llm.provider:
            row.provider, row.model = llm.provider, llm.model
            row.input_tokens = int((llm.usage or {}).get("input", 0) or 0)
            row.output_tokens = int((llm.usage or {}).get("output", 0) or 0)
            row.est_cost_usd = float(llm.cost_usd or 0.0)
        if geo is not None and (geo.resolved or geo.status == CONFLICT):
            _apply_geo(row, geo)
            row.verified_at = now
            row.next_attempt_at = None if geo.resolved else now + timedelta(days=365)
        else:
            hours = float(settings.geo_verify_retry_hours or 0) * (2 ** max(0, row.attempts - 1))
            row.next_attempt_at = now + timedelta(hours=max(0.25, hours))
        session.add(row)
        session.commit()
        return _row_to_geo(row)


def verify_one(row) -> Tuple[Optional[Geography], str]:
    """Steps B then C for one posting, outside any session. Returns
    (geography or None, what happened)."""
    started = time.monotonic()
    url, description = _posting_text(row.source, row.external_id)
    page = page_evidence(url, row.source)
    if page.geo is not None:
        ms = int((time.monotonic() - started) * 1000)
        geo = _record_attempt(row.id, page.geo, step="page", error="", llm=None, duration_ms=ms)
        _bump("verified_postings")
        return geo, page.how
    # Only text the rules could not read earns a model call: excerpts from the
    # description and, when we fetched it, the page. Never the whole posting.
    limit = int(settings.geo_verify_llm_max_chars or 1500)
    excerpts = location_excerpts(description, limit)
    if page.text and len(excerpts) < limit:
        more = location_excerpts(page.text, limit - len(excerpts))
        if more:
            excerpts = (excerpts + "\n" + more).strip()
    llm = llm_extract(excerpts)
    ms = int((time.monotonic() - started) * 1000)
    err = "" if llm.how in ("llm", "no_evidence") else llm.how
    if page.how.startswith("error:") and not err:
        err = page.how
    geo = _record_attempt(row.id, llm.geo, step="llm" if llm.how not in ("no_evidence",) or page.text else "page",
                          error=err, llm=llm, duration_ms=ms)
    if llm.geo is not None:
        _bump("verified_postings")
    return geo, llm.how


def verify_pending(deadline: Optional[float] = None, budget: Optional[Budget] = None) -> dict:
    """The sweep. Bounded by `Budget` (count, seconds, cycle deadline). Every
    resolved posting re-decides all of its copies; an unresolved one is put
    back with exponential backoff. Returns the counters it moved."""
    stats = {"pending_examined": 0, "resolved": 0, "still_unknown": 0, "copies_updated": 0}
    if not settings.geo_verify_enabled:
        return stats
    budget = budget or Budget(deadline=deadline)
    try:
        rows = _pending_rows(budget.items)
    except Exception as e:
        log.warning("geo verification: pending lookup failed: %s", e)
        return stats
    prefs_cache: dict = {}
    for row in rows:
        if budget.exhausted:
            _bump("deferred_budget")
            break
        stats["pending_examined"] += 1
        budget.done += 1
        try:
            geo, how = verify_one(row)
        except Exception as e:
            log.debug("geo verification failed for one posting: %s", e)
            _bump("verify_errored")
            continue
        if geo is not None and (geo.resolved or geo.status == CONFLICT):
            stats["resolved" if geo.resolved else "still_unknown"] += 1
            stats["copies_updated"] += redecide_copies(row.source, row.external_id, geo, prefs_cache)
        else:
            stats["still_unknown"] += 1
    snap = metrics_snapshot(reset=True)
    if any(v for k, v in snap.items() if k not in ("llm_cost_usd", "cost_per_verified_posting_usd")):
        stats["geo_verify"] = snap
    return stats
