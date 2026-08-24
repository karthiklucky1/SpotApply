"""Read-only before/after snapshot for the discovery + expiry fixes.

One command that prints every number the Aug 2026 investigation asked for, so
"before" and "after" are the same measurement rather than two different ad-hoc
queries::

    python -m scripts.verify_discovery_fix            # 24h window
    python -m scripts.verify_discovery_fix --hours 6
    python -m scripts.verify_discovery_fix --json     # machine-readable

STRICTLY READ-ONLY. Every statement is a SELECT; nothing is written, updated or
deleted. Safe to run against production.

Cheap by construction — counts and small ordered slices only, no ``select(Job)``
and no unindexed predicate (docs/CAPACITY.md: full-row scans put Supabase at
205% of its egress quota).

Companion tools:
  * ``scripts.pulse_check``   — pulse-lane detail and the capacity verdict
  * ``scripts.stage_latency`` — first_seen -> prescored/scored/shortlisted
"""
from __future__ import annotations

import argparse
import json as _json
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import select

from app.common.freshness import (
    expired_without_scoring_expr,
    terminal_verdict_expr, tier1_drain_expr,
)
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    Application, ApplicationStatus, CompanyRegistry, FunnelEvent, Job,
)
from app.discovery.pipeline import SHARED_POOL_USER


