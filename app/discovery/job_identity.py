"""One posting identity, scoped so two employers can never become one job.

THE DEFECT THIS EXISTS FOR (production, 2026-09-25). A Workday requisition id
is unique inside ONE TENANT, not across Workday. Two employers both had
requisition `R29845`, and because every posting-keyed table is keyed
``(source, external_id)`` they merged:

    CrowdStrike  "Sr. Full Stack Engineer, Cloud Native - AIDR"  Sunnyvale CA
    GN           "Aud Fitting Support Advisor"                   Shakopee MN

The CrowdStrike job's `JobHiringContext` row carried GN's `source_url`, and its
`Job.first_seen` was one second after GN's sighting — four days before
CrowdStrike's posting was actually discovered. `first_seen` is what the 5-day
scoring window and the "be first to apply" promise are built on, so the
collision did not merely mislabel a row: it aged a fresh posting.

WHICH SOURCES NEED THIS. Only the ones whose native posting id is namespaced
per employer board. An id minted from a platform-wide sequence or a UUID is
already unique and must NOT be rewritten — that would churn identity for no
gain and break existing deduplication:

    scoped      workday    tenant + jobReqId   (`R118682`, `JR0309198`)
                bamboohr   subdomain + numeric id
                teamtailor subdomain + numeric id
    not scoped  ashby, lever            UUIDs
                greenhouse              platform-wide job ids
                workable                globally unique shortcodes
                extractor-derived       already hashed from the URL

`TENANT_SCOPED_SOURCES` is deliberately a small allowlist rather than "all
sources": adding one is a data migration (see
`scripts/diagnose_job_identity.py`), so it is opt-in per source with evidence.

THE FORM. ``<tenant>:<raw id>``, tenant lowercased. `external_id` is an OPAQUE
key everywhere in the codebase — every consumer either compares it or passes it
in an `IN (...)` list, and nothing reconstructs a URL from it — so widening it
is safe. Where the raw requisition still matters (display, an ATS join) use
`raw_requisition()`; the split is on the FIRST separator only, so a raw id
containing a colon round-trips unchanged.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# One character, and one that no ATS uses inside an id we have seen. Changing
# it is a migration, not an edit.
SCOPE_SEP = ":"

TENANT_SCOPED_SOURCES = frozenset({"workday", "bamboohr", "teamtailor"})

# Host labels that are never the employer: a posting on `careers.acme.com`
# belongs to `acme`, not to `careers`.
_GENERIC_HOST_LABELS = frozenset({
    "www", "careers", "career", "jobs", "job", "apply", "hire", "hiring",
    "recruiting", "recruit", "talent", "work", "join", "boards", "my",
})


def _norm_tenant(tenant: Optional[str]) -> str:
    """Lowercased, trimmed, separator-free. A tenant that would itself contain
    the separator is not allowed to break the round-trip."""
    t = (tenant or "").strip().lower()
    return t.replace(SCOPE_SEP, "-")


def is_tenant_scoped(source: Optional[str]) -> bool:
    """Does this source's native id need an employer qualifier?"""
    return (source or "").strip().lower() in TENANT_SCOPED_SOURCES


def scoped_external_id(source: Optional[str], tenant: Optional[str],
                       raw_id) -> str:
    """The stored `external_id` for one posting.

    Falls back to the bare raw id when the source is not tenant-scoped, or when
    no tenant is known — a missing tenant must not silently produce the string
    ``":R29845"``, which would collide exactly as before while looking fixed.
    """
    raw = str(raw_id or "").strip()
    if not raw:
        return ""
    t = _norm_tenant(tenant)
    if not is_tenant_scoped(source) or not t:
        return raw
    if raw.startswith(f"{t}{SCOPE_SEP}"):
        return raw                      # already scoped; idempotent
    return f"{t}{SCOPE_SEP}{raw}"


def parse_scoped(external_id: Optional[str]) -> tuple[Optional[str], str]:
    """``(tenant, raw id)``. Tenant is None when the id carries no scope."""
    s = (external_id or "").strip()
    if SCOPE_SEP not in s:
        return None, s
    tenant, _, raw = s.partition(SCOPE_SEP)
    if not tenant or not raw:
        return None, s                  # malformed; treat the whole as raw
    return tenant, raw


def raw_requisition(external_id: Optional[str]) -> str:
    """The employer-facing requisition id, for display and ATS joins."""
    return parse_scoped(external_id)[1]


def tenant_from_url(source: Optional[str], url: Optional[str]) -> str:
    """The employer tenant implied by a posting URL.

    This is what makes the repair of existing rows deterministic and
    REVERSIBLE: every affected row already stores the URL its tenant came from,
    so the backfill derives the qualifier from data rather than guessing, and
    stripping the prefix restores the original value exactly.
    """
    if not is_tenant_scoped(source):
        return ""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return ""
    labels = [p for p in host.split(".") if p]
    if not labels:
        return ""
    # Walk past generic labels (`careers.acme.com`), but never past the
    # registrable part — a two-label host is the employer's own domain.
    for i, label in enumerate(labels):
        if label in _GENERIC_HOST_LABELS and i + 2 < len(labels):
            continue
        return _norm_tenant(label)
    return _norm_tenant(labels[0])


def looks_unscoped(source: Optional[str], external_id: Optional[str]) -> bool:
    """True when a tenant-scoped source stored a bare id — i.e. a row written
    before this module, and a candidate for the backfill."""
    if not is_tenant_scoped(source):
        return False
    tenant, _raw = parse_scoped(external_id)
    return tenant is None
