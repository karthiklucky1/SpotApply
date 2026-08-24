"""Zero-yield board census — evidence before policy. READ-ONLY.

Production carries roughly 31k active boards with ``job_count == 0`` — about
59% of the working set — and the pulse lane schedules every one of them on the
``pulse_dead_interval_hours`` (24h) retry. That is a large, permanent claim on a
lane that is already capacity-limited, so the question "should they poll less
often, or at all?" is worth real numbers rather than a guess.

This script answers it and CHANGES NOTHING. Every statement is a SELECT.

    python -m scripts.zero_yield_boards
    python -m scripts.zero_yield_boards --json

What it measures, in the order the decision needs it:

  1. SIZE          — exact count, and share of the active working set.
  2. COST          — polling attempts/day these consume, and what share of the
                     lane's real completed-fetch capacity that is.
  3. LIVENESS      — never successfully fetched vs. fetches fine but serves an
                     empty board. Those are different problems: the first is a
                     dead slug, the second is a real company with nothing open.
  4. AGE           — how long they have been in the registry, and how long since
                     anyone last reached them.
  5. SOURCE MIX    — which ATS platforms they concentrate in.
  6. CONVERSION    — of boards first seen 1d / 7d / 30d ago with no jobs then,
                     how many have produced a posting since. This is the number
                     that decides the policy: a board that never converts is
                     paying rent forever.
  7. RECOVERY      — capacity freed at a 48h / 72h / 7d cadence, or by retiring
                     the chronically dead, expressed in boards/day the floor
                     could reclaim.

Nothing here is a recommendation. Read section 6 first: if conversion is
non-trivial, these boards are an investment and the answer is a longer cadence;
if it rounds to zero, they are dead weight and retirement is on the table.
"""
from __future__ import annotations

import argparse
import json as _json
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import CompanyRegistry, Job


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    - "


