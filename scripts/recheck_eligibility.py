#!/usr/bin/env python3
"""Re-decide location eligibility for recommendations already on a board.

READ-ONLY BY DEFAULT. With no flags it writes nothing: it reports how many
untouched shortlist entries would change verdict under the CURRENT rules,
evidence and profile preferences. `--apply` writes the new verdict onto the
job row, in bounded batches.

    python -m scripts.recheck_eligibility                    # report only
    python -m scripts.recheck_eligibility --apply --limit 500
    python -m scripts.recheck_eligibility --user <user_id>

WHY (audit 2026-09-25, finding 1). The verdict is STAMPED on each Job copy when
it is admitted. New rules (eligibility.RULES_VERSION), new evidence (a
re-derived JobGeography) and a changed profile do not rewrite old stamps, so
the audit found 92 of 182 shortlisted rows that the current rules would hold.
`slate.place()` now re-decides at delivery; this script covers what was
delivered before that.

WHAT IT DOES AND DOES NOT DO.

  * ONLY untouched entries: SHORTLISTED and never viewed. Anything opened,
    tailored, applied to or dismissed is left exactly as it is.
  * Writes ONLY `Job.eligibility` / `Job.eligibility_reason`. It never deletes
    an application, never changes its status, never re-scores. The board shows
    a "Location: check" marker with the reason, and the user decides.
  * Skips any copy without a geography row (pre-rollout postings keep the
    string gate that admitted them) or whose profile cannot be read.
  * Bounded: ascending job id, `--limit` rows, one commit per `--batch`.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from sqlmodel import select

from app.db.init_db import get_session, init_db
from app.db.models import Application, ApplicationStatus, Job


def _candidates(user: str | None, limit: int) -> list[tuple]:
    with get_session() as s:
        q = (select(Job.id, Job.user_id, Job.source, Job.external_id, Job.eligibility)
             .join(Application, Application.job_id == Job.id)
             .where(Application.status == ApplicationStatus.SHORTLISTED,
                    Application.viewed_at.is_(None))
             .order_by(Job.id).limit(limit))
        if user:
            q = q.where(Job.user_id == user)
        return list(s.exec(q).all())


def run(*, apply: bool, limit: int, batch: int, user: str | None) -> Counter:
    from app.discovery.geo_verify import current_decision
    stats: Counter = Counter()
    rows = _candidates(user, limit)
    stats["examined"] = len(rows)
    for start in range(0, len(rows), max(1, batch)):
        chunk = rows[start:start + batch]
        with get_session() as s:
            for jid, uid, source, ext, stamped in chunk:
                d, meta = current_decision(s, source, ext, uid)
                if d is None:
                    stats["skipped:" + ("no_geography" if meta.get("geography") == "none"
                                        else "prefs_unreadable")] += 1
                    continue
                was = stamped or "unstamped"
                if d.status == stamped:
                    stats[f"unchanged:{d.status}"] += 1
                    continue
                stats[f"{was}->{d.status}"] += 1
                stats[f"code:{d.code}"] += 1
                if apply:
                    job = s.get(Job, jid)
                    if job is not None:
                        job.eligibility = d.status
                        job.eligibility_reason = d.reason[:200]
                        s.add(job)
            if apply:
                s.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the new verdicts")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--user", default=None, help="only this user's board")
    args = ap.parse_args(argv)
    init_db()
    stats = run(apply=args.apply, limit=args.limit, batch=args.batch, user=args.user)
    mode = "APPLIED" if args.apply else "DRY RUN — nothing written"
    print(f"recheck_eligibility ({mode})")
    for k in sorted(stats):
        print(f"  {k:40s} {stats[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
