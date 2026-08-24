"""Pulse-lane health snapshot — run this to verify the freshness guarantee is live.

Usage:
    python -m scripts.pulse_check          # against the configured DB (prod or local)

Prints: whether ticks are running, board scheduling coverage (fast lane / hourly
floor / dead), overdue boards (floor health), and the last 24h of new jobs +
fresh alerts produced by the lane.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlmodel import select
from sqlalchemy import func

from app.config import settings
from app.db.init_db import get_session
from app.db.models import CompanyRegistry, FunnelEvent, UserNotification


def main() -> None:
    now = datetime.utcnow()
    print(f"Pulse lane enabled: {settings.pulse_lane_enabled} "
          f"(fast={settings.pulse_fast_interval_minutes}m, "
          f"floor={settings.pulse_floor_interval_minutes}m)")

    with get_session() as session:
        def cnt(q) -> int:
            v = session.exec(q).one()
            return int(v[0] if isinstance(v, tuple) else v)

        active_cut = now - timedelta(days=settings.pulse_active_days)
        live = cnt(select(func.count(CompanyRegistry.id)).where(
            CompanyRegistry.is_active == True, CompanyRegistry.job_count > 0))  # noqa: E712
        fast = cnt(select(func.count(CompanyRegistry.id)).where(
            CompanyRegistry.is_active == True,  # noqa: E712
            CompanyRegistry.last_new_job_at != None,  # noqa: E711
            CompanyRegistry.last_new_job_at >= active_cut))
        scheduled = cnt(select(func.count(CompanyRegistry.id)).where(
            CompanyRegistry.is_active == True,  # noqa: E712
            CompanyRegistry.next_poll_at != None))  # noqa: E711
        overdue = cnt(select(func.count(CompanyRegistry.id)).where(
            CompanyRegistry.is_active == True,  # noqa: E712
            CompanyRegistry.job_count > 0,
            CompanyRegistry.next_poll_at != None,  # noqa: E711
            CompanyRegistry.next_poll_at < now - timedelta(minutes=10)))

        ticks = session.exec(
            select(FunnelEvent).where(
                FunnelEvent.stage == "pulse_tick",
                FunnelEvent.created_at > now - timedelta(hours=24))
            .order_by(FunnelEvent.created_at.desc()).limit(2000)
        ).all()
        alerts_24h = cnt(select(func.count(UserNotification.id)).where(
            UserNotification.type == "fresh_job",
            UserNotification.created_at > now - timedelta(hours=24)))

        # Boards we ACTUALLY reached. last_seen moves only on a COMPLETED fetch
        # (_mark_polled); a deferral never touches it, so this cannot be
        # inflated by boards the lane merely selected.
        reached_24h = cnt(select(func.count(CompanyRegistry.id)).where(
            CompanyRegistry.last_seen != None,  # noqa: E711
            CompanyRegistry.last_seen >= now - timedelta(hours=24)))

    print(f"Boards: {live:,} live · {fast:,} on the fast lane · "
          f"{scheduled:,} scheduled · {overdue:,} overdue (>10m late)")
    deferred_pct = None
    revisit_h = None
    if ticks:
        last = ticks[0]
        age_min = (now - last.created_at).total_seconds() / 60
        # SELECTED IS NOT POLLED. This script used to print
        # sum(stats["boards"]) — the SELECTION — under the label "board polls",
        # which is how ~8x the real poll rate ended up in an investigation.
        # Every bucket below is now reported under its own name.
        totals = {"selected": 0, "started": 0, "fetch_ok": 0, "fetch_failed": 0,
                  "unsupported": 0, "deferred": 0, "deferred_cancelled": 0,
                  "deferred_running": 0, "deferred_unconsumed": 0,
                  "unchanged": 0, "changed": 0, "new_jobs": 0, "scored": 0,
                  "alerts": 0}
        legacy_ticks = 0
        lat = []
        for t in ticks:
            try:
                m = json.loads(t.metadata_json or "{}")
            except Exception:
                continue
            if "fetch_ok" not in m:
                # Written before the outcome split shipped: it recorded only the
                # selection, so it CANNOT contribute a poll count. Counted
                # separately rather than guessed at.
                legacy_ticks += 1
                totals["selected"] += int(m.get("boards") or 0)
                totals["changed"] += int(m.get("changed") or 0)
                totals["new_jobs"] += int(m.get("new_jobs") or 0)
                totals["scored"] += int(m.get("scored") or 0)
                totals["alerts"] += int(m.get("alerts") or 0)
                continue
            for k in totals:
                totals[k] += int(m.get(k) or 0)
            if m.get("fetch_p50_ms"):
                lat.append(int(m["fetch_p50_ms"]))

        print(f"Ticks (24h): {len(ticks)} · last {age_min:.0f}m ago"
              + (f" · {legacy_ticks} pre-split tick(s) report selection only"
                 if legacy_ticks else ""))
        print(f"  boards selected      : {totals['selected']:,}")
        print(f"  fetches started      : {totals['started']:,}")
        print(f"  fetches COMPLETED ok : {totals['fetch_ok']:,}   <- the real poll count")
        print(f"  fetches failed       : {totals['fetch_failed']:,} "
              f"(+{totals['unsupported']:,} unsupported/retired)")
        print(f"  fetches deferred     : {totals['deferred']:,} "
              f"({totals['deferred_cancelled']:,} never started, "
              f"{totals['deferred_running']:,} still running, "
              f"{totals['deferred_unconsumed']:,} fetched-but-unprocessed)")
        print(f"  boards changed       : {totals['changed']:,} "
              f"(+{totals['unchanged']:,} unchanged)")
        print(f"  jobs discovered      : {totals['new_jobs']:,} "
              f"· {totals['scored']:,} fast-path scored "
              f"· {totals['alerts']:,} lane alerts")
        print(f"  unique boards reached: {reached_24h:,} (registry last_seen, 24h)")
        if lat:
            lat.sort()
            print(f"  fetch latency p50    : {lat[len(lat) // 2]:,}ms "
                  f"(median of per-tick medians)")
        # POST-FETCH CONSUMER COST — the breakdown that explains
        # deferred_unconsumed. The fetch is already done for those boards, so
        # whatever is still costing time is in one of these stages.
        stages: dict = {}
        for t in ticks:
            try:
                m = json.loads(t.metadata_json or "{}")
            except Exception:
                continue
            for stage, c in (m.get("consumer_ms") or {}).items():
                acc = stages.setdefault(stage, {"n": 0, "total_ms": 0, "p50": [],
                                                "p90": [], "p95": []})
                acc["n"] += int(c.get("n") or 0)
                acc["total_ms"] += int(c.get("total_ms") or 0)
                for q in ("p50", "p90", "p95"):
                    if c.get(q) is not None:
                        acc[q].append(int(c[q]))
        if stages:
            grand = sum(v["total_ms"] for v in stages.values()) or 1
            print("  post-fetch consumer cost per stage (24h):")
            for stage, v in sorted(stages.items(),
                                   key=lambda kv: -kv[1]["total_ms"]):
                def _med(xs):
                    return sorted(xs)[len(xs) // 2] if xs else 0
                print(f"    {stage:<14} n={v['n']:>7,}  "
                      f"p50={_med(v['p50']):>5}ms p90={_med(v['p90']):>6}ms "
                      f"p95={_med(v['p95']):>6}ms  "
                      f"share={100.0 * v['total_ms'] / grand:5.1f}%")
            print("    ^ the stage with the largest SHARE is the remaining "
                  "serial bottleneck.")
        if totals["selected"]:
            deferred_pct = 100.0 * totals["deferred"] / totals["selected"]
            print(f"  DEFERRAL RATE        : {deferred_pct:.1f}% of selected boards "
                  "were never fetched")
        if totals["fetch_ok"] and live:
            revisit_h = live / (totals["fetch_ok"] / 24.0)
            print(f"  EFFECTIVE REVISIT    : ~{revisit_h:.1f}h between visits to a "
                  f"given board (vs a {settings.pulse_floor_interval_minutes}m "
                  "floor promise)")
    else:
        print("Ticks (24h): NONE — the lane hasn't run. Check PULSE_LANE_ENABLED, "
              "server logs for 'Pulse lane ENABLED', and that the deploy restarted.")
    print(f"Fresh alerts delivered (24h, all lanes): {alerts_24h}")

    # Say WHICH world we are in. A handful of boards briefly late is a backlog
    # draining; a lane that defers most of what it selects is capacity-limited
    # and will NOT drain on its own — calling that "catching up" is how it went
    # unnoticed for weeks. The deferral rate is checked FIRST and independently,
    # because overdue_boards was measured against a schedule the lane itself was
    # falsifying (deferred boards used to have next_poll_at advanced exactly like
    # polled ones, so the column said "on time" about fetches that never ran).
    pct = (100.0 * overdue / live) if live else None
    if not ticks:
        print("VERDICT: ❌ not running.")
    elif deferred_pct is not None and deferred_pct >= 20:
        print(f"VERDICT: ❌ CAPACITY-LIMITED — {deferred_pct:.1f}% of selected boards "
              f"are never fetched"
              + (f", so a board is really visited about every {revisit_h:.1f}h "
                 f"against a {settings.pulse_floor_interval_minutes}m promise"
                 if revisit_h else "")
              + ".\n  This is NOT a backlog and will not drain on its own. Check the "
                "deferral split above first:\n"
                "    mostly 'never started'          -> batch too large for the "
                "worker-seconds available\n"
                "    mostly 'still running'          -> slow hosts; bound the "
                "per-fetch time, not the worker count\n"
                "    mostly 'fetched-but-unprocessed'-> the serial post-fetch work "
                "(registry writes/upserts) is the\n"
                "                                       bottleneck; more workers "
                "would make it worse\n"
              "  Sizing: demand/h = live x 60/floor + fast x 60/fast_interval; "
              "capacity/h = completed fetches x 24/24.")
    elif overdue == 0:
        print("VERDICT: ✅ guarantee holding — fast lane live, floor on schedule.")
    elif pct is not None and pct < 5:
        print(f"VERDICT: ✅ floor essentially holding — {overdue} board(s) "
              f"({pct:.1f}%) briefly behind.")
    elif pct is not None and pct < 20:
        print(f"VERDICT: ⚠ {overdue} board(s) ({pct:.1f}%) behind schedule, and the "
              "lane is fetching everything it selects — a backlog draining, or a "
              "slow source. Re-check in an hour.")
    elif pct is not None:
        print(f"VERDICT: ❌ floor NOT holding — {overdue} of {live} boards "
              f"({pct:.1f}%) past the sweep interval, without a high deferral rate. "
              "Compare demand (live_boards × 60/floor_interval + fast_boards × "
              "60/fast_interval) against real completed fetches/hour above.")
    else:
        print(f"VERDICT: ⚠ running, {overdue} board(s) behind schedule.")


if __name__ == "__main__":
    main()
