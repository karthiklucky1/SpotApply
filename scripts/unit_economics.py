#!/usr/bin/env python3
"""What does DISCOVERY + MATCHING actually cost, and what does it deliver?

Reads the last N days of production data and answers, per user and averaged:

  - jobs retrieved into their pool (adoption), scored by each tier, and
    DELIVERED at each quality bar (>=60 / >=65 / >=70 / >=75)
  - LLM spend, computed TWO ways:
      reconstructed — call counts from the Job rows themselves x verified
        per-call rates (finals = rows with a real rerank_breakdown; prescores
        = rows with Job.prescore). Trustworthy for history.
      ledger — the LlmSpend table. CAVEAT: score_final rows written before
        the 2026-08-04 provider-gating fix under-report ~100x (audit bug #8),
        so the ledger is only честn for days after that seam. Both numbers
        are printed; when they disagree on old days, believe the reconstruction.
  - unit economics: cost per scored job, cost per DELIVERED job at each bar,
    and a package-pricing table ("N delivered jobs/month costs you $X; at a
    60% gross margin charge $Y") for subscription planning.

Read-only. Zero LLM spend. Discovery+matching only — tailoring/autofill spend
is reported separately and EXCLUDED from the package math, per the pricing
question this answers.

Honesty notes baked into the output:
  * ">=75" under the OLD Tier-2 contract is nearly unpopulated — Haiku never
    exceeded 72 in 300 production calls before the 2026-08-04 contract gave it
    explicit 90+ permission. The tool prints the full score distribution and
    flags how many window rows predate the seam, so you can price on a bar
    the data actually supports (>=65 today, >=75 once new-contract rows
    dominate).
  * Delivered counts use Job.first_seen as the window key (there is no
    scored_at column); lanes score fresh jobs within minutes, so first_seen
    approximates scored-at for everything except backlog drains.

    python -m scripts.unit_economics --days 7
    python -m scripts.unit_economics --days 7 --user <uid> --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

from sqlmodel import select

from app.db.init_db import get_session, init_db
from app.db.models import Job, LlmSpend

# Verified per-call rates (Anthropic first-party pricing; CAPACITY.md §3.3).
FINAL_WARM = 0.0033      # Haiku 4.5, cached resume prefix
FINAL_COLD = 0.0087      # first call of a burst (cache write) — prewarm makes these rare
PRESCORE = 0.0002        # gpt-4o-mini (assumed OpenAI rate, flagged in CAPACITY.md)
CONTRACT_SEAM = "2026-08-04"   # Tier-2 deterministic-cap / 90+ contract change
LEDGER_SEAM = "2026-08-04"     # LlmSpend provider-gating fix (audit bug #8)

BARS = (60, 65, 70, 75, 80)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="window length (default 7)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--margin", type=float, default=0.60,
                    help="target gross margin for the pricing table (default 0.60)")
    ap.add_argument("--json", default=None, help="write full result here")
    args = ap.parse_args()

    init_db()
    from app.discovery.pipeline import SHARED_POOL_USER
    since = datetime.utcnow() - timedelta(days=args.days)
    seam = datetime.strptime(CONTRACT_SEAM, "%Y-%m-%d")

    with get_session() as session:
        q = select(Job.user_id, Job.first_seen, Job.prescore, Job.rerank_score,
                   Job.rerank_breakdown.is_not(None)).where(
            Job.user_id != SHARED_POOL_USER,
            Job.first_seen >= since,
        )
        if args.user:
            q = q.where(Job.user_id == args.user)
        rows = session.exec(q).all()

        lq = select(LlmSpend).where(LlmSpend.day >= since.date())
        if args.user:
            lq = lq.where(LlmSpend.user_id == args.user)
        ledger = session.exec(lq).all()

    if not rows:
        print(f"No per-user jobs first seen in the last {args.days} days"
              + (f" for user {args.user}" if args.user else "") + ".")
        return 1

    # ── per-user aggregation from the Job rows (the reconstruction) ──────────
    users: dict = {}
    pre_seam_finals = 0
    for uid, first_seen, prescore, rscore, is_final in rows:
        u = users.setdefault(uid or "local", {
            "retrieved": 0, "prescored": 0, "finals": 0, "scored_any": 0,
            **{f"ge{b}": 0 for b in BARS},
        })
        u["retrieved"] += 1
        if prescore is not None:
            u["prescored"] += 1
        if rscore is not None:
            u["scored_any"] += 1
        if is_final:
            u["finals"] += 1
            if first_seen and first_seen < seam:
                pre_seam_finals += 1
            for b in BARS:
                if rscore is not None and rscore >= b:
                    u[f"ge{b}"] += 1

    n_users = len(users)
    days = max(1, args.days)
    tot = {k: sum(u[k] for u in users.values()) for k in next(iter(users.values()))}

    # Reconstructed matching spend. Finals priced warm — prewarm_cache writes
    # the prefix once per user/cycle, so bursts read at 0.1x; the cold rate is
    # shown as the pessimistic bound.
    recon_lo = tot["finals"] * FINAL_WARM + tot["prescored"] * PRESCORE
    recon_hi = tot["finals"] * FINAL_COLD + tot["prescored"] * PRESCORE

    # Ledger view, split at the seam it can be trusted from.
    led = {"match_pre_seam": 0.0, "match_post_seam": 0.0, "tailor": 0.0, "other": 0.0}
    led_calls = {"final": 0, "prescore": 0}
    seam_d = seam.date()
    for r in ledger:
        if r.kind in ("score_final", "score_prescore"):
            key = "match_post_seam" if r.day >= seam_d else "match_pre_seam"
            led[key] += r.est_cost_usd
            led_calls["final" if r.kind == "score_final" else "prescore"] += r.calls
        elif r.kind == "tailor":
            led["tailor"] += r.est_cost_usd
        else:
            led["other"] += r.est_cost_usd

    # ── report ───────────────────────────────────────────────────────────────
    print(f"=== Discovery + matching, last {args.days} days, {n_users} user(s) ===\n")
    hdr = (f"{'user':<14}{'retr':>6}{'presc':>7}{'finals':>7}"
           + "".join(f"{'>=' + str(b):>6}" for b in BARS))
    print(hdr)
    for uid, u in sorted(users.items(), key=lambda kv: -kv[1]["finals"]):
        print(f"{uid[:13]:<14}{u['retrieved']:>6}{u['prescored']:>7}{u['finals']:>7}"
              + "".join(f"{u[f'ge{b}']:>6}" for b in BARS))
    print(f"{'TOTAL':<14}{tot['retrieved']:>6}{tot['prescored']:>7}{tot['finals']:>7}"
          + "".join(f"{tot[f'ge{b}']:>6}" for b in BARS))

    print(f"\nPer user-day averages ({n_users} users x {days} days):")
    per_ud = {k: tot[k] / (n_users * days) for k in tot}
    print(f"  retrieved {per_ud['retrieved']:.1f} | prescored {per_ud['prescored']:.1f} | "
          f"Claude finals {per_ud['finals']:.1f} | "
          + " | ".join(f">={b}: {per_ud[f'ge{b}']:.2f}" for b in BARS))
    print("  monthly projection (x30): "
          + " | ".join(f">={b}: {per_ud[f'ge{b}'] * 30:.0f}" for b in BARS))

    print("\nSpend (matching only — tailoring shown separately, excluded from packages):")
    print(f"  reconstructed: ${recon_lo:.2f} (warm cache) .. ${recon_hi:.2f} (all-cold bound)")
    print(f"  ledger:        ${led['match_post_seam']:.2f} post-{LEDGER_SEAM}"
          f" + ${led['match_pre_seam']:.2f} pre-seam (UNDER-REPORTS ~100x — bug #8; "
          f"believe the reconstruction for those days)")
    print(f"  tailoring (excluded): ${led['tailor']:.2f} ledger")
    per_user_month_lo = recon_lo / n_users / days * 30
    per_user_month_hi = recon_hi / n_users / days * 30
    print(f"  matching $/user/month: ${per_user_month_lo:.2f} .. ${per_user_month_hi:.2f}")

    if pre_seam_finals:
        print(f"\nWARNING: {pre_seam_finals}/{tot['finals']} finals in this window predate "
              f"the {CONTRACT_SEAM} Tier-2 contract (old Haiku never exceeded 72, so "
              f">=75 counts are structurally depressed). Price on >=65 until "
              f"new-contract rows dominate, then re-run.")

    print(f"\n=== Package pricing (at {args.margin:.0%} gross margin, matching cost only) ===")
    print("Cost basis: warm-cache reconstruction. Add your infra share "
          "(~$45-80/mo fixed / N users) before setting final prices.\n")
    scored = tot["finals"] or 1
    cost_per_final = recon_lo / scored
    print(f"{'bar':>5}{'deliv/window':>14}{'finals per delivered':>22}"
          f"{'cost per delivered':>20}{'500-job pkg cost':>18}{'price @margin':>15}")
    result_pkgs = {}
    for b in BARS:
        d = tot[f"ge{b}"]
        if d == 0:
            print(f"{'>=' + str(b):>5}{0:>14}{'—':>22}{'—':>20}{'—':>18}"
                  f"{'no data at this bar':>15}")
            continue
        finals_per = scored / d
        cost_per = cost_per_final * finals_per
        pkg500 = cost_per * 500
        price = pkg500 / (1 - args.margin)
        result_pkgs[b] = {"delivered": d, "finals_per_delivered": round(finals_per, 1),
                          "cost_per_delivered": round(cost_per, 4),
                          "pkg500_cost": round(pkg500, 2),
                          "pkg500_price_at_margin": round(price, 2)}
        print(f"{'>=' + str(b):>5}{d:>14}{finals_per:>22.1f}{('$%.4f' % cost_per):>20}"
              f"{('$%.2f' % pkg500):>18}{('$%.2f' % price):>15}")

    print("\nRead the 500-job package line at the bar you sell as 'perfect match'. "
          "The package must ALSO fit inside the plan's finals_daily cap: 500 delivered "
          "at F finals-per-delivered needs 500xF finals/month =~ "
          "17xF finals/day within PLAN_LIMITS.")

    if args.json:
        out = {"window_days": args.days, "users": users, "totals": tot,
               "per_user_day": {k: round(v, 3) for k, v in per_ud.items()},
               "spend_reconstructed_usd": [round(recon_lo, 2), round(recon_hi, 2)],
               "spend_ledger": {k: round(v, 2) for k, v in led.items()},
               "ledger_calls": led_calls,
               "matching_usd_per_user_month": [round(per_user_month_lo, 2),
                                               round(per_user_month_hi, 2)],
               "pre_seam_finals": pre_seam_finals,
               "packages_500": result_pkgs}
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nFull result written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
