"""Backfill (and re-stamp) the columns the board reads instead of the posting.

    python -m scripts.backfill_row_facets              # report only — start here
    python -m scripts.backfill_row_facets --yes        # stamp never-stamped rows
    python -m scripts.backfill_row_facets --yes --restamp   # re-assess EVERY row
    python -m scripts.backfill_row_facets --yes --user <uuid> --seconds 1800

`Job.on_role`, `Job.salary_text` and `Job.sponsorship_json` are stamped at write
time for new rows and backfilled at boot for old ones. A pool of ~170k rows takes
a while, and until a row is stamped its card shows no pay or visa signal and its
role filter falls back to the slow ILIKE path — so this exists to finish the job
now rather than over the next N restarts.

## --restamp: run this after uploading sponsor data

The facet is otherwise WRITE-ONCE. `_upsert` stamps it on INSERT and the
default backfill only fills rows where `sponsorship_json IS NULL`, so an
ingested USCIS/register dataset never reaches a job that was already stamped —
only jobs discovered afterwards pick it up. If you uploaded sponsor data and
the badges did not change, this is why, and `--restamp` is the fix.

The report below prints the sponsor registry alongside the card verdicts, so a
dry run answers both halves of the question: is the DATA loaded, and do the
CARDS reflect it?

Safe to re-run: idempotent, works in small chunks with a pause between them,
and stops on the first error.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import func
from sqlmodel import select

from app.db.init_db import get_session, init_db
from app.db.models import Job, UserProfile


def _users(explicit: str | None) -> list[tuple[str | None, list[str]]]:
    if explicit:
        with get_session() as s:
            p = s.exec(select(UserProfile).where(UserProfile.user_id == explicit)).first()
        roles = [r.strip() for r in ((p.target_roles if p else "") or "").split(",") if r.strip()]
        return [(explicit, roles)]
    out = []
    with get_session() as s:
        for p in s.exec(select(UserProfile)).all():
            roles = [r.strip() for r in (p.target_roles or "").split(",") if r.strip()]
            out.append((p.user_id, roles))
    return out


def _report_registry() -> None:
    """Is any sponsor data actually loaded, and do the cards reflect it?

    Two independent facts, printed together because confusing them is the whole
    problem: rows in `h1b_sponsor` mean the UPLOAD worked; the badge/source
    distribution means the CARDS were re-assessed since. A loaded registry next
    to a wall of source=none/curated means you need --restamp.
    """
    import json
    from collections import Counter
    from app.db.models import H1BSponsor

    with get_session() as s:
        rows = s.exec(select(H1BSponsor.country, func.count(H1BSponsor.id),
                             func.max(H1BSponsor.fiscal_year))
                      .group_by(H1BSponsor.country)).all()
    print("SPONSOR REGISTRY (h1b_sponsor)")
    if not rows:
        print("  EMPTY — no dataset ingested. Load one with:")
        print("    python -m app.intelligence.h1b_data <uscis_datahubexport.csv>")
        print('    python -m app.intelligence.h1b_data <uk_register.csv> "united kingdom"')
    for country, n, year in rows:
        print(f"  {(country or 'united states'):<22} {int(n):>7} employers"
              f"{f'   latest FY{int(year)}' if year else ''}")

    with get_session() as s:
        stamped = s.exec(select(Job.sponsorship_json)
                         .where(Job.sponsorship_json.is_not(None))
                         .limit(20000)).all()
    sources, badges = Counter(), Counter()
    for raw in stamped:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        # Rows stamped before provenance shipped carry no "source" key at all —
        # that itself is the signal that they predate the current assessment.
        sources[d.get("source") or "(pre-provenance)"] += 1
        badges[d.get("badge") or "(none)"] += 1
    print(f"\nCARD VERDICTS (sample of {len(stamped)} stamped rows)")
    for src, n in sources.most_common():
        print(f"  source={src:<18} {n:>7}")
    for badge, n in badges.most_common(8):
        print(f"  badge={badge:<19} {n:>7}")
    if rows and (sources.get("(pre-provenance)") or sources.get("none")):
        print("\n  ⚠ A registry IS loaded but cards still show pre-registry verdicts.")
        print("    Re-run with:  --yes --restamp")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=None, help="one user_id (default: every profile)")
    ap.add_argument("--seconds", type=float, default=900.0, help="wall-clock budget per user")
    ap.add_argument("--chunk", type=int, default=500)
    ap.add_argument("--pause", type=float, default=0.15, help="seconds between chunks")
    ap.add_argument("--yes", action="store_true", help="write (default: report only)")
    ap.add_argument("--restamp", action="store_true",
                    help="re-assess EVERY row, not just never-stamped ones "
                         "(run this after uploading sponsor data)")
    ap.add_argument("--start-id", type=int, default=0, dest="start_id",
                    help="resume a --restamp from this job id (printed by the "
                         "previous run when its wall-clock budget ran out)")
    args = ap.parse_args()

    init_db()
    from app.strategy import job_facets, on_role

    _report_registry()

    targets = _users(args.user)
    print(f"{len(targets)} user(s)\n")
    total = 0
    for uid, roles in targets:
        with get_session() as s:
            jobs = s.exec(select(func.count(Job.id)).where(Job.user_id == uid)).first()
            missing_role = s.exec(select(func.count(Job.id)).where(
                Job.user_id == uid, Job.on_role.is_(None))).first()
            missing_facets = s.exec(select(func.count(Job.id)).where(
                Job.user_id == uid, Job.sponsorship_json.is_(None))).first()
        _n = lambda v: (v[0] if isinstance(v, tuple) else v) or 0  # noqa: E731
        print(f"  {uid}: {_n(jobs):>7} jobs | on_role missing {_n(missing_role):>7} | "
              f"facets missing {_n(missing_facets):>7} | roles={roles or '—'}")
        if not args.yes:
            continue
        if roles:
            done = on_role.backfill(uid, roles)
            print(f"    on_role stamped: {done}")
            total += done
        if not args.restamp:
            done = job_facets.backfill(uid, max_seconds=args.seconds,
                                       chunk=args.chunk, pause=args.pause)
            print(f"    facets stamped:  {done}")
            total += done

    # The re-stamp is GLOBAL and pages on the primary key — it is not a per-user
    # loop. See job_facets.restamp_facets: the per-user `ORDER BY id` version had
    # no index to serve it and hit Supabase's statement timeout on chunk one.
    if args.yes and args.restamp:
        done, nxt = job_facets.restamp_facets(
            start_id=args.start_id, max_seconds=args.seconds,
            chunk=args.chunk, pause=args.pause)
        total += done
        print(f"\nRe-stamped {done} row(s).")
        if nxt:
            print(f"Budget spent before the end of the table. Continue with:\n"
                  f"  python -m scripts.backfill_row_facets --yes --restamp "
                  f"--start-id {nxt}")
        else:
            print("Reached the end of the table — re-stamp complete.")
        return 0

    if not args.yes:
        print("\nDry run. Re-run with --yes to write"
              " (add --restamp after loading sponsor data).")
    else:
        print(f"\nStamped {total} row(s). Re-run until 'missing' reaches 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
