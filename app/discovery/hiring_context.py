"""Persist hiring context ONCE per distinct posting.

The write path deliberately does not know which user triggered it. Context is
a property of the posting, so it is keyed by (source, external_id) — the pair
that per-user copies preserve verbatim (`strategy/adoption.py:212`). Twelve
users adopting the same posting produce twelve `job` rows and exactly one
`job_hiring_context` row.

Rules this module enforces, in order of importance:

  1. A value is never stored without an evidence entry. `evidence_json` is the
     record of how each field was established, and the UI renders its label
     from that class — so an unproven value cannot be displayed as a fact.
  2. A weaker observation never overwrites a stronger one. Adoption rebuilds
     RawJobs from stored columns and therefore carries no context at all; that
     must not blank a row the scraper filled in.
  3. Nothing here calls an LLM or makes a network request.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, Iterable, List

from app.discovery.base import (
    EVIDENCE_DIRECT_PERSON,
    EVIDENCE_NONE,
    EVIDENCE_ORG_ENTITY,
    EVIDENCE_SELF_IDENTIFIED,
    EVIDENCE_STRUCTURED_PERSON,
    EVIDENCE_SUGGESTED,
    EVIDENCE_TEAM_OR_DEPARTMENT,
    EVIDENCE_TITLE_ONLY,
    ContextField,
    RawJob,
)

log = logging.getLogger(__name__)

EXTRACTOR_VERSION = 1

#: Columns on JobHiringContext that a ContextField may target. Anything else
#: an adapter emits is dropped with a warning rather than silently ignored.
CONTEXT_FIELDS = (
    "department", "team", "division", "hiring_entity", "recruiting_agency",
    "requisition_id", "ats", "reporting_title", "reporting_manager_name",
    "recruiter_name", "posting_creator_name", "contact_email",
)

#: How much a claim is worth. A structured ATS field outranks prose; prose that
#: names a person outranks prose that only names a title; a guess outranks
#: nothing. Used only to decide whether a new observation may overwrite a
#: stored one — never to decide what to show the user.
_RANK = {
    EVIDENCE_DIRECT_PERSON: 60,
    EVIDENCE_STRUCTURED_PERSON: 50,
    EVIDENCE_SELF_IDENTIFIED: 40,
    EVIDENCE_TITLE_ONLY: 30,
    EVIDENCE_ORG_ENTITY: 25,
    EVIDENCE_TEAM_OR_DEPARTMENT: 25,
    EVIDENCE_SUGGESTED: 10,
    EVIDENCE_NONE: 0,
}


def evidence_rank(evidence: str | None) -> int:
    return _RANK.get(evidence or EVIDENCE_NONE, 0)


def _assert_evidence_constants_match_enum() -> None:
    """base.py keeps evidence names as plain strings so scrapers do not import
    SQLModel. That duplication is only safe while the two agree."""
    from app.db.models import EvidenceClass
    ours = set(_RANK)
    theirs = {e.value for e in EvidenceClass}
    if ours != theirs:
        raise AssertionError(
            f"evidence constants drifted: only in discovery={ours - theirs}, "
            f"only in models={theirs - ours}")


def clean(value: object, limit: int = 200) -> str:
    """Normalise an upstream value to a storable string, or '' to skip it."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    out = " ".join(value.split()).strip(" ,;:-–—|")
    # An ATS that returns a placeholder is returning nothing.
    if out.lower() in ("", "n/a", "na", "none", "null", "-", "unknown", "tbd",
                       "not specified", "not applicable"):
        return ""
    return out[:limit]


def put(ctx: Dict[str, ContextField], key: str, value: object, evidence: str,
        field: str, quote: str = "") -> None:
    """Add a context field if the value survives cleaning. No-op otherwise, so
    adapters can call it unconditionally on keys that are usually absent."""
    if key not in CONTEXT_FIELDS:
        log.warning("hiring_context: unknown field %r ignored", key)
        return
    v = clean(value)
    if not v:
        return
    prior = ctx.get(key)
    if prior and evidence_rank(prior.evidence) >= evidence_rank(evidence):
        return
    ctx[key] = ContextField(value=v, evidence=evidence, field=field, quote=quote[:300])


