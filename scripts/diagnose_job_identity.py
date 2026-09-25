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

def report_collisions(session, limit: int) -> list[tuple]:
    """Any source where one external_id maps to disagreeing employers.

    Deliberately generic rather than Workday-only: `TENANT_SCOPED_SOURCES` is an
    allowlist built from evidence, and this is how a source that is not on it
    yet gets caught — with its own rows as the evidence, instead of a guess.
    """
    rows = session.exec(
        select(Job.source, Job.external_id,
               func.count(func.distinct(Job.company)).label("companies"),
               func.count(Job.id).label("rows"))
        .where(Job.external_id.is_not(None), Job.external_id != "")
        .group_by(Job.source, Job.external_id)
        .having(func.count(func.distinct(Job.company)) > 1)
        .limit(limit)
    ).all()
    return list(rows)


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
    ap.add_argument("--limit", type=int, default=5000,
                    help="max rows to consider (default 5000)")
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
        unscoped = report_unscoped(s)
        repairable, unresolvable = plan(s, args.limit)

    print("== employers sharing one external_id ==")
    if not collisions:
        print("  none found")
    for src, ext, companies, rows in collisions:
        print(f"  {src:<12} {ext:<28} {companies} employers across {rows} rows")

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
