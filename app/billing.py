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
    """What the UI shows on the upgrade screen. Never includes secrets.

    Carries the temporary-Pro state so every client — web, the Plans modal, the
    mobile app, the extension — reads ONE answer about whether there is anything
    to buy. A surface that decided for itself is how a "Start Pro — $100/mo"
    button survives on a page whose features are free that day.
    """
    temp = temporary_pro_status()
    buyable = purchase_available()
    return {
        "price_monthly_usd": pro_price_usd(),
        "stripe_enabled": stripe_enabled(),
        "stripe_live": stripe_live_mode(),
        # Paid paths are OFF while temporary Pro is on — including the manual
        # bank transfer, which is still someone sending us money for nothing.
        "purchase_available": buyable,
        "bank_transfer": bool(settings.payment_bank_details.strip()) and buyable,
        "bank_details": (settings.payment_bank_details.strip() or None) if buyable else None,
        "contact_email": settings.payment_contact_email.strip() or None,
        "temporary_pro": temp,
        # There is no priced recruiter-research product. This says so in the one
        # payload every upgrade surface already reads, so no page can invent one.
        "recruiter_research": {"available": False, "for_sale": False,
                               "status": RECRUITER_RESEARCH_STATUS},
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


#: The one sentence every surface shows while temporary Pro is on. No end date:
#: inventing one ("free until October 31") is a promise nobody has made, and a
#: date that slips is worse than no date at all.
TEMPORARY_PRO_NOTICE = "Pro features are temporarily available to everyone."

#: What to say instead of selling a recruiter-research add-on. Phase 7's wording.
RECRUITER_RESEARCH_STATUS = "On hold while we improve matching quality"


def temporary_pro_active() -> bool:
    """Is every legitimate tenant currently riding on PRO limits?

    ONE switch (`TEMPORARY_PRO_FOR_ALL`), read through ONE function, so there is
    no second entitlement check to drift from this one. It answers a question
    about the PLAN only. `is_paid_entitlement` below is untouched by it on
    purpose — see its docstring: conflating the two is exactly how 10 dormant
    free riders came to spend 90.8% of a week's LLM budget at the PRO ceiling.
    """
    return bool(settings.temporary_pro_for_all)


def purchase_available() -> bool:
    """May we take money for Pro right now?

    False while temporary Pro is on. Asking someone to pay $100/month to unlock
    features they already have is not an upsell, it is a false sale, and hiding
    the button is not enough — `billing_checkout` refuses on this too, so a
    direct POST cannot open a Checkout session either.
    """
    return stripe_enabled() and not temporary_pro_active()


def temporary_pro_status() -> dict:
    """The banner state for every client surface. Never implies payment."""
    active = temporary_pro_active()
    return {
        "active": active,
        "notice": TEMPORARY_PRO_NOTICE if active else "",
        # Spelled out because it is the invariant, not an implementation detail:
        # temporary access never marks anyone as a paying subscriber.
        "marks_user_as_paying": False,
        "ends_at": None,                 # deliberately unknown
        "auto_enrolls_on_end": False,    # nothing charges anyone when it stops
    }


def is_paid_entitlement(row, now: Optional[datetime] = None) -> bool:
    """Is money actually changing hands for this subscription row?

    Stricter than "plan != FREE", which server._get_user_plan answers and
    which is also True for two complimentary cases this returns False for:
      - no row at all (a grandfathered PRO is a free ride — the dormancy gate
        applies to them; 10 dormant, row-less users were scored every day at
        the PRO ceiling because they read as paying);
      - a Stripe-backed row while the keys are TEST mode (a sandbox
        subscription — the only subscription production had, reported as $100
        MRR by /api/admin/metrics).
    A row WITHOUT Stripe ids is a manual activation (bank transfer / admin
    set-plan) and counts as paid: someone paid outside Stripe and an operator
    wrote the row. A row WITH Stripe ids counts only under `sk_live_`.
    An expired period (past ENTITLEMENT_GRACE_DAYS) is never paid.

    `temporary_pro_active()` is NOT consulted here and must never be: temporary
    access grants the PLAN, not revenue. If this function ever learned about it,
    every dormant account would read as a paying customer, the dormancy gate
    would stop applying to anyone, and the MRR figure would be the count of our
    users (guard: `test_temporary_pro`).
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
    return stripe_live_mode()


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
    if not customer:
        raise LookupError("no Stripe customer for this user")
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
             last_event_at: Optional[datetime] = None) -> None:
    """Idempotent upsert of a user's subscription row.

    Every optional field follows one rule: omit it and the stored value is
    kept, pass it (including ``None``) and it is written. Only a caller that
    actually knows the period end may change it.
    """
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
        if current_period_end is not _UNSET:
            row.current_period_end = current_period_end
        if last_event_at is not None:
            row.last_event_at = last_event_at
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


def _event_created(event) -> Optional[datetime]:
    """Stripe's own timestamp for the event — the only clock that orders
    deliveries, since arrival order does not."""
    ts = _field(event, "created")
    try:
        return datetime.utcfromtimestamp(int(ts)) if ts else None
    except (TypeError, ValueError):
        return None


def _claim_event(event_id: str, event_type: str) -> bool:
    """Record this event id, returning False if it was already recorded.

    The unique constraint is what makes the claim atomic: two workers handed
    the same redelivery race to INSERT and exactly one wins. On any database
    error this returns True — processing a duplicate is recoverable, refusing
    to process a real event is a user who paid and did not get their plan.
    """
    from app.db.models import BillingEvent
    try:
        with get_session() as session:
            seen = session.exec(select(BillingEvent).where(
                BillingEvent.event_id == event_id)).first()
            if seen:
                return False
            session.add(BillingEvent(event_id=event_id, event_type=event_type or ""))
            session.commit()
        return True
    except Exception as e:
        from sqlalchemy.exc import IntegrityError
        if isinstance(e, IntegrityError):
            return False        # lost the race — the winner is applying it
        log.warning("Billing webhook: could not record event %s (%s) — "
                    "processing anyway", event_id, e)
        return True


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
    event_id = _field(event, "id")
    created = _event_created(event)
    obj = _field(event, "data", {})
    obj = _field(obj, "object", {})

    # Stripe delivers AT LEAST ONCE — it retries every non-2xx and replays on
    # request — so the same event can arrive twice. Applying
    # `checkout.session.completed` a second time re-granted PRO to a user who
    # had cancelled in between. Claim the id first; losing the claim means
    # someone already applied this event.
    if event_id and not _claim_event(event_id, etype):
        log.info("Billing webhook: %s (%s) already applied — ignoring replay",
                 etype, event_id)
        return {"received": True, "type": etype, "duplicate": True}

    if etype == "checkout.session.completed":
        user_id = _field(obj, "client_reference_id")
        sub_id = _field(obj, "subscription")
        if user_id:
            # No current_period_end here on purpose: a checkout session does not
            # carry one, and passing None would ERASE the real period end that
            # customer.subscription.updated stored. Whichever of the two lands
            # last, the stored expiry is now the one that came from the
            # subscription.
            set_plan(user_id, PlanTier.PRO,
                     stripe_customer_id=_field(obj, "customer"),
                     stripe_subscription_id=sub_id,
                     last_event_at=created)
            # Then go and GET the period end, best effort. The founder's row sat
            # on PRO with a NULL period end for two weeks because this event
            # was the only one the endpoint received (0 subscription events
            # delivered that week) and NULL reads as "never expires". A failure
            # here is logged and the webhook still returns 2xx — Stripe must not
            # retry a checkout we have already applied.
            if sub_id:
                try:
                    _apply_subscription(user_id, sub_id,
                                        _retrieve_subscription(sub_id))
                except Exception as e:
                    log.warning("Billing webhook: checkout for %s applied, but "
                                "could not retrieve subscription %s for its "
                                "period end (%s) — reconcile will fill it",
                                user_id, sub_id, e)
        else:
            log.warning("Billing webhook: checkout completed without client_reference_id")

    elif etype in ("customer.subscription.created", "customer.subscription.updated",
                   "customer.subscription.deleted"):
        sub_id = _field(obj, "id")
        status = _field(obj, "status")
        row = _row_for_subscription(sub_id)
        if row:
            if _is_stale(row, created):
                log.info("Billing webhook: %s for %s is older than the last "
                         "applied event — ignoring", etype, sub_id)
                return {"received": True, "type": etype, "stale": True}
            if etype == "customer.subscription.deleted":
                status = "canceled"
            _apply_subscription(row.user_id, sub_id, obj, status=status,
                                last_event_at=created)
        else:
            log.info("Billing webhook: %s for unknown subscription %s", etype, sub_id)

    elif etype in ("invoice.paid", "invoice.payment_succeeded"):
        # A renewal charge. The endpoint production ran on was never subscribed
        # to invoice events, so renewals reached us only through
        # customer.subscription.updated — which also never arrived. Either one
        # is enough now: the invoice names the subscription, the subscription
        # carries the new period end.
        sub_id = _invoice_subscription_id(obj)
        row = _row_for_subscription(sub_id) if sub_id else None
        if row is None:
            log.info("Billing webhook: %s for unknown subscription %s", etype, sub_id)
        elif _is_stale(row, created):
            log.info("Billing webhook: %s for %s is older than the last "
                     "applied event — ignoring", etype, sub_id)
            return {"received": True, "type": etype, "stale": True}
        else:
            try:
                sub = _retrieve_subscription(sub_id)
            except Exception as e:
                # The invoice's own line period is the fallback: less precise
                # (one line, not the subscription), but it beats leaving a
                # paid renewal with a stale or NULL period end.
                log.warning("Billing webhook: %s — could not retrieve %s (%s); "
                            "using the invoice line period", etype, sub_id, e)
                sub = {"status": "active",
                       "current_period_end": _invoice_period_end(obj)}
            _apply_subscription(row.user_id, sub_id, sub, last_event_at=created)

    elif etype == "invoice.payment_failed":
        # Dunning. Stripe retries the card on its own schedule and tells us via
        # customer.subscription.updated (past_due keeps access, unpaid ends
        # it). Nothing of ours decides the retry policy — log and move on.
        log.info("Billing webhook: payment failed for subscription %s — Stripe "
                 "is retrying; access continues until it reports unpaid",
                 _invoice_subscription_id(obj))

    else:
        log.debug("Billing webhook: ignoring event type %s", etype)
    return {"received": True, "type": etype}


# ── what Stripe says a subscription is, applied to our row ───────────────────

def _row_for_subscription(sub_id) -> Optional[UserSubscription]:
    if not sub_id:
        return None
    with get_session() as session:
        return session.exec(select(UserSubscription).where(
            UserSubscription.stripe_subscription_id == sub_id)).first()


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
                        last_event_at: Optional[datetime] = None) -> PlanTier:
    """Write what a Subscription object says onto the user's row.

    Dead statuses -> FREE with the period end cleared (a downgrade leaves no
    stale expiry). Anything else -> PRO with the subscription's period end; a
    payload that carries NO period end (a hand-built `past_due` update, an
    incomplete object) keeps the stored one rather than wiping it — the same
    `_UNSET` rule set_plan already applies to the checkout event.
    """
    status = status or _field(sub, "status")
    if status in _DEAD_STATUSES:
        set_plan(user_id, PlanTier.FREE, stripe_subscription_id=sub_id,
                 current_period_end=None, last_event_at=last_event_at)
        return PlanTier.FREE
    end = _period_end(sub)
    set_plan(user_id, PlanTier.PRO, stripe_subscription_id=sub_id,
             current_period_end=end if end is not None else _UNSET,
             last_event_at=last_event_at)
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

    Targets: Stripe-backed rows whose `current_period_end` is NULL (PRO forever
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