def collect() -> dict:
    now = datetime.utcnow()
    out: dict = {"as_of": now.isoformat()}

    with get_session() as session:
        def cnt(*where) -> int:
            v = session.exec(
                select(func.count(CompanyRegistry.id)).where(*where)).one()
            return int(v[0] if isinstance(v, tuple) else v)

        active = CompanyRegistry.is_active == True  # noqa: E712
        zero = (CompanyRegistry.job_count == 0) | (CompanyRegistry.job_count.is_(None))

        # ── 1. SIZE ──────────────────────────────────────────────────────────
        out["active_boards"] = cnt(active)
        out["live_boards"] = cnt(active, CompanyRegistry.job_count > 0)
        out["zero_yield"] = cnt(active, zero)
        out["zero_yield_pct"] = (round(100.0 * out["zero_yield"] / out["active_boards"], 1)
                                 if out["active_boards"] else None)

        # ── 2. COST ──────────────────────────────────────────────────────────
        dead_hours = max(1, int(settings.pulse_dead_interval_hours or 24))
        out["dead_interval_hours"] = dead_hours
        out["zero_yield_polls_per_day"] = round(out["zero_yield"] * 24.0 / dead_hours)
        # Real capacity, measured from completed fetches over the last 24h —
        # NOT from ticks x selected (see scripts/verify_discovery_fix.py).
        from app.db.models import FunnelEvent
        ticks = session.exec(
            select(FunnelEvent.metadata_json)
            .where(FunnelEvent.stage == "pulse_tick",
                   FunnelEvent.created_at >= now - timedelta(hours=24))
            .limit(5000)
        ).all()
        completed = 0
        for row in ticks:
            raw = row[0] if isinstance(row, tuple) else row
            try:
                completed += int(_json.loads(raw or "{}").get("fetch_ok") or 0)
            except Exception:
                pass
        out["completed_fetches_24h"] = completed
        out["zero_yield_share_of_capacity_pct"] = (
            round(100.0 * out["zero_yield_polls_per_day"] / completed, 1)
            if completed else None)

        # ── 3. LIVENESS ──────────────────────────────────────────────────────
        out["never_fetched"] = cnt(active, zero, CompanyRegistry.last_seen.is_(None))
        out["fetched_but_empty"] = cnt(active, zero, CompanyRegistry.last_seen.is_not(None))
        out["with_failures"] = cnt(active, zero, CompanyRegistry.failure_count > 0)
        out["with_last_error"] = cnt(active, zero, CompanyRegistry.last_error.is_not(None))

        # ── 4. AGE ───────────────────────────────────────────────────────────
        out["registry_age_days"] = {}
        for label, days in (("<=1d", 1), ("<=7d", 7), ("<=30d", 30), ("<=90d", 90)):
            out["registry_age_days"][label] = cnt(
                active, zero, CompanyRegistry.first_seen >= now - timedelta(days=days))
        out["registry_age_days"][">90d"] = out["zero_yield"] - out["registry_age_days"]["<=90d"]

        out["last_seen_age"] = {
            "never": out["never_fetched"],
            "<=24h": cnt(active, zero, CompanyRegistry.last_seen >= now - timedelta(hours=24)),
            "<=7d": cnt(active, zero, CompanyRegistry.last_seen >= now - timedelta(days=7)),
        }
        out["last_seen_age"][">7d"] = (
            out["fetched_but_empty"] - out["last_seen_age"]["<=7d"])

        # ── 5. SOURCE MIX ────────────────────────────────────────────────────
        rows = session.exec(
            select(CompanyRegistry.ats, func.count(CompanyRegistry.id))
            .where(active, zero).group_by(CompanyRegistry.ats)
        ).all()
        mix = {}
        for ats, n in rows:
            mix[getattr(ats, "value", str(ats))] = int(n)
        out["by_ats"] = dict(sorted(mix.items(), key=lambda kv: -kv[1]))

        srows = session.exec(
            select(CompanyRegistry.source, func.count(CompanyRegistry.id))
            .where(active, zero).group_by(CompanyRegistry.source)
        ).all()
        out["by_source"] = dict(sorted(
            ((str(s), int(n)) for s, n in srows), key=lambda kv: -kv[1]))

        # ── 6. CONVERSION ────────────────────────────────────────────────────
        # Of boards registered N days ago, how many have EVER produced a posting
        # (last_new_job_at set)? That is the honest "was the retry worth it"
        # number — a cohort measure, not a snapshot.
        out["conversion"] = {}
        for label, days in (("1d", 1), ("7d", 7), ("30d", 30)):
            lo = now - timedelta(days=days)
            hi = now - timedelta(days=days - 1) if days > 1 else now
            cohort = cnt(active, CompanyRegistry.first_seen >= lo,
                         CompanyRegistry.first_seen < hi)
            produced = cnt(active, CompanyRegistry.first_seen >= lo,
                           CompanyRegistry.first_seen < hi,
                           CompanyRegistry.last_new_job_at.is_not(None))
            out["conversion"][label] = {
                "cohort": cohort, "ever_produced": produced,
                "pct": round(100.0 * produced / cohort, 1) if cohort else None,
            }
        # Chronically dead: in the registry over 30 days, fetched at least once,
        # and still never produced anything.
        out["chronically_dead"] = cnt(
            active, zero,
            CompanyRegistry.first_seen < now - timedelta(days=30),
            CompanyRegistry.last_seen.is_not(None),
            CompanyRegistry.last_new_job_at.is_(None))

        # ── 7. RECOVERY ──────────────────────────────────────────────────────
        # Polls/day freed by lengthening the dead cadence, or by retiring the
        # chronically dead outright.
        base = out["zero_yield_polls_per_day"]
        out["recovery_polls_per_day"] = {}
        for label, hours in (("48h", 48), ("72h", 72), ("7d", 168)):
            out["recovery_polls_per_day"][label] = round(
                base - out["zero_yield"] * 24.0 / hours)
        out["recovery_polls_per_day"]["retire_chronically_dead"] = round(
            out["chronically_dead"] * 24.0 / dead_hours)

        # Sanity cross-check: do zero-yield boards actually hold zero jobs? The
        # registry counter could be stale, which would make this whole census
        # wrong. Sample the Job table for a handful of them.
        sample_slugs = [
            (r[0] if isinstance(r, tuple) else r) for r in session.exec(
                select(CompanyRegistry.slug).where(active, zero).limit(200)).all()]
        if sample_slugs:
            held = session.exec(
                select(func.count(func.distinct(Job.company)))
                .where(Job.company.in_(sample_slugs))
            ).one()
            out["sample_slugs_checked"] = len(sample_slugs)
            out["sample_slugs_with_jobs"] = int(
                held[0] if isinstance(held, tuple) else held)

    return out


