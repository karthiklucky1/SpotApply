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

import hashlib
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


#: Marks a stored row that predates `content_hash`. Distinguishable from a real
#: hash (64 hex chars) and from "no row at all", which are three different
#: states with three different answers.
_NO_HASH = ""


def description_hash(text: str) -> str:
    """sha256 of the description this row's context was extracted from.

    Deliberately NOT read from `Job.content_hash`: this only ever compares a
    description against the one THIS module last extracted from, so it needs no
    agreement with the discovery pipeline's hash and gains no coupling to it.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def captured_state(pairs: List[tuple]) -> Dict[tuple, str]:
    """{(source, external_id): stored description hash} for rows at the CURRENT
    extractor version. `_NO_HASH` for a row written before hashing existed.

    This exists because of a measured production regression. The hook runs
    inside `_upsert`, and the pulse lane re-sees the same 4,000-5,000 postings
    every tick — so extracting from every candidate meant re-running the regex
    battery over the full description of thousands of unchanged postings, every
    tick, forever. Pulse `upsert_shared` p50 went from ~810ms to ~2,200ms and
    the lane, which is already capacity-limited, deferred more boards.

    One bulk indexed SELECT answers "have we already done this posting, and from
    WHICH description?", and the expensive work then runs once per posting per
    version of its text, instead of once per sighting.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobHiringContext

    if not pairs:
        return {}
    wanted = set(pairs)
    done: Dict[tuple, str] = {}
    try:
        with get_session() as session:
            for start in range(0, len(pairs), 300):
                chunk = pairs[start:start + 300]
                rows = session.exec(
                    select(JobHiringContext.source, JobHiringContext.external_id,
                           JobHiringContext.content_hash)
                    .where(JobHiringContext.source.in_([k[0] for k in chunk]),
                           JobHiringContext.external_id.in_([k[1] for k in chunk]),
                           JobHiringContext.extractor_version >= EXTRACTOR_VERSION)
                ).all()
                for src, ext, chash in rows:
                    if (src, ext) in wanted:
                        done[(src, ext)] = chash or _NO_HASH
    except Exception as e:
        # On failure do the work rather than skip it: correctness over cost.
        log.debug("captured_state lookup failed: %s", e)
        return {}
    return done


