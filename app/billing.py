"""Billing — Stripe subscription checkout for the ONE paid plan (Pro,
PLAN_PRICES[PRO] per month, cancel any time), plus a manual bank-transfer
path for the pre-Stripe period.

Designed to be safe BEFORE the business entity exists:
- Until STRIPE_SECRET_KEY is set, `stripe_enabled()` is False, every user
  resolves to PRO (the pre-revenue free-for-all in server._get_user_plan),
  and the dashboard upgrade flow shows the manual payment options instead
  (PAYMENT_BANK_DETAILS / PAYMENT_CONTACT_EMAIL) — activation is manual via
  the admin set-plan endpoint.
- Once the LLC + Stripe account exist, setting STRIPE_SECRET_KEY,
  STRIPE_PRICE_ID_PRO (a monthly recurring Price for PLAN_PRICES[PRO]) and
  STRIPE_WEBHOOK_SECRET turns on real checkout + webhook-driven plan sync.
  No code change needed. Test-mode keys (sk_test_/price_ from a test-mode
  Price) exercise the whole flow without charging anyone.
- Existing users are NOT dropped when that happens: server._is_grandfathered
  keeps everyone with a profile on PRO until PLAN_GRANDFATHER_UNTIL names
  the go-live date, after which only earlier signups keep it.

Lifecycle, as Stripe drives it (handle_webhook):
  checkout.session.completed            -> PRO (customer + subscription ids stored)
  customer.subscription.updated         -> PRO with the new current_period_end
                                           (renewal; also past_due during dunning —
                                           the user keeps access while Stripe
                                           retries the card)
  ... status canceled/unpaid, or
  customer.subscription.deleted         -> FREE (a portal cancellation lands
                                           here at period end; "cancel any time"
                                           is the Stripe Customer Portal,
                                           create_portal_session)
Entitlement is then server._get_user_plan: a PRO row is PRO until
current_period_end + 3 days grace.

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
from app.db.models import PLAN_PRICES, PlanTier, UserSubscription

log = logging.getLogger(__name__)


def stripe_enabled() -> bool:
    return bool(settings.stripe_secret_key and settings.stripe_price_id_pro)


def pro_price_usd() -> int:
    """The one number every surface states: Pro's monthly price."""
    return int(PLAN_PRICES[PlanTier.PRO])


def payment_options() -> dict:
    """What the UI shows on the upgrade screen. Never includes secrets."""
    return {
        "price_monthly_usd": pro_price_usd(),
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


def create_portal_session(user_id: str, base_url: str) -> str:
    """Stripe Customer Portal URL for this user's subscription — where they
    update the card, download invoices, or CANCEL. Nothing here cancels on
    the user's behalf: Stripe does it and reports back through the webhook.
    Raises LookupError when the user has no Stripe customer (bank-transfer
    activations and grandfathered users have nothing to manage there)."""
    if not stripe_enabled():
        raise RuntimeError("Stripe is not configured")
    with get_session() as session:
        row = session.exec(
            select(UserSubscription).where(UserSubscription.user_id == user_id)
        ).first()
        customer = row.stripe_customer_id if row else None
    if not customer:
        raise LookupError("no Stripe customer for this user")
    stripe = _stripe()
    portal = stripe.billing_portal.Session.create(
        customer=customer,
        return_url=f"{base_url}/dashboard?billing=portal",
    )
    return portal.url


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


def _field(obj, key, default=None):
    """Read one field out of a Stripe webhook payload object.

    `event["data"]["object"]` is a TYPED StripeObject in production —
    `checkout.Session`, `Subscription` — and since stripe-python 12 those are
    no longer dict subclasses. `.get()` on one raises

        AttributeError: 'get' is a dict method, but a Session is not a dict.

    so EVERY real `checkout.session.completed` returned HTTP 500 and Stripe
    kept retrying a webhook that could never succeed (production, 2026-09-04
    16:35 UTC, evt_1UC054…). The suite stayed green throughout because its
    stripe double returned `json.loads(payload)` — a plain dict, on which
    `.get()` works. Subscripting is the one access that behaves identically on
    both, so every payload read goes through here and the test double is now
    the real SDK object (tests/test_billing.py).
    """
    try:
        value = obj[key]
    except (KeyError, IndexError, AttributeError, TypeError):
        return default
    return default if value is None else value


def _period_end(sub) -> Optional[datetime]:
    """When the paid period runs out — the value `_get_user_plan` expires on.

    Read from the subscription's top level, then from its first item: Stripe
    moved `current_period_end` onto the subscription ITEMS in API version
    2025-03-31.basil, and this account is on 2026-08-26.dahlia, so the
    top-level field is simply absent and every renewal would store no expiry
    at all.
    """
    ts = _field(sub, "current_period_end")
    if ts is None:
        data = _field(sub, "items", {})
        data = _field(data, "data", []) or []
        if data:
            ts = _field(data[0], "current_period_end")
    return datetime.utcfromtimestamp(int(ts)) if ts else None


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

    etype = _field(event, "type")
    obj = _field(event, "data", {})
    obj = _field(obj, "object", {})

    if etype == "checkout.session.completed":
        user_id = _field(obj, "client_reference_id")
        if user_id:
            set_plan(user_id, PlanTier.PRO,
                     stripe_customer_id=_field(obj, "customer"),
                     stripe_subscription_id=_field(obj, "subscription"))
        else:
            log.warning("Billing webhook: checkout completed without client_reference_id")

    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = _field(obj, "id")
        status = _field(obj, "status")
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
