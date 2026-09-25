"""Who is being charged, and what would change — READ-ONLY, by construction.

Phase 7 asks for a subscription audit kept SEPARATE from the temporary-Pro
change, and for a transition prepared *for review* before any cancellation,
refund or other financial mutation. So this script has no `--apply`, no write
path and no Stripe mutation call anywhere in it. It opens one database session,
SELECTs, and prints. `test_temporary_pro` pins the absence of the write calls.

What it answers:

  * how many subscription rows exist, and of what kind — Stripe-backed vs
    manual/bank activation vs already FREE;
  * which of them `billing.is_paid_entitlement` counts as PAYING right now, and
    therefore what MRR is;
  * whether the configured Stripe key can move real money at all
    (`stripe_live_mode`) — under `sk_test_` a "subscription" bills test invoices
    and no one is charged;
  * rows whose `current_period_end` is NULL, which `entitlement_expired` reads
    as "never expires" (the founder's row sat like that for two weeks);
  * what each row's entitlement would be if TEMPORARY_PRO_FOR_ALL were turned
    off tomorrow — the only number that matters for a transition.

It never prints a user id, an email, a Stripe customer/subscription id or a key.
Rows are counted and bucketed; where an individual row must be shown it is shown
as an 8-character fingerprint, the same convention `/api/admin/budget-diagnostic`
uses.

Run it where the database is reachable:

    python -m scripts.audit_subscriptions
    python -m scripts.audit_subscriptions --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from typing import Optional


def _fp(value: Optional[str]) -> str:
    """Stable 8-char fingerprint. Enough to correlate two lines, not to identify."""
    if not value:
        return "--------"
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def audit() -> dict:
    from app import billing
    from app.db.init_db import get_session
    from app.db.models import PLAN_PRICES, PlanTier, UserProfile, UserSubscription
    from sqlmodel import func, select

    now = datetime.utcnow()
    live = billing.stripe_live_mode()

    out: dict = {
        "generated_at": now.isoformat() + "Z",
        "stripe_mode": billing.stripe_mode(),
        "stripe_can_charge_real_money": live,
        "temporary_pro_for_all": billing.temporary_pro_active(),
        "pro_price_usd": billing.pro_price_usd(),
    }

    with get_session() as session:
        rows = list(session.exec(select(UserSubscription)).all())
        # `.one()` on a COUNT gives a bare scalar on some drivers and a 1-tuple
        # on others; normalise rather than guess.
        raw = session.exec(select(func.count(UserProfile.id))
                           .where(UserProfile.user_id.isnot(None))).one()
        profiles = int(raw[0] if isinstance(raw, (list, tuple)) else raw)

    buckets = Counter()
    mrr = 0
    detail = []
    for r in rows:
        stripe_backed = bool(r.stripe_subscription_id or r.stripe_customer_id)
        expired = billing.entitlement_expired(r, now)
        paying = billing.is_paid_entitlement(r, now)
        if r.plan == PlanTier.FREE:
            kind = "already_free"
        elif expired:
            kind = "expired"
        elif not stripe_backed:
            kind = "manual_activation"          # paid outside Stripe; counts as paid
        elif live:
            kind = "stripe_live"                # real recurring charge
        else:
            kind = "stripe_sandbox"             # test-mode; bills test invoices only
        buckets[kind] += 1
        if paying:
            mrr += int(PLAN_PRICES.get(r.plan, 0) or 0)
        detail.append({
            "account": _fp(r.user_id),
            "kind": kind,
            "row_plan": r.plan.value if r.plan else None,
            "is_paid_entitlement": bool(paying),
            "period_end": r.current_period_end.isoformat() if r.current_period_end else None,
            "period_end_missing": r.current_period_end is None,
            "has_stripe_customer": bool(r.stripe_customer_id),
            "has_stripe_subscription": bool(r.stripe_subscription_id),
            # What this account would get the moment the flag is turned off.
            "entitlement_if_flag_off": (
                "PRO" if (r.plan and r.plan != PlanTier.FREE and not expired) else "FREE"),
        })

    out["profiles_total"] = profiles
    out["subscription_rows"] = len(rows)
    out["profiles_without_a_row"] = max(profiles - len(rows), 0)
    out["by_kind"] = dict(buckets)
    out["counted_as_paying"] = sum(1 for d in detail if d["is_paid_entitlement"])
    out["mrr_usd"] = mrr
    out["rows_missing_period_end"] = sum(1 for d in detail if d["period_end_missing"])
    out["would_be_pro_if_flag_off"] = sum(
        1 for d in detail if d["entitlement_if_flag_off"] == "PRO")
    out["rows"] = detail

    # THE question Phase 7 asks, answered from the numbers above rather than
    # from an assumption about how the account is configured.
    if buckets.get("stripe_live"):
        out["are_subscribers_still_charged"] = (
            f"YES for {buckets['stripe_live']} row(s). Stripe bills on its own "
            f"schedule; TEMPORARY_PRO_FOR_ALL is entitlement resolution on our "
            f"side and does not touch Stripe. Stopping those charges is a "
            f"financial mutation and is NOT done by this script.")
    elif buckets.get("stripe_sandbox"):
        out["are_subscribers_still_charged"] = (
            f"NO real money. {buckets['stripe_sandbox']} sandbox row(s) under a "
            f"test-mode key — Stripe issues test invoices and charges nobody. "
            f"They still receive webhooks and still show a subscription in the "
            f"portal.")
    else:
        out["are_subscribers_still_charged"] = (
            "NO recurring Stripe charge found. Manual activations are one-off "
            "payments already made; there is nothing recurring to stop.")
    return out


def _print_human(a: dict) -> None:
    print("SUBSCRIPTION AUDIT  (read-only)")
    print(f"  generated              {a['generated_at']}")
    print(f"  stripe mode            {a['stripe_mode']}"
          f"   can charge real money: {a['stripe_can_charge_real_money']}")
    print(f"  TEMPORARY_PRO_FOR_ALL  {a['temporary_pro_for_all']}")
    print()
    print(f"  profiles               {a['profiles_total']}")
    print(f"  subscription rows      {a['subscription_rows']}"
          f"   (profiles with no row: {a['profiles_without_a_row']})")
    for kind, n in sorted(a["by_kind"].items(), key=lambda kv: -kv[1]):
        print(f"    {kind:20} {n}")
    print()
    print(f"  counted as PAYING      {a['counted_as_paying']}   (MRR ${a['mrr_usd']})")
    print(f"  rows with NULL period  {a['rows_missing_period_end']}"
          f"   <- 'never expires' to entitlement_expired")
    print(f"  would be PRO if the flag went off today: {a['would_be_pro_if_flag_off']}")
    print()
    print("  Are current subscribers still charged?")
    for line in a["are_subscribers_still_charged"].split(". "):
        if line.strip():
            print(f"    {line.strip().rstrip('.')}.")
    if a["rows"]:
        print()
        print("  per row (fingerprints, never ids):")
        print(f"    {'acct':9} {'kind':19} {'plan':5} {'paying':7} {'if flag off':12} period_end")
        for d in a["rows"]:
            print(f"    {d['account']:9} {d['kind']:19} {str(d['row_plan']):5} "
                  f"{str(d['is_paid_entitlement']):7} {d['entitlement_if_flag_off']:12} "
                  f"{d['period_end'] or '(none)'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    a = audit()
    if args.json:
        print(json.dumps(a, indent=2))
    else:
        _print_human(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