def merge_into(stored_values: dict, stored_evidence: dict,
               incoming: Dict[str, ContextField]) -> tuple[dict, dict, bool]:
    """Apply `incoming` over a stored row. Returns (values, evidence, changed).

    A field is written only when it is currently empty or the new claim ranks
    strictly higher. This is what makes the writer safe to call repeatedly from
    every lane and every user's adoption pass.
    """
    values = dict(stored_values)
    evidence = dict(stored_evidence)
    changed = False
    for key, cf in incoming.items():
        if key not in CONTEXT_FIELDS:
            continue
        have = values.get(key)
        have_rank = evidence_rank((evidence.get(key) or {}).get("evidence"))
        if have and have_rank >= evidence_rank(cf.evidence):
            continue
        if have == cf.value and have_rank == evidence_rank(cf.evidence):
            continue
        values[key] = cf.value
        evidence[key] = {"evidence": cf.evidence, "field": cf.field}
        if cf.quote:
            evidence[key]["quote"] = cf.quote
        changed = True
    return values, evidence, changed


#: Longest description prefix the text extractor scans. Reporting lines sit
#: after the responsibilities section, so this must be far past the 800-char
#: retrieval projection (`matcher._candidate_columns`) — that projection is why
#: extraction has to happen HERE, at ingest, on the full stored text. The cap
#: exists only to bound the cost of a pathological posting.
TEXT_SCAN_CHARS = 20000

#: Assertion.relationship → (context column, evidence when a person is named,
#: evidence when only a title/org is named). None means "do not map": a
#: role-unspecified job contact is not a recruiter, and saying so would be the
#: exact mislabelling this whole design exists to prevent.
_REL_TO_FIELD = {
    "department": ("department", EVIDENCE_TEAM_OR_DEPARTMENT, EVIDENCE_TEAM_OR_DEPARTMENT),
    "team": ("team", EVIDENCE_TEAM_OR_DEPARTMENT, EVIDENCE_TEAM_OR_DEPARTMENT),
    "recruiter": ("recruiter_name", EVIDENCE_DIRECT_PERSON, None),
    "job_poster": ("posting_creator_name", EVIDENCE_DIRECT_PERSON, None),
    "recruiting_agency": ("recruiting_agency", EVIDENCE_ORG_ENTITY, EVIDENCE_ORG_ENTITY),
    "hiring_company_named_in_ad": ("hiring_entity", EVIDENCE_ORG_ENTITY, EVIDENCE_ORG_ENTITY),
}


