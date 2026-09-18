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
  checkout.session.completed            -> PRO (customer + subscription ids stored),
                                           then the subscription is RETRIEVED so
                                           the period end lands with it (below)
  customer.subscription.created/updated -> PRO with the new current_period_end
                                           (renewal; also past_due during dunning —
                                           the user keeps access while Stripe
                                           retries the card)
  invoice.paid / invoice.payment_succeeded
                                        -> PRO with the period end of the
                                           subscription the invoice paid for
  invoice.payment_failed                -> logged only (dunning keeps access)
  ... status canceled/unpaid, or
  customer.subscription.deleted         -> FREE (a portal cancellation lands
                                           here at period end; "cancel any time"
                                           is the Stripe Customer Portal,
                                           create_portal_session)
Entitlement is then server._get_user_plan: a PRO row is PRO until
current_period_end + ENTITLEMENT_GRACE_DAYS.

THE STRIPE ENDPOINT MUST BE SUBSCRIBED TO (Dashboard -> Developers -> Webhooks):
    checkout.session.completed
    customer.subscription.created
    customer.subscription.updated
    customer.subscription.deleted
    invoice.paid
Production (2026-09-16) subscribed only to checkout.session.completed and
customer.subscription.updated/deleted, and no invoice event at all. The founder's
PRO row was written by checkout.session.completed and no subscription event ever
followed (0 webhook deliveries that week), so `current_period_end` stayed NULL —
which entitlement reads as "never expires" — while Stripe showed a next billing
date. Three things now close that gap: checkout retrieves the subscription for
its period end, invoice.paid is handled, and `reconcile_subscriptions` (run
daily by server._billing_maintenance) re-reads any Stripe-backed row whose
period end is missing or long past.

TEST MODE IS NOT REVENUE. `sk_test_` keys make `stripe_enabled()` True — the
whole flow works, nobody is charged — so a sandbox subscription used to count as
a paid one everywhere: /api/admin/metrics reported the founder's sandbox
subscription as $100 MRR, and the dormancy gate read "plan != FREE" as "paying"
(server._user_paid_search_is_live), which is how 10 users with no sign-in in 7
days (three last seen in June) kept scoring at the 250-finals/day PRO ceiling and
took 90.8% of the week's LLM spend. `stripe_live_mode()` and
`is_paid_entitlement()` are the two predicates that tell revenue from rehearsal.

The `stripe` package is imported lazily so the app boots even when the
dependency isn't installed (e.g. a slim deployment that never enables it).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import PLAN_PRICES, PlanTier, UserSubscription

log = logging.getLogger(__name__)

#: How long a PRO row stays PRO past its `current_period_end`. Covers the gap
#: between Stripe's renewal charge and the webhook that reports it (and a card
#: retry or two). server._get_user_plan and is_paid_entitlement read the same
#: number so "entitled" and "paid" cannot drift apart at the boundary.
ENTITLEMENT_GRACE_DAYS = 3

#: Subscription statuses that mean Stripe has given up on collecting: the row
#: goes to FREE. `past_due` is NOT here — that is dunning, and the user keeps
#: access while Stripe retries the card. `incomplete_expired` is the initial
#: payment that never went through (Stripe closes it after 23h); granting PRO
#: on it would be a free plan for a card that was never charged.
_DEAD_STATUSES = ("canceled", "unpaid", "incomplete_expired")


def stripe_enabled() -> bool:
    return bool(settings.stripe_secret_key and settings.stripe_price_id_pro)


def stripe_live_mode() -> bool:
    """True only when the configured key can actually move money.

    `stripe_enabled()` is deliberately True for `sk_test_` keys so the whole
    checkout/webhook flow can be exercised before go-live — but that makes it
    the wrong question for anything about REVENUE. Production ran on test keys
    (the billing portal opened under "Spotapply llc sandbox"; live payments were
    never activated) and every "is this user paying?" check said yes.
    """
    return stripe_enabled() and settings.stripe_secret_key.startswith("sk_live_")