def _adopt_baseline_hashes(hashes: Dict[tuple, str]) -> int:
    """Stamp the current description hash onto rows that predate the column.

    Those rows were extracted from SOME description and we have no reason to
    think they are wrong, so re-running the extractor over the entire captured
    corpus to learn a hash would re-create the exact regression this module was
    fixed for — all at once, on the first tick after deploy. Recording the
    current text as the baseline instead costs one narrow UPDATE per posting,
    once, and makes every one of those rows change-detectable from then on.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobHiringContext

    if not hashes:
        return 0
    stamped = 0
    try:
        with get_session() as session:
            # Ascending PK within one transaction — the lock order every
            # multi-row write in this codebase takes (CLAUDE.md, the
            # companyregistry deadlock post-mortem).
            rows = session.exec(
                select(JobHiringContext)
                .where(JobHiringContext.source.in_([k[0] for k in hashes]),
                       JobHiringContext.external_id.in_([k[1] for k in hashes]),
                       JobHiringContext.content_hash.is_(None))
                .order_by(JobHiringContext.id)
            ).all()
            for row in rows:
                h = hashes.get((row.source, row.external_id))
                if not h:
                    continue
                row.content_hash = h
                session.add(row)
                stamped += 1
            if stamped:
                session.commit()
    except Exception as e:
        log.debug("baseline hash stamp failed: %s", e)
        return 0
    return stamped


def capture(raw_jobs: List[RawJob]) -> tuple:
    """Extract and persist context for postings we have not done yet.

    Three answers, not two: never seen (extract), seen with the SAME
    description (skip), seen with a DIFFERENT description (re-extract and
    merge — a posting can gain a team, a req id or a recruiter line days after
    it goes up, and its first sighting would otherwise be its only one).

    Returns (rows_written, text_enriched, skipped_already_done).
    """
    if not raw_jobs:
        return 0, 0, 0
    pairs = sorted({(r.source, r.external_id) for r in raw_jobs if r.external_id})
    state = captured_state(pairs)

    todo: List[RawJob] = []
    baselines: Dict[tuple, str] = {}
    seen_todo: set = set()
    for r in raw_jobs:
        key = (r.source, r.external_id)
        if not r.external_id or key not in state:
            todo.append(r)
            seen_todo.add(key)
            continue
        if key in seen_todo:            # a changed posting seen twice in one batch
            todo.append(r)
            continue
        stored = state[key]
        current = description_hash(r.description)
        if stored == _NO_HASH:
            baselines[key] = current
            continue
        if stored != current:
            todo.append(r)
            seen_todo.add(key)
    if baselines:
        _adopt_baseline_hashes(baselines)
    if not todo:
        return 0, 0, len(raw_jobs)
    enriched = apply_text_extraction(todo)
    written = record_context(todo)
    return written, enriched, len(raw_jobs) - len(todo)


def record_context(raw_jobs: Iterable[RawJob]) -> int:
    """Upsert one context row per distinct (source, external_id).

    Returns the number of rows created or updated. A posting that yields
    NOTHING still gets a row — `examined_only`, carrying only the description
    hash and the extractor version. That looked like clutter and is the
    opposite: roughly a third of postings expose no context at all, and without
    a record of having looked, those are the ones the regex battery re-reads on
    every single tick, forever. The row has no values and no evidence, so
    nothing about it can ever render as fact.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import JobHiringContext

    # Collapse duplicates inside the batch first: the same posting can arrive
    # twice in one discovery pass, and one round trip is enough for both.
    wanted: Dict[tuple, Dict[str, ContextField]] = {}
    urls: Dict[tuple, str] = {}
    hashes: Dict[tuple, str] = {}
    for r in raw_jobs:
        if not r.external_id:
            continue
        key = (r.source, r.external_id)
        merged = wanted.setdefault(key, {})
        # Last description wins for the hash, and it is the one the merged
        # context above was extracted from in this same pass.
        hashes[key] = description_hash(r.description)
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
                    row = JobHiringContext(
                        source=key[0], external_id=key[1],
                        source_url=urls.get(key), observed_at=now, updated_at=now,
                        extractor_version=EXTRACTOR_VERSION,
                        content_hash=hashes.get(key),
                        examined_only=not values,
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
                new_hash = hashes.get(key)
                stamp = (new_hash and row.content_hash != new_hash) or \
                        row.extractor_version != EXTRACTOR_VERSION
                if not changed and not stamp:
                    continue
                if changed:
                    for k, v in values.items():
                        if v:
                            setattr(row, k, v)
                    row.evidence_json = json.dumps(evidence)
                    row.updated_at = now
                    if values:
                        row.examined_only = False
                # Advance the hash even when the re-read found nothing new.
                # Otherwise a posting whose description changed without adding a
                # field would fail the skip check on every subsequent tick and
                # be re-extracted forever — the same unbounded re-work the
                # capture-once fix removed, just triggered differently.
                row.content_hash = new_hash or row.content_hash
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
                values = {k: getattr(row, k, None) for k in CONTEXT_FIELDS}
                if not any(values.values()):
                    # An `examined_only` row: we looked and there was nothing.
                    # Returning it would hand callers an entry where they used
                    # to get none, and an empty section is worse than no
                    # section. The row exists to stop re-extraction, not to be
                    # displayed.
                    continue
                try:
                    evidence = json.loads(row.evidence_json or "{}")
                except (json.JSONDecodeError, TypeError):
                    evidence = {}
                out[(row.source, row.external_id)] = {
                    **values,
                    "evidence": evidence,
                    "source_url": row.source_url,
                    "observed_at": row.observed_at,
                }
    return out
