"""Stage-latency + lifecycle report — read-only.

Answers the questions the Aug 2026 discovery/expiry investigation could not,
because no timestamps existed for the middle of the funnel:

    first_seen -> adopted           (shared pool -> the user's own pool)
    first_seen -> prescored         (Tier-1 produced a number)
    first_seen -> final scored      (a scorer wrote a terminal verdict)
    first_seen -> shortlisted       (an Application row was created)

and the lifecycle split that ``rerank_score IS NOT NULL`` could never give:

    genuinely scored / prescored-only / expired without scoring / pending

Usage:
    python -m scripts.stage_latency              # last 7 days
    python -m scripts.stage_latency --days 1
    python -m scripts.stage_latency --user <uuid>

STRICTLY READ-ONLY: every statement is a SELECT. Safe against production.

The stage columns (``prescored_at``, ``scored_at``, ``expired_at``) ship
nullable with no backfill, so rows written before the migration report as
"unmeasured" rather than being guessed at — the counts say how many.
"""
from __future__ import annotations

import argparse
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import select

from app.common.freshness import (
    EXPIRY_SENTINEL_SCORE, GHOST_SENTINEL_SCORE,
    expired_without_scoring_expr, terminal_verdict_expr,
)
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, Job
from app.discovery.pipeline import SHARED_POOL_USER


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    - "


def _summary(name: str, hours: list[float]) -> None:
    if not hours:
        print(f"  {name:<26} no measurable rows yet")
        return
    hours.sort()
    p50 = statistics.median(hours)
    p90 = hours[min(len(hours) - 1, int(len(hours) * 0.90))]
    print(f"  {name:<26} n={len(hours):<7,} p50={p50:8.1f}h  p90={p90:8.1f}h  "
          f"max={hours[-1]:8.1f}h")