def stripe_mode() -> str:
    """'live' | 'test' | 'off' — for the admin KPIs, never the key itself."""
    if not stripe_enabled():
        return "off"
    return "live" if stripe_live_mode() else "test"


def pro_price_usd() -> int:
    """The one number every surface states: Pro's monthly price."""
    return int(PLAN_PRICES[PlanTier.PRO])


def payment_options() -> dict:
    """What the UI shows on the upgrade screen. Never includes secrets."""
    return {
        "price_monthly_usd": pro_price_usd(),
        "stripe_enabled": stripe_enabled(),
        "stripe_live": stripe_live_mode(),
        "bank_transfer": bool(settings.payment_bank_details.strip()),
        "bank_details": settings.payment_bank_details.strip() or None,
        "contact_email": settings.payment_contact_email.strip() or None,
    }


def entitlement_expired(row, now: Optional[datetime] = None) -> bool:
    """True once `current_period_end` + grace is behind us. NULL never expires
    (the founder's row — see the header — is exactly why that is dangerous, and
    why reconcile_subscriptions exists to fill it)."""
    end = getattr(row, "current_period_end", None) if row is not None else None
    if end is None:
        return False
    if end.tzinfo is not None:
        from datetime import timezone
        end = end.astimezone(timezone.utc).replace(tzinfo=None)
    return end + timedelta(days=ENTITLEMENT_GRACE_DAYS) < (now or datetime.utcnow())


def is_paid_entitlement(row, now: Optional[datetime] = None) -> bool:
    """Is money actually changing hands for this subscription row?

    Stricter than "plan != FREE", which server._get_user_plan answers and
    which is also True for two complimentary cases this returns False for:
      - no row at all (a grandfathered PRO is a free ride — the dormancy gate
        applies to them; 10 dormant, row-less users were scored every day at
        the PRO ceiling because they read as paying);
      - a Stripe-backed row whose object is TEST mode or not yet verified (a sandbox
        subscription — the only subscription production had, reported as $100
        MRR by /api/admin/metrics).
    A row WITHOUT Stripe ids is a manual activation (bank transfer / admin
    set-plan) and counts as paid: someone paid outside Stripe and an operator
    wrote the row. A row WITH Stripe ids counts only when Stripe confirmed
    `livemode=True` for that object. Deployment keys cannot prove its mode:
    switching to live keys must not relabel old sandbox subscriptions as paid.
    An expired period (past ENTITLEMENT_GRACE_DAYS) is never paid.
    """
    if row is None:
        return False
    plan = getattr(row, "plan", None)
    if not plan or plan == PlanTier.FREE:
        return False
    if entitlement_expired(row, now):
        return False
    stripe_backed = bool(getattr(row, "stripe_subscription_id", None)
                         or getattr(row, "stripe_customer_id", None))
    if not stripe_backed:
        return True
    return getattr(row, "stripe_livemode", None) is True


_TEST_MODE_WARNED = [False]


def warn_if_stripe_test_mode() -> bool:
    """Log, once per process, that the deployment cannot collect money.

    Only when it matters: a hosted (Supabase) deployment with Stripe configured
    on test keys. Local dev and pre-revenue (no keys) are silent. Returns True
    when the warning was written — server._billing_maintenance calls this on
    its first pass so the line lands in the boot log where the founder reads
    it, not only in a metrics field nobody opens.
    """
    if _TEST_MODE_WARNED[0]:
        return False
    if not (settings.use_supabase and stripe_enabled() and not stripe_live_mode()):
        return False
    _TEST_MODE_WARNED[0] = True
    log.warning(
        "Stripe is in TEST mode — checkout cannot collect money and sandbox "
        "subscriptions are not revenue. Set STRIPE_SECRET_KEY to the sk_live_ "
        "key (and STRIPE_PRICE_ID_PRO / STRIPE_WEBHOOK_SECRET to their live "
        "counterparts) to go live.")
    return True


def _stripe():
    import stripe  # lazy: optional dependency until payments launch
    stripe.api_key = settings.stripe_secret_key
    return stripe