def apply_text_extraction(raw_jobs: Iterable[RawJob]) -> int:
    """Merge deterministic full-description evidence into each job's context.

    Runs before `record_context`, so structured ATS fields still win: `put`
    only replaces a value when the new evidence ranks strictly higher, and a
    STRUCTURED/TEAM_OR_DEPARTMENT claim outranks anything text can offer for
    the same column.

    Regex only. No LLM, no network. Returns the number of jobs that gained at
    least one field.
    """
    from app.intelligence.hiring_contacts import extract_from_text

    touched = 0
    for r in raw_jobs:
        text = (r.description or "")[:TEXT_SCAN_CHARS]
        if len(text) < 40:
            continue
        before = len(r.context)
        try:
            assertions = extract_from_text(text, source_url=r.url or "")
        except Exception as e:      # a regex must never take down discovery
            log.debug("text extraction failed for %s/%s: %s", r.source, r.external_id, e)
            continue
        for a in assertions:
            if a.relationship == "reporting_manager":
                # The one place a person and a title diverge into different
                # columns. A title is not a person and never becomes one.
                if a.name:
                    evidence = (EVIDENCE_SELF_IDENTIFIED
                                if a.evidence_type == "self_identified"
                                else EVIDENCE_DIRECT_PERSON)
                    put(r.context, "reporting_manager_name", a.name, evidence,
                        f"description:{a.evidence_type}", a.quote)
                    if a.title:
                        put(r.context, "reporting_title", a.title,
                            EVIDENCE_TITLE_ONLY, "description:reports_to", a.quote)
                elif a.title:
                    put(r.context, "reporting_title", a.title, EVIDENCE_TITLE_ONLY,
                        "description:reports_to", a.quote)
                continue
            if a.relationship == "contact_email":
                # A shared careers@ mailbox is an application route, not a
                # person, so only a personal address is stored.
                if a.evidence_type != "generic_mailbox":
                    put(r.context, "contact_email", a.quote, EVIDENCE_DIRECT_PERSON,
                        "description:email", a.quote)
                continue
            mapped = _REL_TO_FIELD.get(a.relationship)
            if not mapped:
                continue
            column, person_evidence, org_evidence = mapped
            value = a.name or a.organization
            evidence = person_evidence if a.name else org_evidence
            if not value or not evidence:
                continue
            put(r.context, column, value, evidence,
                f"description:{a.relationship}", a.quote)
        if len(r.context) > before:
            touched += 1
    return touched


