#!/usr/bin/env python3
"""Find and repair postings that two employers share.

READ-ONLY BY DEFAULT. Run it with no flags and it writes nothing: it reports
collisions, counts rows that predate tenant-scoped identity, and shows what a
repair would do. `--apply` performs the repair in bounded batches.

    python -m scripts.diagnose_job_identity                 # report only
    python -m scripts.diagnose_job_identity --apply         # repair
    python -m scripts.diagnose_job_identity --apply --limit 500

WHY. A Workday requisition id is unique inside one TENANT. Production had two
employers on `R29845` (CrowdStrike and GN), and because every posting-keyed
table is keyed `(source, external_id)` they merged — including `first_seen`,
which made a posting discovered on the 25th look four days old. See
`app/discovery/job_identity.py`.

WHAT THE REPAIR DOES AND DOES NOT DO. It rewrites `external_id` in place from
`R29845` to `crowdstrike:R29845`, deriving the tenant from the URL the row
already stores. That is:

  * REVERSIBLE — strip the prefix and the original value returns exactly.
  * NON-DESTRUCTIVE — no row is deleted. `Application` rows reference `job.id`,
    not `external_id`, so applications, notes, status and history are untouched.
  * BOUNDED — batched, ascending id, capped by `--limit`.
  * SKIPPED, NEVER FORCED — if two rows would land on the same new id (a
    genuine same-employer duplicate) the collision is reported and skipped
    rather than violating the unique constraint.

It does NOT re-score, re-embed, reset a board, or touch a user's shortlist.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from typing import Iterable
from urllib.parse import urlparse

from sqlalchemy import String as _SQLString
from sqlalchemy import func
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import (Job, JobGeography, JobHiringContext, JobLiveness)
from app.discovery.job_identity import (SCOPE_SEP, TENANT_SCOPED_SOURCES,
                                        looks_unscoped, scoped_external_id,
                                        tenant_from_url)

# Rewriting the three posting-keyed side tables keeps their evidence attached to
# the employer it was actually observed from. Each has a `source_url` (liveness
# calls it `checked_url`) that says which employer a merged row belongs to.
_SIDE_TABLES = (
    (JobHiringContext, "source_url"),
    (JobGeography, "source_url"),
    (JobLiveness, "checked_url"),
)


def _host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower()


# ── report ───────────────────────────────────────────────────────────────────

def report_collisions(session, limit: int) -> tuple[list, list]:
    """(real collisions, spelling-only groups).

    A REAL collision is one external_id under two different EMPLOYER TENANTS,
    derived from the posting URL. The first version of this counted
    `DISTINCT company` strings instead, which is not the same question: a
    company stored once as "CrowdStrike" and once as "Crowdstrike" counted as
    two employers and was reported as a collision. Against production that
    produced false positives on greenhouse, lever and ashby — sources whose ids
    ARE globally unique — which would have argued for putting them on the
    tenant-scoping allowlist they do not belong on.

    Both groups are returned because the difference is the useful bit: the
    second is a display-name inconsistency, not an identity defect.
    """
    sub = (select(Job.source, Job.external_id)
           .where(Job.external_id.is_not(None), Job.external_id != "")
           .group_by(Job.source, Job.external_id)
           .having(func.count(func.distinct(Job.company)) > 1)
           .subquery())
    rows = session.exec(
        select(Job.source, Job.external_id, Job.company, Job.url)
        .join(sub, (Job.source == sub.c.source)
              & (Job.external_id == sub.c.external_id))
    ).all()

    groups: dict[tuple, dict] = {}
    for source, ext, company, url in rows:
        src = str(getattr(source, "value", source)).lower()
        g = groups.setdefault((src, ext), {"tenants": {}, "companies": set(), "rows": 0})
        g["rows"] += 1
        g["companies"].add(company or "")
        t = tenant_from_url(src, url) or _host(url) or ""
        if t:
            g["tenants"].setdefault(t, set()).add(company or "")

    real, spelling = [], []
    for (src, ext), g in groups.items():
        entry = (src, ext, len(g["tenants"]), g["rows"],
                 sorted(g["companies"])[:6], sorted(g["tenants"])[:6])
        (real if len(g["tenants"]) > 1 else spelling).append(entry)
    real.sort(key=lambda e: (-e[2], -e[3]))
    spelling.sort(key=lambda e: -e[3])
    return real[:limit], spelling[:limit]


def report_tenant_mapping(session, limit: int = 40) -> dict[str, list]:
    """What each source's URLs actually derive as a tenant.

    The repair keys off `tenant_from_url`, and "the first label of the host is
    non-empty" is not evidence it is the EMPLOYER — a board on its own domain
    (`careers.acme.com`) or a shortener would derive something else. This prints
    the mapping so it can be eyeballed per source BEFORE `--apply` touches
    anything, which is the check teamtailor in particular needs.
    """
    out: dict[str, list] = {}
    for src in sorted(TENANT_SCOPED_SOURCES):
        rows = session.exec(
            select(Job.url, Job.company, func.count(Job.id).label("n"))
            .where(func.lower(Job.source.cast(_SQLString)) == src,
                   Job.url.is_not(None), Job.url != "")
            .group_by(Job.url, Job.company)
            .limit(4000)
        ).all()
        seen: dict[str, dict] = {}
        for url, company, n in rows:
            t = tenant_from_url(src, url)
            e = seen.setdefault(t or "(none)",
                                {"rows": 0, "companies": set(), "host": _host(url)})
            e["rows"] += int(n or 0)
            e["companies"].add(company or "")
        out[src] = sorted(
            ((t, e["rows"], sorted(e["companies"])[:3], e["host"])
             for t, e in seen.items()),
            key=lambda x: -x[1])[:limit]
    return out


def report_unscoped(session) -> dict[str, int]:
    """How many rows of each tenant-scoped source predate the qualifier."""
    out: dict[str, int] = {}
    for src in sorted(TENANT_SCOPED_SOURCES):
        n = session.exec(
            select(func.count(Job.id)).where(
                Job.source == src,
                Job.external_id.is_not(None), Job.external_id != "",
                Job.external_id.notlike(f"%{SCOPE_SEP}%"))
        ).one()
        out[src] = int(n[0] if isinstance(n, (list, tuple)) else n)
    return out


def plan(session, limit: int) -> tuple[list, list]:
    """(repairable, unresolvable) — rows whose tenant the URL does or does not give."""
    rows = session.exec(
        select(Job.id, Job.user_id, Job.source, Job.external_id, Job.url,
               Job.company)
        .where(Job.source.in_(sorted(TENANT_SCOPED_SOURCES)),
               Job.external_id.is_not(None), Job.external_id != "",
               Job.external_id.notlike(f"%{SCOPE_SEP}%"))
        .order_by(Job.id)
        .limit(limit)
    ).all()
    repairable, unresolvable = [], []
    for r in rows:
        jid_, uid, src, ext, url, company = r
        if not looks_unscoped(src, ext):
            continue
        tenant = tenant_from_url(src, url)
        if not tenant:
            unresolvable.append((jid_, src, ext, url, company))
            continue
        repairable.append((jid_, uid, src, ext, scoped_external_id(src, tenant, ext),
                           tenant, url, company))
    return repairable, unresolvable


# ── repair ───────────────────────────────────────────────────────────────────

def _taken(session, uid, source, new_ext) -> bool:
    """Would this rewrite hit uq_job_user_source_external_id?"""
    hit = session.exec(
        select(Job.id).where(Job.user_id == uid, Job.source == source,
                             Job.external_id == new_ext).limit(1)).first()
    return hit is not None


def apply_repair(repairable: Iterable[tuple], batch: int = 200) -> dict:
    """Rewrite external_id in place, ascending id, in bounded batches.

    Ascending id and one transaction per batch follow the repository's
    lock-order convention (`test_registry_lock_order` exists because
    piecewise-sorted multi-row writes deadlocked production).
    """
    stats = {"jobs": 0, "skipped_would_collide": 0, "side_rows": 0}
    items = sorted(repairable, key=lambda r: r[0])
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        with get_session() as s:
            for jid_, uid, src, old_ext, new_ext, tenant, url, _company in chunk:
                job = s.get(Job, jid_)
                if job is None or job.external_id != old_ext:
                    continue                      # moved under us; leave it
                if _taken(s, uid, src, new_ext):
                    stats["skipped_would_collide"] += 1
                    continue
                job.external_id = new_ext
                s.add(job)
                stats["jobs"] += 1
            s.commit()

    # Side tables are keyed by (source, external_id) with no user scope, so they
    # are rewritten once per distinct posting, assigned to the employer their
    # OWN recorded url names — that is how a merged row is handed back to the
    # employer it was really observed from.
    by_key: dict[tuple, set[str]] = defaultdict(set)
    for _jid, _uid, src, old_ext, _new, tenant, _url, _c in items:
        by_key[(src, old_ext)].add(tenant)
    with get_session() as s:
        for model, url_field in _SIDE_TABLES:
            for (src, old_ext), tenants in sorted(by_key.items()):
                rows = s.exec(select(model).where(
                    model.source == src, model.external_id == old_ext)).all()
                for row in rows:
                    tenant = tenant_from_url(src, getattr(row, url_field, "")) \
                        or (next(iter(tenants)) if len(tenants) == 1 else "")
                    if not tenant:
                        continue          # ambiguous: leave for a human
                    new_ext = scoped_external_id(src, tenant, old_ext)
                    exists = s.exec(select(model.id).where(
                        model.source == src,
                        model.external_id == new_ext).limit(1)).first()
                    if exists is not None:
                        continue
                    row.external_id = new_ext
                    s.add(row)
                    stats["side_rows"] += 1
        s.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="perform the repair (default: report only)")
    ap.add_argument("--limit", type=int, default=50000,
                    help="max rows to consider per run (default 50000). "
                         "Production holds ~487k unscoped rows, so --apply is "
                         "meant to be run repeatedly until `repairable` is 0.")
    args = ap.parse_args()

    # SQLite-only, and deliberately so. `init_db()` is NOT read-only — it runs
    # `ensure_model_columns()`, `ALTER TYPE ... ADD VALUE` and index creation —
    # so calling it against production would make a tool documented as
    # read-only mutate the schema. A hosted database already has every table
    # this reads; a fresh local checkout does not, and that is the only case
    # that needs it.
    if not settings.use_supabase:
        init_db()

    with get_session() as s:
        collisions = report_collisions(s, args.limit)
        tenant_map = report_tenant_mapping(s)
        unscoped = report_unscoped(s)
        repairable, unresolvable = plan(s, args.limit)

    real, spelling = collisions
    print("== REAL collisions: one external_id, two employer tenants ==")
    if not real:
        print("  none found")
    for src, ext, tenants, rows, companies, tnames in real[:25]:
        print(f"  {src:<12} {ext:<24} {tenants} tenants / {rows} rows  "
              f"{', '.join(tnames)}")
        print(f"  {'':<12} {'':<24} companies: {', '.join(companies)}")
    if len(real) > 25:
        print(f"  … {len(real) - 25} more")

    print("\n== same employer, different spellings (NOT an identity defect) ==")
    if not spelling:
        print("  none found")
    for src, ext, _t, rows, companies, _tn in spelling[:10]:
        print(f"  {src:<12} {ext:<24} {rows} rows  {' | '.join(companies)}")
    if len(spelling) > 10:
        print(f"  … {len(spelling) - 10} more")

    print("\n== derived tenant per source — CHECK THIS BEFORE --apply ==")
    print("   'the first host label is non-empty' is not evidence it is the")
    print("   employer. Confirm these look like employers, per source.")
    for src, entries in tenant_map.items():
        print(f"  {src}:")
        for tenant, rows, companies, host in entries[:12]:
            print(f"    {tenant:<24} {rows:>7} rows  host={host:<34} "
                  f"{', '.join(companies)[:48]}")

    print("\n== rows predating tenant-scoped identity ==")
    for src, n in unscoped.items():
        print(f"  {src:<12} {n}")

    print(f"\n== repair plan (limit {args.limit}) ==")
    print(f"  repairable   {len(repairable)}")
    print(f"  unresolvable {len(unresolvable)}  (no tenant derivable from the URL)")
    for jid_, src, ext, url, company in unresolvable[:10]:
        print(f"    job {jid_} {src} {ext!r} company={company!r} url={_host(url)!r}")
    for row in repairable[:10]:
        jid_, _uid, src, old, new, tenant, _url, company = row
        print(f"    job {jid_} {src} {old!r} -> {new!r}  ({company})")
    if len(repairable) > 10:
        print(f"    … {len(repairable) - 10} more")

    if not args.apply:
        print("\nREAD-ONLY: nothing was written. Re-run with --apply to repair.")
        return 0

    stats = apply_repair(repairable)
    print(f"\n== applied ==\n  {stats}")
    print("  Reversible: strip the '<tenant>:' prefix to restore the original id.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