class AlreadySubscribed(Exception):
    """This user already has a live Stripe subscription — send them to the
    portal, do not sell them a second one."""


def _checkout_idempotency_key(user_id: str) -> str:
    """Collapse repeat checkout POSTs from one user into ONE Stripe session.

    Two clicks, a double-submit, or a retry after a slow response each used to
    create a separate subscription session. Stripe honours an idempotency key
    for 24h, so bucketing by a 15-minute window means a burst returns the SAME
    session while a genuine retry tomorrow gets a fresh one.
    """
    import time as _t
    return f"spotapply:checkout:{user_id}:{int(_t.time() // 900)}"


def create_checkout_session(user_id: str, email: Optional[str], base_url: str) -> str:
    """Create a Stripe Checkout session for the Pro subscription; returns its URL.

    Raises ``AlreadySubscribed`` when the user is already paying. Without that
    check a second checkout created a SECOND subscription, and because
    ``set_plan`` keeps exactly one (customer, subscription) pair, the webhook
    for the new one overwrote the old — leaving the first subscription billing
    the card while being invisible to ``create_portal_session``, so the user
    could not even find it to cancel it.
    """
    if not stripe_enabled():
        raise RuntimeError("Stripe is not configured")
    with get_session() as session:
        row = session.exec(
            select(UserSubscription).where(UserSubscription.user_id == user_id)
        ).first()
        plan = row.plan if row else None
        customer = row.stripe_customer_id if row else None
        subscription = row.stripe_subscription_id if row else None
        object_live = row.stripe_livemode if row else None
    if object_live is False and stripe_live_mode():
        # Test customers/subscriptions do not exist in the live account.
        # Allow a real purchase without reusing those sandbox ids.
        customer = subscription = None
    if subscription and plan and plan != PlanTier.FREE:
        raise AlreadySubscribed(
            f"user {user_id} already has subscription {subscription}")

    stripe = _stripe()
    kwargs = dict(
        mode="subscription",
        line_items=[{"price": settings.stripe_price_id_pro, "quantity": 1}],
        success_url=f"{base_url}/dashboard?billing=success",
        cancel_url=f"{base_url}/pricing",
        client_reference_id=user_id,
        allow_promotion_codes=True,
    )
    # Reuse the Stripe customer we already know about, so a returning user
    # (lapsed, then upgrading again) keeps one customer record with one card
    # and one invoice history. Stripe rejects `customer` and `customer_email`
    # together, so it is one or the other.
    if customer:
        kwargs["customer"] = customer
    elif email:
        kwargs["customer_email"] = email
    session = stripe.checkout.Session.create(
        **kwargs, idempotency_key=_checkout_idempotency_key(user_id))
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
        object_live = row.stripe_livemode if row else None
    if not customer:
        raise LookupError("no Stripe customer for this user")
    if object_live is not None and object_live != stripe_live_mode():
        raise LookupError("Stripe customer belongs to a different billing mode")
    stripe = _stripe()
    portal = stripe.billing_portal.Session.create(
        customer=customer,
        return_url=f"{base_url}/dashboard?billing=portal",
    )
    return portal.url


#: "the caller said nothing about this field", which is NOT the same as "set it
#: to None". `current_period_end` used to be assigned unconditionally, so
#: `checkout.session.completed` — which knows no period end — wiped the real one
#: to NULL, and entitlement reads NULL as "never expires"
#: (server._get_user_plan). A user could end up on PRO forever from ordinary
#: webhook ordering, with no replay involved.
_UNSET = object()