def already_captured(pairs: List[tuple]) -> set:
    """Which (source, external_id) already have context at the CURRENT version.

    This exists because of a measured production regression. The hook runs
    inside `_upsert`, and the pulse lane re-sees the same 4,000-5,000 postings
    every tick — so extracting from every candidate meant re-running the regex
    battery over the full description of thousands of unchanged postings, every
    tick, forever. Pulse `upsert_shared` p50 went from ~810ms to ~2,200ms and
    the lane, which is already capacity-limited, deferred more boards.

    One bulk indexed SELECT answers "have we already done this posting?", and
    the expensive work then runs once per posting instead of once per sighting.

    A posting whose description later CHANGES is not re-extracted until
    EXTRACTOR_VERSION is bumped. That is the deliberate trade: org-unit fields
    essentially never change on a live posting, and re-reading every posting
    forever to catch the rare edit is what caused the regression.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobHiringContext

    if not pairs:
        return set()
    done: set = set()
    wanted = set(pairs)
    try:
        with get_session() as session:
            for start in range(0, len(pairs), 300):
                chunk = pairs[start:start + 300]
                rows = session.exec(
                    select(JobHiringContext.source, JobHiringContext.external_id)
                    .where(JobHiringContext.source.in_([k[0] for k in chunk]),
                           JobHiringContext.external_id.in_([k[1] for k in chunk]),
                           JobHiringContext.extractor_version >= EXTRACTOR_VERSION)
                ).all()
                for src, ext in rows:
                    if (src, ext) in wanted:
                        done.add((src, ext))
    except Exception as e:
        # On failure do the work rather than skip it: correctness over cost.
        log.debug("already_captured lookup failed: %s", e)
        return set()
    return done


def capture(raw_jobs: List[RawJob]) -> tuple:
    """Extract and persist context for postings we have not done yet.

    Returns (rows_written, text_enriched, skipped_already_done).
    """
    if not raw_jobs:
        return 0, 0, 0
    pairs = sorted({(r.source, r.external_id) for r in raw_jobs if r.external_id})
    done = already_captured(pairs)
    todo = [r for r in raw_jobs if (r.source, r.external_id) not in done]
    if not todo:
        return 0, 0, len(raw_jobs)
    enriched = apply_text_extraction(todo)
    written = record_context(todo)
    return written, enriched, len(raw_jobs) - len(todo)


def record_context(raw_jobs: Iterable[RawJob]) -> int:
    """Upsert one context row per distinct (source, external_id).

    Returns the number of rows created or updated. Safe to call with jobs that
    carry no context — they are filtered out before any query runs, so the
    adoption path costs nothing.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobHiringContext

    # Collapse duplicates inside the batch first: the same posting can arrive
    # twice in one discovery pass, and one round trip is enough for both.
    wanted: Dict[tuple, Dict[str, ContextField]] = {}
    urls: Dict[tuple, str] = {}
    for r in raw_jobs:
        if not r.context:
            continue
        key = (r.source, r.external_id)
        merged = wanted.setdefault(key, {})
        for k, cf in r.context.items():
            prior = merged.get(k)
            if prior is None or evidence_rank(cf.evidence) > evidence_rank(prior.evidence):
                merged[k] = cf
        if r.url:
            urls.setdefault(key, r.url)
    if not wanted:
        return 0

    # Deterministic order. Sorting the keys means two lanes writing overlapping
    # batches take row locks in the same sequence, which is the property the
    # companyregistry deadlock post-mortem (CLAUDE.md) says every multi-row
    # write in this codebase must have.
    keys = sorted(wanted)
    written = 0
    now = datetime.utcnow()
    try:
        with get_session() as session:
            existing = {}
            for start in range(0, len(keys), 200):
                chunk = keys[start:start + 200]
                rows = session.exec(
                    select(JobHiringContext).where(
                        JobHiringContext.source.in_([k[0] for k in chunk]),
                        JobHiringContext.external_id.in_([k[1] for k in chunk]),
                    )
                ).all()
                for row in rows:
                    existing[(row.source, row.external_id)] = row
            for key in keys:
                incoming = wanted[key]
                row = existing.get(key)
                if row is None:
                    values, evidence, _ = merge_into({}, {}, incoming)
                    if not values:
                        continue
                    row = JobHiringContext(
                        source=key[0], external_id=key[1],
                        source_url=urls.get(key), observed_at=now, updated_at=now,
                        extractor_version=EXTRACTOR_VERSION,
                        evidence_json=json.dumps(evidence),
                    )
                    for k, v in values.items():
                        setattr(row, k, v)
                    session.add(row)
                    written += 1
                    continue
                try:
                    stored_evidence = json.loads(row.evidence_json or "{}")
                except (json.JSONDecodeError, TypeError):
                    stored_evidence = {}
                stored_values = {k: getattr(row, k, None) for k in CONTEXT_FIELDS}
                values, evidence, changed = merge_into(stored_values, stored_evidence, incoming)
                if not changed:
                    continue
                for k, v in values.items():
                    if v:
                        setattr(row, k, v)
                row.evidence_json = json.dumps(evidence)
                row.updated_at = now
                row.extractor_version = EXTRACTOR_VERSION
                if not row.source_url:
                    row.source_url = urls.get(key)
                session.add(row)
                written += 1
            session.commit()
    except Exception as e:      # never let context capture break discovery
        log.warning("record_context failed for %d postings: %s", len(keys), e)
        return 0
    return written


def load_context(pairs: List[tuple]) -> Dict[tuple, dict]:
    """Read context for [(source, external_id), …]. Returns {key: {...}}.

    Projected: the caller renders a small card and must not pull whole rows on
    a request path.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobHiringContext

    if not pairs:
        return {}
    out: Dict[tuple, dict] = {}
    with get_session() as session:
        for start in range(0, len(pairs), 200):
            chunk = pairs[start:start + 200]
            rows = session.exec(
                select(JobHiringContext).where(
                    JobHiringContext.source.in_([k[0] for k in chunk]),
                    JobHiringContext.external_id.in_([k[1] for k in chunk]),
                )
            ).all()
            for row in rows:
                if (row.source, row.external_id) not in set(chunk):
                    continue
                try:
                    evidence = json.loads(row.evidence_json or "{}")
                except (json.JSONDecodeError, TypeError):
                    evidence = {}
                out[(row.source, row.external_id)] = {
                    **{k: getattr(row, k, None) for k in CONTEXT_FIELDS},
                    "evidence": evidence,
                    "source_url": row.source_url,
                    "observed_at": row.observed_at,
                }
    return out