def _hours(a: datetime | None, b: datetime | None) -> float | None:
    """b - a in hours, ignoring nonsense (missing, or negative by >1 minute)."""
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds() / 3600.0
    return delta if delta >= -0.017 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="only consider jobs first seen within N days (default 7)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--limit", type=int, default=50000,
                    help="max rows to sample (default 50000)")
    args = ap.parse_args()

    now = datetime.utcnow()
    cutoff = now - timedelta(days=args.days)

    print(f"Stage latency — jobs first seen since {cutoff:%Y-%m-%d %H:%M} UTC "
          f"({args.days}d)" + (f", user={args.user}" if args.user else ""))
    print(f"Windows in force: known-age scoring gate "
          f"{settings.scoring_max_job_age_days}d · posted-age gate "
          f"{getattr(settings, 'scoring_max_posted_age_days', '-')}d · "
          f"render {settings.shortlist_max_age_days}d / "
          f"{getattr(settings, 'shortlist_max_posted_age_days', '-')}d")
    print()

    base = [Job.first_seen != None, Job.first_seen >= cutoff]  # noqa: E711
    if args.user:
        base.append(Job.user_id == args.user)
    else:
        base.append((Job.user_id.is_(None)) | (Job.user_id != SHARED_POOL_USER))

    with get_session() as session:
        def cnt(*extra) -> int:
            v = session.exec(
                select(func.count(Job.id)).where(*base, *extra)).one()
            return int(v[0] if isinstance(v, tuple) else v)

        total = cnt()
        scored = cnt(terminal_verdict_expr())
        expired = cnt(expired_without_scoring_expr())
        ghosted = cnt(Job.rerank_score == GHOST_SENTINEL_SCORE)
        pending = cnt(Job.rerank_score.is_(None))
        prescored_only = cnt(Job.prescore.is_not(None),
                             Job.rerank_score.is_(None))

        print(f"LIFECYCLE  (n={total:,} per-user jobs first seen in window)")
        print(f"  terminal verdicts         {scored:>9,}  {_pct(scored, total)}"
              f"   <- ANY tier, incl. Tier-1 drains")
        print(f"  expired without scoring   {expired:>9,}  {_pct(expired, total)}"
              f"   <- rerank_score={EXPIRY_SENTINEL_SCORE} sentinel")
        print(f"  ghost-filtered            {ghosted:>9,}  {_pct(ghosted, total)}")
        print(f"  pending (queued)          {pending:>9,}  {_pct(pending, total)}")
        print(f"    of which prescored      {prescored_only:>9,}  "
              f"{_pct(prescored_only, total)}")
        print()

        # IMMEDIATE-EXPIRY RATE — the headline number of the expiry bug. A job
        # is "immediately expired" when it was ALREADY past the posted-age it
        # got killed for at the moment we first saw it, i.e. the expiry had
        # nothing to do with how long we sat on it.
        exp_rows = session.exec(
            select(Job.first_seen, Job.posted_at, Job.expired_at)
            .where(*base, expired_without_scoring_expr())
            .limit(args.limit)
        ).all()
        if exp_rows:
            known_days = int(settings.scoring_max_job_age_days or 0)
            immediate = 0
            for fs, posted, _exp in exp_rows:
                if fs is None or posted is None:
                    continue
                if (fs - posted) >= timedelta(days=known_days):
                    immediate += 1
            print(f"IMMEDIATE-EXPIRY RATE  (of {len(exp_rows):,} sampled expiries)")
            print(f"  already >={known_days}d old at first discovery: "
                  f"{immediate:,}  {_pct(immediate, len(exp_rows))}")
            print("  (Under the OLD single-bound gate every one of these expired "
                  "the instant it was\n   discovered. Under the split bounds they "
                  "expire only if genuinely ancient or if we\n   held them unscored "
                  f"for {known_days}d — so this should now be near zero.)")
            print()

        # STAGE LATENCIES
        rows = session.exec(
            select(Job.id, Job.first_seen, Job.prescored_at, Job.scored_at,
                   Job.expired_at, Job.rerank_score)
            .where(*base)
            .order_by(Job.first_seen.desc())
            .limit(args.limit)
        ).all()

        pre_h, score_h = [], []
        unmeasured_scored = 0
        job_ids = []
        for jid, fs, pre_at, sc_at, _exp_at, rs in rows:
            job_ids.append(jid)
            h = _hours(fs, pre_at)
            if h is not None:
                pre_h.append(h)
            h = _hours(fs, sc_at)
            if h is not None:
                score_h.append(h)
            elif rs is not None and sc_at is None:
                # Scored before the stage columns shipped.
                unmeasured_scored += 1

        # first_seen -> shortlisted, from Application.created_at (already
        # recorded, so no new column was needed for this stage).
        short_h = []
        for start in range(0, len(job_ids), 500):
            chunk = job_ids[start:start + 500]
            if not chunk:
                continue
            for jid, created in session.exec(
                select(Application.job_id, Application.created_at)
                .where(Application.job_id.in_(chunk))
            ).all():
                fs = next((r[1] for r in rows if r[0] == jid), None)
                h = _hours(fs, created)
                if h is not None:
                    short_h.append(h)

        print(f"STAGE LATENCY  (sampled {len(rows):,} rows)")
        _summary("first_seen -> prescored", pre_h)
        _summary("first_seen -> scored", score_h)
        _summary("first_seen -> shortlisted", short_h)
        if unmeasured_scored:
            print(f"  ({unmeasured_scored:,} scored rows predate the stage columns "
                  "and cannot be measured)")
        print()

        # first_seen -> adopted: the shared-pool row and the per-user copy share
        # (source, external_id), so adoption latency is the gap between their
        # first_seen values. No column needed.
        if not args.user:
            adopt_h = []
            shared = dict(session.exec(
                select(Job.external_id, Job.first_seen)
                .where(Job.user_id == SHARED_POOL_USER,
                       Job.first_seen != None,  # noqa: E711
                       Job.first_seen >= cutoff - timedelta(days=args.days))
                .limit(args.limit)
            ).all())
            if shared:
                for ext, fs in session.exec(
                    select(Job.external_id, Job.first_seen)
                    .where(*base).limit(args.limit)
                ).all():
                    h = _hours(shared.get(ext), fs)
                    if h is not None:
                        adopt_h.append(h)
            print("ADOPTION LATENCY  (shared pool first_seen -> user pool first_seen)")
            _summary("first_seen -> adopted", adopt_h)


if __name__ == "__main__":
    main()