def set_plan(user_id: str, plan: PlanTier,
             stripe_customer_id: Optional[str] = None,
             stripe_subscription_id: Optional[str] = None,
             current_period_end=_UNSET,
             last_event_at: Optional[datetime] = None,
             stripe_livemode=_UNSET, *, session=None) -> None:
    """Idempotent upsert of a user's subscription row.

    Every optional field follows one rule: omit it and the stored value is
    kept, pass it (including ``None``) and it is written. Only a caller that
    actually knows the period end may change it.
    """
    if session is None:
        with get_session() as owned:
            set_plan(user_id, plan, stripe_customer_id, stripe_subscription_id,
                     current_period_end, last_event_at, stripe_livemode, session=owned)
            owned.commit()
        return
    row = session.exec(
        select(UserSubscription).where(UserSubscription.user_id == user_id)
        .with_for_update()
    ).first()
    if row is None:
        row = UserSubscription(user_id=user_id)
    elif _is_stale(row, last_event_at):
        return
    row.plan = plan
    if stripe_customer_id is not None:
        row.stripe_customer_id = stripe_customer_id
    if stripe_subscription_id is not None:
        row.stripe_subscription_id = stripe_subscription_id
    if current_period_end is not _UNSET:
        row.current_period_end = current_period_end
    if stripe_livemode is not _UNSET:
        row.stripe_livemode = stripe_livemode
    if last_event_at is not None:
        row.last_event_at = last_event_at
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.flush()
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


def _event_created(event) -> Optional[datetime]:
    """Stripe's own timestamp for the event — the only clock that orders
    deliveries, since arrival order does not."""
    ts = _field(event, "created")
    try:
        return datetime.utcfromtimestamp(int(ts)) if ts else None
    except (TypeError, ValueError):
        return None


def _claim_event(event_id: str, event_type: str, session) -> bool:
    """Claim in the SAME transaction as the entitlement update.

    An error or process death rolls back both, leaving Stripe's retry usable.
    The unique constraint serializes concurrent deliveries of the same event.
    Database failures must propagate as non-2xx, never consume a real payment.
    """
    from app.db.models import BillingEvent
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    result = session.exec(insert(BillingEvent).values(
        event_id=event_id, event_type=event_type or ""
    ).on_conflict_do_nothing(index_elements=["event_id"]))
    return bool(result.rowcount)


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

    # Network reads happen before the database transaction. Once it starts,
    # the event claim and every plan write commit (or roll back) together.
    etype = _field(event, "type")
    obj = _field(_field(event, "data", {}), "object", {})
    sub_id = (_field(obj, "subscription") if etype == "checkout.session.completed"
              else _invoice_subscription_id(obj) if etype in
              ("invoice.paid", "invoice.payment_succeeded") else None)
    if etype in ("invoice.paid", "invoice.payment_succeeded") and sub_id:
        if _row_for_subscription(sub_id) is None:
            sub_id = None  # no tenant to update; do not buy a Stripe read
    subscription = None
    if sub_id:
        try:
            subscription = _retrieve_subscription(sub_id)
        except Exception as e:
            log.warning("Billing webhook: %s could not retrieve subscription %s (%s)",
                        etype, sub_id, type(e).__name__)
            if etype in ("invoice.paid", "invoice.payment_succeeded"):
                end = _invoice_period_end(obj)
                if end is None:
                    # No authoritative period: let Stripe retry. Do not grant
                    # an open-ended paid plan or acknowledge a lost renewal.
                    raise
                subscription = {"status": "active", "current_period_end": end}

    with get_session() as session:
        event_id = _field(event, "id")
        if event_id and not _claim_event(event_id, etype, session):
            return {"received": True, "type": etype, "duplicate": True}
        out = _apply_webhook_event(event, session, subscription)
        session.commit()
        return out