def _show(d: dict) -> None:
    print(f"Zero-yield board census — {d['as_of']} UTC   (READ-ONLY)")
    print("=" * 74)

    print("\n1. SIZE")
    print(f"  active boards                {d['active_boards']:,}")
    print(f"  live (job_count > 0)         {d['live_boards']:,}")
    print(f"  ZERO-YIELD (job_count == 0)  {d['zero_yield']:,}   = {d['zero_yield_pct']}%")

    print("\n2. COST")
    print(f"  retry cadence                every {d['dead_interval_hours']}h")
    print(f"  polls/day they consume       {d['zero_yield_polls_per_day']:,}")
    print(f"  real completed fetches/24h   {d['completed_fetches_24h']:,}")
    print(f"  share of real capacity       {d['zero_yield_share_of_capacity_pct']}%")

    print("\n3. LIVENESS")
    print(f"  never successfully fetched   {d['never_fetched']:,}"
          f"   {_pct(d['never_fetched'], d['zero_yield'])}")
    print(f"  fetched OK but serve zero    {d['fetched_but_empty']:,}"
          f"   {_pct(d['fetched_but_empty'], d['zero_yield'])}")
    print(f"  carrying failures            {d['with_failures']:,}")

    print("\n4. AGE")
    print("  in registry:  " + "  ".join(
        f"{k}={v:,}" for k, v in d["registry_age_days"].items()))
    print("  last reached: " + "  ".join(
        f"{k}={v:,}" for k, v in d["last_seen_age"].items()))

    print("\n5. SOURCE MIX (top 8 by ATS)")
    for k, v in list(d["by_ats"].items())[:8]:
        print(f"  {k:<20} {v:>8,}   {_pct(v, d['zero_yield'])}")

    print("\n6. CONVERSION — did the retry ever pay off?")
    for label, c in d["conversion"].items():
        print(f"  registered {label:<4} ago: cohort {c['cohort']:>7,}  "
              f"ever produced {c['ever_produced']:>7,}  = {c['pct']}%")
    print(f"  chronically dead (>30d, fetched, never produced): "
          f"{d['chronically_dead']:,}")

    print("\n7. CAPACITY RECOVERED (polls/day freed)")
    for k, v in d["recovery_polls_per_day"].items():
        pct = (round(100.0 * v / d["completed_fetches_24h"], 1)
               if d.get("completed_fetches_24h") else None)
        print(f"  {k:<26} {v:>8,}   = {pct}% of current real capacity")

    if "sample_slugs_checked" in d:
        print(f"\nSANITY: of {d['sample_slugs_checked']} sampled zero-yield slugs, "
              f"{d['sample_slugs_with_jobs']} have jobs in the Job table")
        if d["sample_slugs_with_jobs"]:
            print("  -> job_count may be STALE on some rows; treat the census as "
                  "an upper bound\n     until that is reconciled.")

    print("\nHOW TO READ THIS")
    conv30 = (d["conversion"].get("30d") or {}).get("pct")
    if conv30 is not None and conv30 < 2:
        print(f"  30-day conversion is {conv30}% — these boards are paying rent. "
              "A longer cadence\n  costs almost nothing in missed postings; "
              "retiring the chronically dead costs less.")
    elif conv30 is not None:
        print(f"  30-day conversion is {conv30}% — non-trivial. These are an "
              "investment, so prefer a\n  LONGER CADENCE over retirement; "
              "retiring them forfeits the boards that convert.")
    print("  Nothing was changed. Decide the policy, then change "
          "PULSE_DEAD_INTERVAL_HOURS.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = collect()
    if args.json:
        print(_json.dumps(d, indent=2, default=str))
    else:
        _show(d)


if __name__ == "__main__":
    main()
