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

    print(f"Boards: {live:,} live · {fast:,} on the fast lane · "
          f"{scheduled:,} scheduled · {overdue:,} overdue (>10m late)")
    if ticks:
        last = ticks[0]
        age_min = (now - last.created_at).total_seconds() / 60
        totals = {"boards": 0, "changed": 0, "new_jobs": 0, "scored": 0, "alerts": 0}
        for t in ticks:
            try:
                m = json.loads(t.metadata_json or "{}")
                for k in totals:
                    totals[k] += int(m.get(k) or 0)
            except Exception:
                pass
        print(f"Ticks (24h): {len(ticks)} · last {age_min:.0f}m ago")
        print(f"24h totals: {totals['boards']:,} board polls · "
              f"{totals['changed']:,} changed · {totals['new_jobs']:,} new jobs · "
              f"{totals['scored']:,} fast-path scored · {totals['alerts']:,} lane alerts")
    else:
        print("Ticks (24h): NONE — the lane hasn't run. Check PULSE_LANE_ENABLED, "
              "server logs for 'Pulse lane ENABLED', and that the deploy restarted.")
    print(f"Fresh alerts delivered (24h, all lanes): {alerts_24h}")

    if ticks and overdue == 0:
        print("VERDICT: ✅ guarantee holding — fast lane live, floor on schedule.")
    elif ticks:
        print("VERDICT: ⚠ running, but some boards are behind schedule "
              "(normal during the first post-deploy hour while the backlog drains).")
    else:
        print("VERDICT: ❌ not running.")


if __name__ == "__main__":
    main()