def _apply_webhook_event(event, session, subscription=None) -> dict:
    etype = _field(event, "type")
    created = _event_created(event)
    obj = _field(_field(event, "data", {}), "object", {})
    livemode = _field(event, "livemode", _UNSET)
    if not isinstance(livemode, bool):
        livemode = _UNSET
    result = {"received": True, "type": etype}

    if etype == "checkout.session.completed":
        user_id = _field(obj, "client_reference_id")
        sub_id = _field(obj, "subscription")
        if user_id:
            row = session.exec(select(UserSubscription).where(
                UserSubscription.user_id == user_id).with_for_update()).first()
            if row and _is_stale(row, created):
                return {**result, "stale": True}
            set_plan(user_id, PlanTier.PRO,
                     stripe_customer_id=_field(obj, "customer"),
                     stripe_subscription_id=sub_id, last_event_at=created,
                     stripe_livemode=livemode, session=session)
            if subscription is not None:
                # A retrieved cancellation must win over an old checkout.
                # Database errors here roll back the claim as well.
                _apply_subscription(user_id, sub_id, subscription,
                                    last_event_at=created, session=session)
        else:
            log.warning("Billing webhook: checkout completed without client_reference_id")

    elif etype in ("customer.subscription.created", "customer.subscription.updated",
                   "customer.subscription.deleted", "invoice.paid", "invoice.payment_succeeded"):
        invoice = etype in ("invoice.paid", "invoice.payment_succeeded")
        sub_id = _invoice_subscription_id(obj) if invoice else _field(obj, "id")
        row = _row_for_subscription(sub_id, session=session)
        if row is None:
            log.info("Billing webhook: %s for unknown subscription %s", etype, sub_id)
        elif _is_stale(row, created):
            return {**result, "stale": True}
        else:
            if invoice and subscription is None:
                raise RuntimeError("Subscription appeared during invoice processing; retry")
            status = "canceled" if etype == "customer.subscription.deleted" else None
            _apply_subscription(row.user_id, sub_id, subscription if invoice else obj,
                                status=status, last_event_at=created,
                                stripe_livemode=livemode, session=session)

    elif etype == "invoice.payment_failed":
        log.info("Billing webhook: payment failed for subscription %s — Stripe "
                 "is retrying; access continues until it reports unpaid",
                 _invoice_subscription_id(obj))
    else:
        log.debug("Billing webhook: ignoring event type %s", etype)
    return result


# ── what Stripe says a subscription is, applied to our row ───────────────────

def _row_for_subscription(sub_id, *, session=None) -> Optional[UserSubscription]:
    if not sub_id:
        return None
    if session is None:
        with get_session() as owned:
            return _row_for_subscription(sub_id, session=owned)
    return session.exec(select(UserSubscription).where(
        UserSubscription.stripe_subscription_id == sub_id).with_for_update()).first()


def _is_stale(row, created: Optional[datetime]) -> bool:
    """Webhooks are not ordered. A dunning `unpaid` overtaken by the recovery
    that followed it would otherwise arrive last and cut off someone who is
    paying again. Entitlement only ever moves forward in Stripe's own clock."""
    return bool(created and row.last_event_at and created < row.last_event_at)


def _retrieve_subscription(sub_id: str):
    """GET the subscription from Stripe. Raises on any failure — callers decide
    whether that is fatal (reconcile: count it) or not (webhook: log it)."""
    return _stripe().Subscription.retrieve(sub_id)


def _apply_subscription(user_id: str, sub_id: str, sub, status=None,
                        last_event_at: Optional[datetime] = None,
                        stripe_livemode=_UNSET, *, session=None) -> PlanTier:
    """Write what a Subscription object says onto the user's row.

    Dead statuses -> FREE with the period end cleared (a downgrade leaves no
    stale expiry). Anything else -> PRO with the subscription's period end; a
    payload that carries NO period end (a hand-built `past_due` update, an
    incomplete object) keeps the stored one rather than wiping it — the same
    `_UNSET` rule set_plan already applies to the checkout event.
    """
    status = status or _field(sub, "status")
    mode = _field(sub, "livemode", stripe_livemode)
    if not isinstance(mode, bool):
        mode = _UNSET
    if status in _DEAD_STATUSES:
        set_plan(user_id, PlanTier.FREE, stripe_subscription_id=sub_id,
                 current_period_end=None, last_event_at=last_event_at,
                 stripe_livemode=mode, session=session)
        return PlanTier.FREE
    end = _period_end(sub)
    set_plan(user_id, PlanTier.PRO, stripe_subscription_id=sub_id,
             current_period_end=end if end is not None else _UNSET,
             last_event_at=last_event_at, stripe_livemode=mode, session=session)
    return PlanTier.PRO