def collect(hours: int) -> dict:
    now = datetime.utcnow()
    since = now - timedelta(hours=hours)
    out: dict = {"window_hours": hours, "as_of": now.isoformat()}

    with get_session() as session:
        def cnt(model_id, *where) -> int:
            v = session.exec(select(func.count(model_id)).where(*where)).one()
            return int(v[0] if isinstance(v, tuple) else v)

        # ── Discovery / polling ──────────────────────────────────────────────
        live = cnt(CompanyRegistry.id, CompanyRegistry.is_active == True,  # noqa: E712
                   CompanyRegistry.job_count > 0)
        out["live_boards"] = live
        out["overdue_boards"] = cnt(
            CompanyRegistry.id,
            CompanyRegistry.is_active == True,  # noqa: E712
            CompanyRegistry.job_count > 0,
            CompanyRegistry.next_poll_at != None,  # noqa: E711
            CompanyRegistry.next_poll_at < now - timedelta(minutes=10))
        # last_seen moves ONLY on a completed fetch (_mark_polled). A deferral
        # never touches it, so this cannot be inflated by mere selection.
        out["unique_boards_fetched"] = cnt(
            CompanyRegistry.id,
            CompanyRegistry.last_seen != None,  # noqa: E711
            CompanyRegistry.last_seen >= since)

        ticks = session.exec(
            select(FunnelEvent.metadata_json)
            .where(FunnelEvent.stage == "pulse_tick", FunnelEvent.created_at >= since)
            .order_by(FunnelEvent.created_at.desc()).limit(5000)
        ).all()
        agg = {k: 0 for k in ("selected", "started", "fetch_ok", "fetch_failed",
                              "unsupported", "deferred", "deferred_cancelled",
                              "deferred_running", "deferred_unconsumed",
                              "changed", "unchanged", "new_jobs", "scored",
                              "shortlisted", "alerts")}
        legacy = 0
        lat = []
        for row in ticks:
            raw = row[0] if isinstance(row, tuple) else row
            try:
                m = _json.loads(raw or "{}")
            except Exception:
                continue
            if "fetch_ok" not in m:
                # Pre-split tick: recorded the SELECTION only, so it cannot
                # contribute a poll count. Never back-filled with a guess.
                legacy += 1
                agg["selected"] += int(m.get("boards") or 0)
                agg["new_jobs"] += int(m.get("new_jobs") or 0)
                continue
            for k in agg:
                agg[k] += int(m.get(k) or 0)
            if m.get("fetch_p50_ms"):
                lat.append(int(m["fetch_p50_ms"]))

        out["ticks"] = len(ticks)
        out["pre_split_ticks"] = legacy
        out.update({f"tick_{k}": v for k, v in agg.items()})
        out["deferred_pct"] = (round(100.0 * agg["deferred"] / agg["selected"], 1)
                               if agg["selected"] else None)
        out["completed_fetches_per_tick"] = (
            round(agg["fetch_ok"] / (len(ticks) - legacy), 1)
            if (len(ticks) - legacy) > 0 else None)
        out["completed_fetches_per_day"] = (
            round(agg["fetch_ok"] * 24.0 / hours) if hours else None)
        out["fetch_p50_ms"] = (sorted(lat)[len(lat) // 2] if lat else None)
        # The honest floor number: how long between real visits to a board.
        out["effective_revisit_hours"] = (
            round(live / (agg["fetch_ok"] / hours), 1)
            if (agg["fetch_ok"] and live and hours) else None)
        out["floor_promise_minutes"] = settings.pulse_floor_interval_minutes

        # ── Intake ───────────────────────────────────────────────────────────
        out["shared_pool_new"] = cnt(
            Job.id, Job.user_id == SHARED_POOL_USER, Job.first_seen >= since)
        out["user_pool_new"] = cnt(
            Job.id,
            (Job.user_id.is_(None)) | (Job.user_id != SHARED_POOL_USER),
            Job.first_seen >= since)

        # ── Expiry ───────────────────────────────────────────────────────────
        per_user = ((Job.user_id.is_(None)) | (Job.user_id != SHARED_POOL_USER))
        out["expired_stale"] = cnt(
            Job.id, per_user, expired_without_scoring_expr(),
            Job.first_seen >= since)
        # Terminal verdicts, and the tier split. NOT "Claude finals" — see
        # terminal_verdict_expr. Reported separately because conflating them
        # made a healthy lane look broken.
        out["terminal_verdicts"] = cnt(
            Job.id, per_user, terminal_verdict_expr(), Job.first_seen >= since)
        out["tier1_drains"] = cnt(
            Job.id, per_user, tier1_drain_expr(), Job.first_seen >= since)
        out["tier2_or_rule_verdicts"] = out["terminal_verdicts"] - out["tier1_drains"]
        # Deprecated alias, kept so an older reader doesn't silently lose the row.
        out["genuinely_scored"] = out["terminal_verdicts"]
        out["pending_scoring"] = cnt(
            Job.id, per_user, Job.rerank_score.is_(None), Job.is_closed == False)  # noqa: E712

        # INTAKE-EXPIRY RATE — of the jobs that ENTERED a user pool during this
        # window, how many were expired straight away on the source's posting
        # date?
        #
        # The metric this replaces was structurally incapable of being anything
        # but 100%. It sampled `expired AND first_seen >= since` and asked
        # whether each row was already past the known-age bound at discovery.
        # But a queue-stale expiry REQUIRES first_seen < now - known_days, so it
        # can never appear in a window shorter than that — the sample could only
        # ever contain posting-age expiries, and those satisfy the test by
        # definition. The denominator could not hold a counter-example, so the
        # figure read 100% in production while nothing was wrong, and the advice
        # line told the operator to go check a setting that was already correct.
        #
        # The honest denominator is the same-window INTAKE, not the same-window
        # expiries. `user_pool_new` is exactly that: per-user rows (shared pool
        # excluded, matching the numerator's scope) whose first_seen falls in the
        # window. Deliberately NOT `tick_new_jobs`, which sums shared-pool AND
        # per-user upserts and would understate the rate against a per-user
        # numerator.
        posted_days = int(getattr(settings, "scoring_max_posted_age_days", 0) or 0)
        intake = out["user_pool_new"]
        out["intake_expired"] = cnt(
            Job.id, per_user, Job.first_seen >= since,
            expired_without_scoring_expr())
        # Split by which bound actually fired, tested on the row itself rather
        # than on reasoning text.
        out["intake_expired_ancient"] = cnt(
            Job.id, per_user, Job.first_seen >= since,
            expired_without_scoring_expr(),
            Job.posted_at.is_not(None),
            Job.posted_at < now - timedelta(days=posted_days)) if posted_days > 0 else 0
        out["intake_expired_queue_stale"] = (
            out["intake_expired"] - out["intake_expired_ancient"])
        out["intake_expired_pct"] = (round(100.0 * out["intake_expired"] / intake, 1)
                                     if intake else None)
        out["intake_expired_ancient_pct"] = (
            round(100.0 * out["intake_expired_ancient"] / intake, 1)
            if intake else None)

        # ── Scoring ──────────────────────────────────────────────────────────
        cycles = session.exec(
            select(FunnelEvent.metadata_json)
            .where(FunnelEvent.stage == "scoring_cycle", FunnelEvent.created_at >= since)
            .order_by(FunnelEvent.created_at.desc()).limit(5000)
        ).all()
        cagg = {k: 0 for k in ("users", "queued", "scored", "drained",
                               "shortlisted", "alerts", "expired_stale",
                               "expired_queue_stale", "expired_ancient")}
        for row in cycles:
            raw = row[0] if isinstance(row, tuple) else row
            try:
                m = _json.loads(raw or "{}")
            except Exception:
                continue
            for k in cagg:
                cagg[k] += int(m.get(k) or 0)
        out["scoring_cycles"] = len(cycles)
        out.update({f"cycle_{k}": v for k, v in cagg.items()})

        # _scorable_user_ids is a live call, not a stored metric — it is the
        # thing that read ZERO for 11 of 13 users because the gate had stamped
        # their pools empty.
        try:
            from app.strategy.scoring_lane import _scorable_user_ids
            out["scorable_users"] = len(_scorable_user_ids())
        except Exception as e:
            out["scorable_users"] = f"unavailable: {e}"

        # Finals consumption, from the PERSISTED per-user counters (they live in
        # user_usage precisely so a deploy cannot reset the week).
        try:
            from app.db.models import UserUsage
            rows = session.exec(
                select(UserUsage.finals_count, UserUsage.finals_hits)
                .where(UserUsage.usage_date == now.date())
            ).all()
            out["finals_today"] = sum(int(r[0] or 0) for r in rows)
            out["finals_hits_today"] = sum(int(r[1] or 0) for r in rows)
            out["finals_users_today"] = len(rows)
        except Exception as e:
            out["finals_today"] = f"unavailable: {e}"
            out["finals_hits_today"] = None

        # ── Delivery ─────────────────────────────────────────────────────────
        out["shortlisted_new"] = cnt(
            Application.id, Application.created_at >= since)
        out["shortlisted_open"] = cnt(
            Application.id, Application.status == ApplicationStatus.SHORTLISTED)
        out["fresh_alerts"] = cnt(
            FunnelEvent.id, FunnelEvent.stage == "fresh_alert",
            FunnelEvent.created_at >= since)

    return out


def _show(d: dict) -> None:
    w = d["window_hours"]
    print(f"SpotApply pipeline verification — last {w}h (as of {d['as_of']} UTC)")
    print("=" * 74)

    print("\nDISCOVERY / POLLING")
    print(f"  live boards                  {d['live_boards']:,}")
    print(f"  ticks in window              {d['ticks']:,}"
          + (f"  ({d['pre_split_ticks']} pre-split, selection-only)"
             if d["pre_split_ticks"] else ""))
    print(f"  boards SELECTED              {d['tick_selected']:,}")
    print(f"  fetches started              {d['tick_started']:,}")
    print(f"  fetches COMPLETED            {d['tick_fetch_ok']:,}   <- the real poll count")
    print(f"  fetches failed               {d['tick_fetch_failed']:,} "
          f"(+{d['tick_unsupported']:,} unsupported)")
    print(f"  fetches DEFERRED             {d['tick_deferred']:,}"
          f"  ({d['tick_deferred_cancelled']:,} never started, "
          f"{d['tick_deferred_running']:,} running, "
          f"{d['tick_deferred_unconsumed']:,} unprocessed)")
    print(f"  DEFERRED %                   {d['deferred_pct']}")
    print(f"  completed fetches / tick     {d['completed_fetches_per_tick']}")
    print(f"  completed fetches / day      {d['completed_fetches_per_day']}")
    print(f"  unique boards fetched        {d['unique_boards_fetched']:,}")
    print(f"  EFFECTIVE REVISIT            {d['effective_revisit_hours']}h "
          f"(promise: {d['floor_promise_minutes']}m)")
    print(f"  fetch latency p50            {d['fetch_p50_ms']}ms")
    print(f"  overdue boards               {d['overdue_boards']:,}")

    print("\nINTAKE")
    print(f"  new shared-pool postings     {d['shared_pool_new']:,}")
    print(f"  new per-user rows            {d['user_pool_new']:,}")
    print(f"  jobs discovered via pulse    {d['tick_new_jobs']:,}")

    print("\nEXPIRY  (denominator = jobs that ENTERED a user pool this window)")
    print(f"  intake (user_pool_new)       {d['user_pool_new']:,}")
    print(f"  of which expired             {d['intake_expired']:,}"
          f"   = {d['intake_expired_pct']}%")
    print(f"    on source posting age      {d['intake_expired_ancient']:,}"
          f"   = {d['intake_expired_ancient_pct']}%")
    print(f"    on queue staleness         {d['intake_expired_queue_stale']:,}")
    print(f"  cycle expiries: queue-stale  {d['cycle_expired_queue_stale']:,}"
          f"  ancient {d['cycle_expired_ancient']:,}")

    print("\nSCORING VERDICTS  (terminal = ANY tier, not just Claude finals)")
    print(f"  terminal verdicts (window)   {d['terminal_verdicts']:,}")
    print(f"    Tier-1 drains              {d['tier1_drains']:,}")
    print(f"    Tier-2 finals / rule stamps{d['tier2_or_rule_verdicts']:>7,}")

    print("\nSCORING")
    print(f"  scoring cycles               {d['scoring_cycles']:,}")
    print(f"  _scorable_user_ids           {d['scorable_users']}")
    print(f"  queued / scored / drained    {d['cycle_queued']:,} / "
          f"{d['cycle_scored']:,} / {d['cycle_drained']:,}")
    print(f"  finals consumed today        {d['finals_today']}")
    print(f"  pending (open, unscored)     {d['pending_scoring']:,}")

    print("\nDELIVERY")
    print(f"  shortlisted in window        {d['shortlisted_new']:,}")
    print(f"  shortlist open now           {d['shortlisted_open']:,}")
    print(f"  fresh alerts                 {d['fresh_alerts']:,}")

    print("\nREAD THE RESULT")
    dp = d["deferred_pct"]
    if dp is None:
        print("  No post-split ticks yet — re-run once the deploy has been up "
              "for an hour.")
    elif dp >= 20:
        print(f"  Still capacity-limited ({dp}% deferred). The deferral split "
              "above says which lever:\n"
              "    never started   -> batch too large for the worker-seconds\n"
              "    running         -> slow hosts; bound per-fetch time\n"
              "    unprocessed     -> serial post-fetch DB work is the limit")
    else:
        print(f"  Deferral rate {dp}% — the lane is fetching what it selects. "
              "Judge the floor on\n  EFFECTIVE REVISIT above, not on next_poll_at.")
    # The line this replaces fired on a percentage that could only ever be
    # 100% (see intake-expiry above) and sent the operator to check a setting
    # that was already correct. This one is judged against the window's INTAKE,
    # so it can actually be low — and it does not moralise about the posting-age
    # arm, which suppressing evergreen listings is SUPPOSED to trigger.
    iep = d["intake_expired_pct"]
    if iep is None:
        print("  No intake in this window — widen --hours before reading the "
              "expiry rate.")
    elif iep >= 50:
        print(f"  {iep}% of this window's intake was expired on arrival "
              f"({d['intake_expired_ancient_pct']}% of it on source posting age).\n"
              "  That is the evergreen filter doing its job IF the sources are "
              "genuinely serving old\n  listings — check a sample before "
              "changing SCORING_MAX_POSTED_AGE_DAYS "
              f"(={getattr(settings, 'scoring_max_posted_age_days', '?')}d). "
              "A high rate is a SOURCE-MIX\n  signal, not necessarily a bug.")
    if d["intake_expired_queue_stale"]:
        print(f"  {d['intake_expired_queue_stale']:,} job(s) entered the pool and "
              "aged out unscored inside this window —\n  the scorer is behind the "
              "intake, not the gate.")
    if d["terminal_verdicts"] and d["tier1_drains"] == d["terminal_verdicts"]:
        print("  Every terminal verdict this window was a Tier-1 drain — no "
              "Tier-2 finals were bought.\n  Expected once the per-plan finals "
              "budget is spent for the day (scoring_drain_cap keeps\n  the cheap "
              "drain running); check `finals consumed today` against the plan cap.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()
    data = collect(args.hours)
    if args.json:
        print(_json.dumps(data, indent=2, default=str))
    else:
        _show(data)


if __name__ == "__main__":
    main()
