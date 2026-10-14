"""Apply-latency report — read-only (docs/EARLINESS_RECEIPT.md).

How early do users actually apply, and does early correlate with a reply?

    posting reference -> submitted      (hours; the receipt clock)
    fresh alert       -> submitted      (hours; did the alert turn into an application?)
    reply rate by latency bucket        (first_response_at, printed only above a floor)

Usage:
    python -m scripts.apply_latency_report              # last 90 days
    python -m scripts.apply_latency_report --days 30
    python -m scripts.apply_latency_report --user <uuid>

STRICTLY READ-ONLY: every statement is a SELECT. Safe against production.

Email imports are excluded (their submitted_at is the import time). Rows that
predate the receipt columns are counted as "unmeasured", never guessed at. The
reply split prints nothing under REPLY_FLOOR applications per bucket older
than REPLY_AGE_DAYS: a rate on six rows is noise dressed as a finding.
"""
from __future__ import annotations

import argparse
import statistics
from datetime import datetime, timedelta

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, FunnelEvent, Job
from app.strategy import earliness

SUBMITTED_LIKE = (ApplicationStatus.SUBMITTED, ApplicationStatus.INTERVIEWING, ApplicationStatus.OFFER,
                  ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED)
REPLY_FLOOR = 30
REPLY_AGE_DAYS = 21


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    - "


def _hours(a: datetime | None, b: datetime | None) -> float | None:
    if not a or not b:
        return None
    return (b - a).total_seconds() / 3600.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--user", default=None)
    args = ap.parse_args()
    since = datetime.utcnow() - timedelta(days=args.days)
    window_h = earliness.window_hours()

    with get_session() as s:
        q = (select(Application, Job).join(Job, Job.id == Application.job_id)
             .where(Application.status.in_(SUBMITTED_LIKE), Application.submitted_at.is_not(None),
                    Application.submitted_at >= since, Application.apply_track != "email_import"))
        if args.user:
            q = q.where(Application.user_id == args.user)
        rows = list(s.exec(q).all())

        print(f"Applications submitted through SpotApply, last {args.days} days: {len(rows)}"
              f" ({len({a.user_id for a, _ in rows})} users)")
        measured = [(a, j) for a, j in rows if a.applied_after_min is not None]
        print(f"  with a receipt (applied_after_min): {len(measured)}; unmeasured (pre-column): {len(rows) - len(measured)}")

        # Reference clock for every row (recomputed, so pre-column rows report too)
        hours = []
        by_kind = {"posted": 0, "found": 0, "none": 0}
        for a, j in rows:
            ref, kind = earliness.reference(j)
            by_kind[kind or "none"] += 1
            h = _hours(ref, a.submitted_at)
            if h is not None and h >= 0:
                hours.append(h)
        if hours:
            hours.sort()
            print(f"  reference -> submitted: n={len(hours)} median={statistics.median(hours):.1f}h "
                  f"p90={hours[min(len(hours) - 1, int(len(hours) * 0.9))]:.1f}h")
            print(f"  within 24h: {_pct(sum(h <= 24 for h in hours), len(hours))}   "
                  f"within {window_h}h: {_pct(sum(h <= window_h for h in hours), len(hours))}")
        print(f"  clock: posted={by_kind['posted']} found={by_kind['found']} none={by_kind['none']}")

        # Fresh alert -> submitted
        alerts = list(s.exec(select(FunnelEvent).where(FunnelEvent.stage == "fresh_alert",
                                                       FunnelEvent.created_at >= since)).all())
        alerted_job_ids = {e.job_id for e in alerts}
        alert_at = {}
        for e in alerts:
            alert_at[e.job_id] = min(alert_at.get(e.job_id, e.created_at), e.created_at)
        converted = [(a, j) for a, j in rows if j.id in alerted_job_ids]
        print(f"\nFresh alerts: {len(alerts)} ({len(alerted_job_ids)} postings); submitted after an alert: {len(converted)}"
              f" ({_pct(len(converted), len(alerted_job_ids))} of alerted postings)")
        lat = sorted(h for h in (_hours(alert_at.get(j.id), a.submitted_at) for a, j in converted) if h is not None and h >= 0)
        if lat:
            print(f"  alert -> submitted: median={statistics.median(lat):.1f}h, within 48h: {_pct(sum(h <= 48 for h in lat), len(lat))}")

        # Reply rate by latency bucket — only above the floor
        aged = [(a, j) for a, j in measured if a.submitted_at <= datetime.utcnow() - timedelta(days=REPLY_AGE_DAYS)]
        early = [a for a, _ in aged if a.applied_after_min <= window_h * 60]
        late = [a for a, _ in aged if a.applied_after_min > window_h * 60]
        print(f"\nReply split (applications >= {REPLY_AGE_DAYS}d old, floor {REPLY_FLOOR} per bucket):")
        for name, bucket in (("early", early), ("late", late)):
            if len(bucket) < REPLY_FLOOR:
                print(f"  {name:<6} n={len(bucket):<4} below the floor — not reported")
                continue
            replied = sum(1 for a in bucket if a.first_response_at is not None)
            interviews = sum(1 for a in bucket if a.status in (ApplicationStatus.INTERVIEWING, ApplicationStatus.OFFER, ApplicationStatus.ACCEPTED))
            print(f"  {name:<6} n={len(bucket):<4} any response {_pct(replied, len(bucket))}  interview+ {_pct(interviews, len(bucket))}")


if __name__ == "__main__":
    main()