def _invoice_subscription_id(invoice) -> Optional[str]:
    """The subscription an invoice bills. Top-level `subscription` on older API
    versions; `parent.subscription_details.subscription` from 2025-03-31.basil
    on (this account is on 2026-08-26.dahlia). Either may be an id string or
    an expanded object."""
    ref = _field(invoice, "subscription")
    if ref is None:
        parent = _field(invoice, "parent", {})
        details = _field(parent, "subscription_details", {})
        ref = _field(details, "subscription")
    if ref is not None and not isinstance(ref, str):
        ref = _field(ref, "id")
    return ref or None


def _invoice_period_end(invoice) -> Optional[int]:
    """`lines.data[0].period.end` — the fallback when the subscription cannot
    be retrieved. Returns the raw epoch so `_period_end` can read it."""
    lines = _field(invoice, "lines", {})
    data = _field(lines, "data", []) or []
    if not data:
        return None
    period = _field(data[0], "period", {})
    ts = _field(period, "end")
    try:
        return int(ts) if ts else None
    except (TypeError, ValueError):
        return None


def reconcile_subscriptions(limit: int = 50) -> dict:
    """Re-read Stripe for the rows the webhook stream let drift. Never raises.

    Targets: Stripe-backed rows with unverified `stripe_livemode`, whose
    `current_period_end` is NULL (PRO forever
    as far as entitlement can tell — the founder's row), and non-FREE rows
    whose period end is more than ENTITLEMENT_GRACE_DAYS past (already
    downgraded by _get_user_plan, but the row still says PRO and the KPIs still
    count it). Each is retrieved and applied through the same
    `_apply_subscription` the webhooks use: dead -> FREE, live -> PRO + the
    real period end. Non-FREE rows go first, then FREE rows oldest-checked
    first (set_plan bumps updated_at, so the sweep rotates); `limit` bounds the
    Stripe calls per run. A FREE row with a Stripe id is polled too because it
    is the one case a MISSED recovery webhook would leave a paying user on
    FREE, and this endpoint has already missed a week of deliveries once.
    """
    out = {"examined": 0, "pro": 0, "free": 0, "failed": 0, "skipped": False}
    if not stripe_enabled():
        out["skipped"] = True
        return out
    from sqlalchemy import and_, case, or_
    horizon = datetime.utcnow() - timedelta(days=ENTITLEMENT_GRACE_DAYS)
    try:
        with get_session() as session:
            rows = session.exec(
                select(UserSubscription)
                .where(UserSubscription.stripe_subscription_id.isnot(None))
                .where(or_(
                    UserSubscription.current_period_end.is_(None),
                    UserSubscription.stripe_livemode.is_(None),
                    and_(UserSubscription.plan != PlanTier.FREE,
                         UserSubscription.current_period_end < horizon)))
                .order_by(case((UserSubscription.plan == PlanTier.FREE, 1), else_=0),
                          UserSubscription.updated_at)
                .limit(max(1, int(limit)))
            ).all()
            targets = [(r.user_id, r.stripe_subscription_id) for r in rows]
    except Exception as e:
        log.warning("Billing reconcile: could not list subscriptions (%s)", e)
        out["failed"] += 1
        return out
    if not targets:
        return out
    try:
        stripe = _stripe()
    except Exception as e:                       # stripe package missing
        log.warning("Billing reconcile: stripe unavailable (%s)", e)
        out["failed"] += len(targets)
        return out
    for user_id, sub_id in targets:
        out["examined"] += 1
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            plan = _apply_subscription(user_id, sub_id, sub)
        except Exception as e:
            out["failed"] += 1
            log.warning("Billing reconcile: subscription %s for %s failed (%s)",
                        sub_id, user_id, e)
            continue
        out["pro" if plan == PlanTier.PRO else "free"] += 1
    if out["examined"]:
        log.info("Billing reconcile: examined %d, pro %d, free %d, failed %d",
                 out["examined"], out["pro"], out["free"], out["failed"])
    return out
