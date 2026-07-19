"""Billing — Stripe subscription checkout for the $10/mo Pro plan, plus a
manual bank-transfer path for the pre-Stripe period.

Designed to be safe BEFORE the business entity exists:
- Until STRIPE_SECRET_KEY is set, `stripe_enabled()` is False, every user
  resolves to PRO (the pre-revenue free-for-all in server._get_user_plan),
  and the dashboard upgrade flow shows the manual payment options instead
  (PAYMENT_BANK_DETAILS / PAYMENT_CONTACT_EMAIL) — activation is manual via
  the admin set-plan endpoint.
- Once the LLC + Stripe account exist, setting STRIPE_SECRET_KEY,
  STRIPE_PRICE_ID_PRO (a $10/mo recurring Price) and STRIPE_WEBHOOK_SECRET
  turns on real checkout + webhook-driven plan sync. No code change needed.

The `stripe` package is imported lazily so the app boots even when the
dependency isn't installed (e.g. a slim deployment that never enables it).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import PlanTier, UserSubscription

log = logging.getLogger(__name__)


def stripe_enabled() -> bool:
    return bool(settings.stripe_secret_key and settings.stripe_price_id_pro)


def payment_options() -> dict:
    """What the UI shows on the upgrade screen. Never includes secrets."""
    return {
        "price_monthly_usd": 10,
        "stripe_enabled": stripe_enabled(),
        "bank_transfer": bool(settings.payment_bank_details.strip()),
        "bank_details": settings.payment_bank_details.strip() or None,
        "contact_email": settings.payment_contact_email.strip() or None,
    }


def _stripe():
    import stripe  # lazy: optional dependency until payments launch
    stripe.api_key = settings.stripe_secret_key
    return stripe


def create_checkout_session(user_id: str, email: Optional[str], base_url: str) -> str:
    """Create a Stripe Checkout session for the Pro subscription; returns its URL."""
    if not stripe_enabled():
        raise RuntimeError("Stripe is not configured")
    stripe = _stripe()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": settings.stripe_price_id_pro, "quantity": 1}],
        success_url=f"{base_url}/dashboard?billing=success",
        cancel_url=f"{base_url}/pricing",
        client_reference_id=user_id,
        customer_email=email or None,
        allow_promotion_codes=True,
    )
    return session.url


def set_plan(user_id: str, plan: PlanTier,
             stripe_customer_id: Optional[str] = None,
             stripe_subscription_id: Optional[str] = None,
             current_period_end: Optional[datetime] = None) -> None:
    """Idempotent upsert of a user's subscription row."""
    with get_session() as session:
        row = session.exec(
            select(UserSubscription).where(UserSubscription.user_id == user_id)
        ).first()
        if row is None:
            row = UserSubscription(user_id=user_id)
        row.plan = plan
        if stripe_customer_id is not None:
            row.stripe_customer_id = stripe_customer_id
        if stripe_subscription_id is not None:
            row.stripe_subscription_id = stripe_subscription_id
        row.current_period_end = current_period_end
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
    log.info("Billing: user %s set to plan %s", user_id, plan.value)


def _period_end(sub) -> Optional[datetime]:
    ts = getattr(sub, "current_period_end", None) or (
        sub.get("current_period_end") if isinstance(sub, dict) else None)
    return datetime.utcfromtimestamp(ts) if ts else None


def handle_webhook(payload: bytes, signature: str) -> dict:
    """Verify + apply a Stripe webhook event. Raises ValueError on bad signature."""
    stripe = _stripe()
    if not settings.stripe_webhook_secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET not configured")
    try:
        event = stripe.Webhook.construct_event(
            payload, signature, settings.stripe_webhook_secret)
    except Exception as e:  # bad payload or signature — reject, never guess
        raise ValueError(f"webhook verification failed: {e}") from e

    etype = event["type"]
    obj = event["data"]["object"]

    if etype == "checkout.session.completed":
        user_id = obj.get("client_reference_id")
        if user_id:
            set_plan(user_id, PlanTier.PRO,
                     stripe_customer_id=obj.get("customer"),
                     stripe_subscription_id=obj.get("subscription"))
        else:
            log.warning("Billing webhook: checkout completed without client_reference_id")

    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = obj.get("id")
        status = obj.get("status")
        with get_session() as session:
            row = session.exec(select(UserSubscription).where(
                UserSubscription.stripe_subscription_id == sub_id)).first()
        if row:
            if etype == "customer.subscription.deleted" or status in ("canceled", "unpaid"):
                set_plan(row.user_id, PlanTier.FREE,
                         stripe_subscription_id=sub_id)
            else:
                set_plan(row.user_id, PlanTier.PRO,
                         stripe_subscription_id=sub_id,
                         current_period_end=_period_end(obj))
        else:
            log.info("Billing webhook: %s for unknown subscription %s", etype, sub_id)

    else:
        log.debug("Billing webhook: ignoring event type %s", etype)
    return {"received": True, "type": etype}
