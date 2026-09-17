"""app/billing.py + plan resolution — both had zero tests, and both gate revenue.

Two things are pinned here.

The webhook. An unverified webhook is a free-upgrade vulnerability: anyone who
can POST /api/billing/webhook could hand themselves PRO. The code is written
correctly (refuses with no secret, raises on a bad signature) and these tests
keep it that way, because "we removed the verification to debug a webhook" is a
very normal Tuesday.

Plan resolution, which used to contain a cliff. `_get_user_plan` returns PRO for
everyone while Stripe is unconfigured — correct pre-revenue, since there is
nothing to buy — but no existing user has a user_subscription row, so the instant
STRIPE_SECRET_KEY and STRIPE_PRICE_ID_PRO were set every one of them silently
dropped to FREE: 50 → 15 finals/day, 12 → 5 tailors/day, unlimited → 2
autofills/week. For the 2026-09 friend beta that is the one thing that must not
happen, so the default flipped: with PLAN_GRANDFATHER_UNTIL unset, everyone who
has a profile keeps PRO without a subscription (and the app warns that the
cutoff is unset); once it names the go-live date, only earlier signups keep it.

And the price: ONE paid plan at PLAN_PRICES[PRO] = $100/month, read from that
single constant by every surface — the $10 era shipped the number in eleven
hard-coded places.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app import billing
from app.config import settings
from app.db.init_db import get_session
from app.db.models import PlanTier, UserProfile, UserSubscription

_UID = "billing-user"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            for r in s.exec(select(UserSubscription).where(
                    UserSubscription.user_id == _UID)).all():
                s.delete(r)
            for r in s.exec(select(UserProfile).where(
                    UserProfile.user_id == _UID)).all():
                s.delete(r)
            s.commit()
    _wipe()
    yield
    _wipe()


@pytest.fixture
def fake_stripe(monkeypatch):
    """A stripe module whose signature check and Subscription.retrieve we control.

    `state["subscriptions"]` maps a subscription id to the dict Stripe would
    return for it; retrieving an id that is not there raises, which is what a
    network failure or a deleted object does to the real SDK. Every retrieve is
    recorded in `state["retrieved"]`.
    """
    mod = types.ModuleType("stripe")
    mod.api_key = None
    state = {"verify": True, "event": None, "subscriptions": {}, "retrieved": []}

    class Webhook:
        @staticmethod
        def construct_event(payload, signature, secret):
            if not state["verify"]:
                raise ValueError("Invalid signature")
            return state["event"] or json.loads(payload)

    class Subscription:
        @staticmethod
        def retrieve(sub_id, **kw):
            state["retrieved"].append(sub_id)
            if sub_id not in state["subscriptions"]:
                raise LookupError(f"No such subscription: {sub_id}")
            return state["subscriptions"][sub_id]

    mod.Webhook = Webhook
    mod.Subscription = Subscription
    monkeypatch.setitem(sys.modules, "stripe", mod)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_x", raising=False)
    return state


def _plan_of(uid: str) -> PlanTier | None:
    with get_session() as s:
        row = s.exec(select(UserSubscription).where(
            UserSubscription.user_id == uid)).first()
        return row.plan if row else None


def _event(etype: str, obj: dict) -> bytes:
    return json.dumps({"type": etype, "data": {"object": obj}}).encode()


# ── webhook verification ─────────────────────────────────────────────────────

def test_webhook_refuses_when_no_secret_is_configured(fake_stripe, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", "", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        billing.handle_webhook(_event("checkout.session.completed",
                                      {"client_reference_id": _UID}), "sig")
    assert _plan_of(_UID) is None, "an unverifiable webhook granted a plan"


def test_webhook_rejects_a_bad_signature_and_grants_nothing(fake_stripe):
    fake_stripe["verify"] = False
    with pytest.raises(ValueError, match="verification failed"):
        billing.handle_webhook(_event("checkout.session.completed",
                                      {"client_reference_id": _UID}), "forged")
    assert _plan_of(_UID) is None, "a forged webhook granted PRO — free upgrades"


def test_verified_checkout_grants_pro(fake_stripe):
    billing.handle_webhook(_event("checkout.session.completed", {
        "client_reference_id": _UID, "customer": "cus_1", "subscription": "sub_1"}), "sig")
    assert _plan_of(_UID) == PlanTier.PRO


def test_checkout_without_a_user_reference_changes_no_plan(fake_stripe):
    """No client_reference_id means we do not know who paid — never guess."""
    billing.handle_webhook(_event("checkout.session.completed",
                                  {"customer": "cus_1"}), "sig")
    with get_session() as s:
        assert not s.exec(select(UserSubscription)).all() or _plan_of(_UID) is None


def test_subscription_deleted_downgrades_to_free(fake_stripe):
    billing.handle_webhook(_event("checkout.session.completed", {
        "client_reference_id": _UID, "subscription": "sub_1"}), "sig")
    assert _plan_of(_UID) == PlanTier.PRO
    billing.handle_webhook(_event("customer.subscription.deleted",
                                  {"id": "sub_1", "status": "canceled"}), "sig")
    assert _plan_of(_UID) == PlanTier.FREE


def test_an_unknown_subscription_id_is_a_no_op(fake_stripe):
    billing.handle_webhook(_event("customer.subscription.deleted",
                                  {"id": "sub_never_seen"}), "sig")
    assert _plan_of(_UID) is None


def test_an_unrelated_event_type_is_ignored(fake_stripe):
    out = billing.handle_webhook(_event("invoice.created", {"id": "in_1"}), "sig")
    assert out["received"] is True
    assert _plan_of(_UID) is None


# ── the REAL Stripe SDK, end to end ──────────────────────────────────────────
#
# Every test above this line feeds `handle_webhook` a hand-built dict through a
# fake `stripe` module. That is why the suite was green while production
# returned HTTP 500 to every `checkout.session.completed` (2026-09-04 16:35
# UTC, evt_1UC054…): the real SDK hands the handler a TYPED StripeObject, and
# `Session.get(...)` raises `AttributeError: 'get' is a dict method, but a
# Session is not a dict`. The dict double could not reproduce it.
#
# These tests use the real installed SDK with no stripe mocking at all: a real
# HMAC signature, the real `Webhook.construct_event`, and the real typed
# objects it builds. They fail on the pre-fix parsing.

_WHSEC = "whsec_regression_secret"


def _offline(*args, **kwargs):
    raise RuntimeError("no network in tests")


def _real_stripe(monkeypatch):
    """The genuine `stripe` module, with our two settings pointed at it.

    `Subscription.retrieve` is the ONE thing stubbed (to fail, like the
    blocked socket would, only without the SDK's retry loop): checkout now
    follows up with a GET for the period end, and a test that needs that GET
    to succeed replaces the stub with a typed object of its own."""
    import stripe
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_regression", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_regression", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", _WHSEC, raising=False)
    monkeypatch.setattr(stripe.Subscription, "retrieve", _offline)
    return stripe


def _signed(payload: bytes, secret: str = _WHSEC) -> str:
    """A signature Stripe's own verifier accepts — same scheme Stripe signs with."""
    import hashlib
    import hmac
    import time
    ts = int(time.time())
    mac = hmac.new(secret.encode(), b"%d.%s" % (ts, payload), hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def _deliver(event_type: str, obj: dict) -> dict:
    """Deliver one event exactly as Stripe does: signed JSON over the wire."""
    payload = _event(event_type, obj)
    return billing.handle_webhook(payload, _signed(payload))


def test_the_sdk_object_rejects_the_dict_access_that_broke_production(monkeypatch):
    """Pin the SDK behaviour itself, so this is a named fact and not folklore:
    a checkout Session answers subscripting and attributes, and refuses
    `.get()`. If a future SDK makes it a dict again, this test says so."""
    stripe = _real_stripe(monkeypatch)
    session = stripe.checkout.Session.construct_from(
        {"id": "cs_1", "object": "checkout.session", "client_reference_id": _UID},
        "sk_test_regression")
    assert not isinstance(session, dict)
    assert session["client_reference_id"] == _UID
    with pytest.raises(AttributeError, match="is a dict method"):
        session.get("client_reference_id")
    assert billing._field(session, "client_reference_id") == _UID
    assert billing._field(session, "absent", "fallback") == "fallback"


def test_a_real_signed_checkout_session_grants_pro(monkeypatch):
    """THE PRODUCTION FAILURE. Pre-fix this raised AttributeError inside
    handle_webhook — a 500 to Stripe, retried forever, user stuck on Free."""
    _real_stripe(monkeypatch)
    out = _deliver("checkout.session.completed", {
        "id": "cs_test_real", "object": "checkout.session",
        "client_reference_id": _UID, "customer": "cus_real",
        "subscription": "sub_real", "payment_status": "paid", "status": "complete"})
    assert out == {"received": True, "type": "checkout.session.completed"}
    with get_session() as s:
        row = s.exec(select(UserSubscription).where(
            UserSubscription.user_id == _UID)).first()
    assert row is not None, "the paid checkout never reached the database"
    assert row.plan == PlanTier.PRO
    assert row.stripe_customer_id == "cus_real"
    assert row.stripe_subscription_id == "sub_real"


def test_the_webhook_route_answers_a_real_signed_event_with_2xx(monkeypatch):
    """What Stripe actually measures: the HTTP status of the delivery."""
    from fastapi.testclient import TestClient
    from app.api.server import app as fastapp
    _real_stripe(monkeypatch)
    payload = _event("checkout.session.completed", {
        "id": "cs_route", "object": "checkout.session",
        "client_reference_id": _UID, "customer": "cus_route",
        "subscription": "sub_route"})
    r = TestClient(fastapp).post(
        "/api/billing/webhook", content=payload,
        headers={"stripe-signature": _signed(payload),
                 "content-type": "application/json"})
    assert r.status_code == 200, r.text
    assert r.json()["received"] is True
    assert _plan_of(_UID) == PlanTier.PRO


def test_a_real_renewal_stores_the_period_end_from_the_subscription_items(monkeypatch):
    """Stripe moved `current_period_end` onto the subscription ITEMS in API
    version 2025-03-31.basil; this account is on 2026-08-26.dahlia. Reading
    only the top level stored no expiry at all."""
    _real_stripe(monkeypatch)
    _deliver("checkout.session.completed", {
        "id": "cs_r", "object": "checkout.session", "client_reference_id": _UID,
        "customer": "cus_r", "subscription": "sub_r"})
    period_end = int((datetime.utcnow() + timedelta(days=30)).timestamp())
    _deliver("customer.subscription.updated", {
        "id": "sub_r", "object": "subscription", "status": "active",
        "items": {"object": "list", "data": [
            {"id": "si_1", "object": "subscription_item",
             "current_period_end": period_end}]}})
    with get_session() as s:
        row = s.exec(select(UserSubscription).where(
            UserSubscription.user_id == _UID)).first()
    assert row.plan == PlanTier.PRO
    assert row.current_period_end is not None, "renewal stored no expiry"
    assert abs((row.current_period_end
                - datetime.utcfromtimestamp(period_end)).total_seconds()) < 2


def test_a_real_cancellation_downgrades_to_free(monkeypatch):
    _real_stripe(monkeypatch)
    _deliver("checkout.session.completed", {
        "id": "cs_c", "object": "checkout.session", "client_reference_id": _UID,
        "customer": "cus_c", "subscription": "sub_c"})
    assert _plan_of(_UID) == PlanTier.PRO
    _deliver("customer.subscription.deleted", {
        "id": "sub_c", "object": "subscription", "status": "canceled"})
    assert _plan_of(_UID) == PlanTier.FREE


def test_a_real_event_with_a_forged_signature_is_refused(monkeypatch):
    """The real verifier, not our double, rejects a bad signature — and the
    route turns that into 400, never a 500 Stripe would retry."""
    _real_stripe(monkeypatch)
    payload = _event("checkout.session.completed", {
        "id": "cs_f", "object": "checkout.session", "client_reference_id": _UID})
    with pytest.raises(ValueError, match="verification failed"):
        billing.handle_webhook(payload, _signed(payload, "whsec_wrong_secret"))
    assert _plan_of(_UID) is None


def test_a_paid_user_resolves_as_pro_afterwards(monkeypatch):
    """End to end: the webhook that failed in production now lands the user on
    PRO through the same `_get_user_plan` the dashboard reads."""
    from app.api.server import _get_user_plan
    _real_stripe(monkeypatch)
    _deliver("checkout.session.completed", {
        "id": "cs_plan", "object": "checkout.session", "client_reference_id": _UID,
        "customer": "cus_plan", "subscription": "sub_plan"})
    assert billing.stripe_enabled() is True
    assert _get_user_plan(_UID) == PlanTier.PRO


# ── plan resolution ──────────────────────────────────────────────────────────

def _set_stripe(monkeypatch, on: bool):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_x" if on else "", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x" if on else "",
                        raising=False)
    assert billing.stripe_enabled() is on


def test_everyone_is_pro_while_payments_are_not_live(monkeypatch):
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, False)
    assert _get_user_plan(_UID) == PlanTier.PRO
    assert _get_user_plan("anyone-at-all") == PlanTier.PRO


def test_turning_stripe_on_does_not_drop_existing_users(monkeypatch, caplog):
    """THE CLIFF, defused. With no cutoff set, a user who has a profile keeps
    PRO the second the STRIPE_* vars land — and the app says so, once, so the
    founder cannot forget to set the cutoff at go-live."""
    import logging
    from app.api import server
    _set_stripe(monkeypatch, True)
    monkeypatch.setattr(settings, "plan_grandfather_until", "", raising=False)
    with get_session() as s:
        s.add(UserProfile(user_id=_UID, created_at=datetime(2026, 8, 20)))
        s.commit()
    server._GRANDFATHER_WARNED[0] = False
    with caplog.at_level(logging.WARNING, logger="app.api.server"):
        assert server._get_user_plan(_UID) == PlanTier.PRO
        assert server._get_user_plan(_UID) == PlanTier.PRO
    warned = [r for r in caplog.records if "PLAN_GRANDFATHER_UNTIL" in r.getMessage()]
    assert len(warned) == 1, "warn once per process, not once per plan lookup"


def test_an_account_with_no_profile_is_never_grandfathered(monkeypatch):
    """'Everyone with a profile' is the rule — a bare uid that never onboarded
    gets nothing for free."""
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, True)
    monkeypatch.setattr(settings, "plan_grandfather_until", "", raising=False)
    assert _get_user_plan(_UID) == PlanTier.FREE


def test_grandfathering_keeps_pre_launch_users_on_pro(monkeypatch):
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, True)
    with get_session() as s:
        s.add(UserProfile(user_id=_UID, created_at=datetime(2026, 7, 1)))
        s.commit()
    monkeypatch.setattr(settings, "plan_grandfather_until", "2026-08-01", raising=False)
    assert _get_user_plan(_UID) == PlanTier.PRO


def test_grandfathering_does_not_cover_users_who_signed_up_after_the_cutoff(monkeypatch):
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, True)
    with get_session() as s:
        s.add(UserProfile(user_id=_UID, created_at=datetime(2026, 9, 1)))
        s.commit()
    monkeypatch.setattr(settings, "plan_grandfather_until", "2026-08-01", raising=False)
    assert _get_user_plan(_UID) == PlanTier.FREE


@pytest.mark.parametrize("raw", ["", "  ", "not-a-date", "01/08/2026"])
def test_an_unset_or_unparseable_cutoff_keeps_existing_users_on_pro(monkeypatch, raw):
    """The direction this fails in is deliberate: a fat-fingered date at
    go-live must not lock the beta out. Free PRO for a while is a revenue
    leak the WARNING makes visible; a locked-out user base is a broken
    promise. (Set the date correctly and later signups are FREE — see the
    cutoff tests above.)"""
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, True)
    with get_session() as s:
        s.add(UserProfile(user_id=_UID, created_at=datetime(2020, 1, 1)))
        s.commit()
    monkeypatch.setattr(settings, "plan_grandfather_until", raw, raising=False)
    assert _get_user_plan(_UID) == PlanTier.PRO


def test_the_cutoff_is_unset_by_default_which_is_no_cliff():
    """Unset = everyone with a profile keeps PRO when Stripe turns on. The
    founder sets PLAN_GRANDFATHER_UNTIL to the go-live date; that is the one
    billing step that is a decision, not a secret."""
    assert settings.plan_grandfather_until == ""


def test_a_subscription_row_always_wins_over_grandfathering(monkeypatch):
    """A grandfathered user who subscribed and then cancelled is FREE: the
    row is the truth once it exists, or 'cancel' would mean nothing."""
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, True)
    monkeypatch.setattr(settings, "plan_grandfather_until", "", raising=False)
    with get_session() as s:
        s.add(UserProfile(user_id=_UID, created_at=datetime(2026, 8, 20)))
        s.commit()
    assert _get_user_plan(_UID) == PlanTier.PRO
    billing.set_plan(_UID, PlanTier.FREE, stripe_subscription_id="sub_cancelled")
    assert _get_user_plan(_UID) == PlanTier.FREE


def test_a_paid_row_is_pro_and_an_expired_one_falls_back_to_free(monkeypatch):
    """3-day grace, asserted on both sides of the boundary."""
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, True)
    now = datetime.utcnow()

    def _set_period_end(when):
        with get_session() as s:
            row = s.exec(select(UserSubscription).where(
                UserSubscription.user_id == _UID)).first()
            if row is None:
                row = UserSubscription(user_id=_UID)
            row.plan = PlanTier.PRO
            row.current_period_end = when
            s.add(row)
            s.commit()

    _set_period_end(now + timedelta(days=10))
    assert _get_user_plan(_UID) == PlanTier.PRO, "an active subscription is PRO"
    _set_period_end(now - timedelta(days=2))
    assert _get_user_plan(_UID) == PlanTier.PRO, "inside the 3-day grace, still PRO"
    _set_period_end(now - timedelta(days=4))
    assert _get_user_plan(_UID) == PlanTier.FREE, "past the grace, back to FREE"


def test_local_dev_is_always_pro(monkeypatch):
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, True)
    assert _get_user_plan("local") == PlanTier.PRO


# ── the numbers the plans actually mean ──────────────────────────────────────

def test_plan_limits_are_the_documented_numbers():
    """These drive per-user spend and feed straight into the CAPACITY arithmetic,
    so a silent edit is a silent change to unit economics."""
    from app.db.models import PLAN_LIMITS
    assert PLAN_LIMITS[PlanTier.FREE]["finals_daily"] == 120
    assert PLAN_LIMITS[PlanTier.PRO]["finals_daily"] == 250
    assert PLAN_LIMITS[PlanTier.AGENCY]["finals_daily"] == 250
    assert PLAN_LIMITS[PlanTier.FREE]["tailor_daily"] == 5
    # A real number, not None: "unlimited apart from the 25/day abuse ceiling"
    # was the default for every user while Stripe is unconfigured, because
    # _get_user_plan puts everyone on PRO. 12/day is past real human use.
    assert PLAN_LIMITS[PlanTier.PRO]["tailor_daily"] == 35
    assert PLAN_LIMITS[PlanTier.FREE]["autofill_weekly"] == 2


def test_payment_options_never_leak_a_secret(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_SECRET", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_SECRET", raising=False)
    blob = json.dumps(billing.payment_options())
    assert "SECRET" not in blob


# ── one price, stated once ───────────────────────────────────────────────────

def test_pro_is_one_hundred_dollars_a_month_and_every_surface_reads_it():
    """PLAN_PRICES[PRO] is the single source; the pricing page, the dashboard
    plans modal, the upsell strings and /api/billing/options all render from
    it. A literal "$10" anywhere is the old price leaking back."""
    import pathlib
    from fastapi.testclient import TestClient
    from app.api.server import app
    from app.db.models import PLAN_PRICES

    assert PLAN_PRICES[PlanTier.PRO] == 100
    assert billing.pro_price_usd() == 100
    assert billing.payment_options()["price_monthly_usd"] == 100

    client = TestClient(app)
    page = client.get("/pricing").text
    assert "$100" in page and "$10/" not in page and "$10<" not in page
    assert "cancel any time" in page.lower()

    tpl_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates"
    for name in ("pricing.html", "dashboard.html"):
        src = (tpl_dir / name).read_text(encoding="utf-8")
        assert "$10/" not in src and "$10<" not in src and "$10 " not in src, name
        assert "pro_price" in src, f"{name} must render the price from PLAN_PRICES"


def test_limit_messages_quote_the_real_price(monkeypatch):
    """The 429 detail the dashboard shows when a Free user hits a cap."""
    from app.api import server as srv
    from app.db.models import PLAN_LIMITS
    monkeypatch.setattr(srv, "_get_user_plan", lambda uid: PlanTier.FREE)
    monkeypatch.setattr(srv, "_get_week_autofill_count",
                        lambda session, uid: PLAN_LIMITS[PlanTier.FREE]["autofill_weekly"])
    ok, msg, info = srv._check_autofill_limit(_UID)
    assert not ok and "$100/mo" in msg and "$10/" not in msg


# ── cancel any time: the Stripe Customer Portal ──────────────────────────────

def _client(monkeypatch, uid):
    from fastapi.testclient import TestClient
    from app.api import server
    monkeypatch.setattr(server, "_get_user_id", lambda request: uid)
    return TestClient(server.app)


def test_portal_is_unavailable_until_stripe_is_live(monkeypatch):
    _set_stripe(monkeypatch, False)
    r = _client(monkeypatch, _UID).post("/api/billing/portal")
    assert r.status_code == 503


def test_portal_needs_a_stripe_customer(monkeypatch, fake_stripe):
    """Bank-transfer activations and grandfathered users have nothing to
    manage in Stripe — say so instead of erroring."""
    _set_stripe(monkeypatch, True)
    r = _client(monkeypatch, _UID).post("/api/billing/portal")
    assert r.status_code == 404
    assert "subscription" in r.json()["detail"].lower()


def test_portal_sends_a_subscriber_to_their_stripe_billing_page(monkeypatch, fake_stripe):
    _set_stripe(monkeypatch, True)
    calls = {}

    class _Session:
        @staticmethod
        def create(**kw):
            calls.update(kw)
            return types.SimpleNamespace(url="https://billing.stripe.com/p/session_x")

    sys.modules["stripe"].billing_portal = types.SimpleNamespace(Session=_Session)
    billing.handle_webhook(_event("checkout.session.completed", {
        "client_reference_id": _UID, "customer": "cus_42", "subscription": "sub_42"}), "sig")
    r = _client(monkeypatch, _UID).post("/api/billing/portal")
    assert r.status_code == 200 and r.json()["url"].startswith("https://billing.stripe.com/")
    assert calls["customer"] == "cus_42"
    assert calls["return_url"].endswith("/dashboard?billing=portal")


def test_a_cancellation_from_the_portal_lands_as_free_at_period_end(fake_stripe):
    """The user cancels in Stripe; Stripe tells us. We never cancel for them."""
    billing.handle_webhook(_event("checkout.session.completed", {
        "client_reference_id": _UID, "customer": "cus_1", "subscription": "sub_1"}), "sig")
    # cancel_at_period_end: still active until the period runs out
    billing.handle_webhook(_event("customer.subscription.updated", {
        "id": "sub_1", "status": "active", "cancel_at_period_end": True,
        "current_period_end": int((datetime.utcnow() + timedelta(days=9)).timestamp())}), "sig")
    assert _plan_of(_UID) == PlanTier.PRO
    billing.handle_webhook(_event("customer.subscription.deleted",
                                  {"id": "sub_1", "status": "canceled"}), "sig")
    assert _plan_of(_UID) == PlanTier.FREE


def test_a_failed_renewal_keeps_access_during_dunning_then_falls_to_free(fake_stripe):
    """past_due = Stripe is retrying the card: the user keeps PRO. unpaid or
    deleted (Stripe gave up) = FREE. No code of ours decides the retry policy."""
    billing.handle_webhook(_event("checkout.session.completed", {
        "client_reference_id": _UID, "customer": "cus_1", "subscription": "sub_1"}), "sig")
    billing.handle_webhook(_event("customer.subscription.updated",
                                  {"id": "sub_1", "status": "past_due"}), "sig")
    assert _plan_of(_UID) == PlanTier.PRO
    billing.handle_webhook(_event("customer.subscription.updated",
                                  {"id": "sub_1", "status": "unpaid"}), "sig")
    assert _plan_of(_UID) == PlanTier.FREE


# ══════════════════════════════════════════════════════════════════════════
# Repeat checkout must not sell a second subscription
# ══════════════════════════════════════════════════════════════════════════
# Reviewed 2026-09-16: checkout always created a session, with no check for an
# existing subscription. Completing two of them left the card billed twice
# while `set_plan` keeps ONE (customer, subscription) pair — so the first
# subscription kept charging and was invisible to the portal, which reads that
# one pair. The user could not even find it to cancel it.

@pytest.fixture
def fake_checkout(monkeypatch):
    """A stripe module that records checkout.Session.create calls."""
    mod = types.ModuleType("stripe")
    mod.api_key = None
    calls = []

    class Session:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(url="https://checkout.test/session")

    mod.checkout = types.SimpleNamespace(Session=Session)
    monkeypatch.setitem(sys.modules, "stripe", mod)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x", raising=False)
    return calls


def _seed_sub(**kw):
    with get_session() as s:
        row = s.exec(select(UserSubscription).where(
            UserSubscription.user_id == _UID)).first() or UserSubscription(user_id=_UID)
        for k, v in kw.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


def test_a_repeat_checkout_for_an_existing_subscriber_is_refused(fake_checkout):
    _seed_sub(plan=PlanTier.PRO, stripe_customer_id="cus_1",
              stripe_subscription_id="sub_1")
    with pytest.raises(billing.AlreadySubscribed):
        billing.create_checkout_session(_UID, "a@b.test", "https://app.test")
    assert fake_checkout == [], "no second subscription may be created"


def test_a_lapsed_subscriber_can_check_out_again(fake_checkout):
    """FREE with an old subscription id is someone whose plan ended. They are
    allowed to buy again — the guard is about DOUBLE billing, not about
    locking anyone out."""
    _seed_sub(plan=PlanTier.FREE, stripe_customer_id="cus_1",
              stripe_subscription_id="sub_old")
    url = billing.create_checkout_session(_UID, "a@b.test", "https://app.test")
    assert url == "https://checkout.test/session"
    assert len(fake_checkout) == 1


def test_checkout_reuses_a_known_customer_instead_of_making_another(fake_checkout):
    _seed_sub(plan=PlanTier.FREE, stripe_customer_id="cus_1")
    billing.create_checkout_session(_UID, "a@b.test", "https://app.test")
    kwargs = fake_checkout[0]
    assert kwargs["customer"] == "cus_1"
    assert "customer_email" not in kwargs, "Stripe rejects both together"


def test_a_first_time_buyer_is_identified_by_email(fake_checkout):
    billing.create_checkout_session(_UID, "a@b.test", "https://app.test")
    kwargs = fake_checkout[0]
    assert kwargs["customer_email"] == "a@b.test"
    assert "customer" not in kwargs


def test_rapid_repeat_clicks_collapse_onto_one_idempotency_key(fake_checkout):
    """A double-submit used to create two sessions, either of which the user
    could complete."""
    billing.create_checkout_session(_UID, "a@b.test", "https://app.test")
    billing.create_checkout_session(_UID, "a@b.test", "https://app.test")
    keys = [c["idempotency_key"] for c in fake_checkout]
    assert keys[0] == keys[1] and keys[0]


# ══════════════════════════════════════════════════════════════════════════
# Webhooks are at-least-once and unordered
# ══════════════════════════════════════════════════════════════════════════

def _wipe_events():
    from app.db.models import BillingEvent
    with get_session() as s:
        for r in s.exec(select(BillingEvent).where(
                BillingEvent.event_id.like("evt_test_%"))).all():
            s.delete(r)
        s.commit()


def _ev(etype: str, obj: dict, event_id: str, created: int) -> bytes:
    return json.dumps({"id": event_id, "type": etype, "created": created,
                       "data": {"object": obj}}).encode()


def _period_end_of(uid: str):
    with get_session() as s:
        row = s.exec(select(UserSubscription).where(
            UserSubscription.user_id == uid)).first()
        return row.current_period_end if row else None


def test_a_replayed_checkout_cannot_re_grant_a_cancelled_plan(fake_stripe):
    """Stripe retries every non-2xx and replays on request. Re-applying a
    checkout event after the user cancelled handed them PRO again."""
    _wipe_events()
    try:
        payload = _ev("checkout.session.completed",
                      {"client_reference_id": _UID, "customer": "cus_1",
                       "subscription": "sub_1"}, "evt_test_replay", 1_000)
        billing.handle_webhook(payload, "sig")
        assert _plan_of(_UID) == PlanTier.PRO

        billing.set_plan(_UID, PlanTier.FREE)
        out = billing.handle_webhook(payload, "sig")
        assert out.get("duplicate") is True
        assert _plan_of(_UID) == PlanTier.FREE, "a replay must change nothing"
    finally:
        _wipe_events()


def test_checkout_completion_does_not_erase_a_known_period_end(fake_stripe):
    """The unguarded assignment in set_plan meant the checkout event — which
    carries no period end — wiped the real one to NULL, and entitlement reads
    NULL as 'never expires'. Ordinary webhook ordering, no replay needed."""
    _wipe_events()
    try:
        ends = datetime.utcnow() + timedelta(days=30)
        _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_1",
                  current_period_end=ends)
        billing.handle_webhook(
            _ev("checkout.session.completed",
                {"client_reference_id": _UID, "customer": "cus_1",
                 "subscription": "sub_1"}, "evt_test_order", 2_000), "sig")
        assert _plan_of(_UID) == PlanTier.PRO
        assert _period_end_of(_UID) is not None, "PRO with no expiry is PRO forever"
    finally:
        _wipe_events()


def test_a_stale_unpaid_event_cannot_revoke_a_recovered_subscription(fake_stripe):
    """Dunning `unpaid` overtaken by the recovery that followed it: arriving
    last, it used to cut off a user who is paying again."""
    _wipe_events()
    try:
        _seed_sub(plan=PlanTier.FREE, stripe_subscription_id="sub_1")
        ends = int((datetime.utcnow() + timedelta(days=30)).timestamp())
        billing.handle_webhook(
            _ev("customer.subscription.updated",
                {"id": "sub_1", "status": "active", "current_period_end": ends},
                "evt_test_recover", 5_000), "sig")
        assert _plan_of(_UID) == PlanTier.PRO

        out = billing.handle_webhook(
            _ev("customer.subscription.updated",
                {"id": "sub_1", "status": "unpaid"},
                "evt_test_stale", 4_000), "sig")          # created EARLIER
        assert out.get("stale") is True
        assert _plan_of(_UID) == PlanTier.PRO
    finally:
        _wipe_events()


def test_a_genuinely_newer_cancellation_still_downgrades(fake_stripe):
    """The ordering guard must not become a way to ignore real cancellations."""
    _wipe_events()
    try:
        _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_1")
        billing.handle_webhook(
            _ev("customer.subscription.updated",
                {"id": "sub_1", "status": "active"},
                "evt_test_a", 5_000), "sig")
        billing.handle_webhook(
            _ev("customer.subscription.deleted", {"id": "sub_1"},
                "evt_test_b", 6_000), "sig")
        assert _plan_of(_UID) == PlanTier.FREE
        assert _period_end_of(_UID) is None, "a downgrade leaves no stale expiry"
    finally:
        _wipe_events()


# ══════════════════════════════════════════════════════════════════════════
# Test mode is not revenue
# ══════════════════════════════════════════════════════════════════════════
# Audit 2026-09-16: production's STRIPE_SECRET_KEY was an sk_test_ key (the
# billing portal opened under "Spotapply llc sandbox"; live payments were never
# activated). stripe_enabled() is deliberately True for test keys, and every
# "is this user paying?" check was built on it — so the founder's SANDBOX
# subscription was $100 MRR in /api/admin/metrics, and a grandfathered user with
# NO subscription row read as a paying subscriber to the dormancy gate.

def _row(**kw) -> UserSubscription:
    row = UserSubscription(user_id=_UID, plan=PlanTier.PRO)
    for k, v in kw.items():
        setattr(row, k, v)
    return row


def _keys(monkeypatch, key: str):
    monkeypatch.setattr(settings, "stripe_secret_key", key, raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x" if key else "",
                        raising=False)


def test_live_mode_is_only_an_sk_live_key(monkeypatch):
    _keys(monkeypatch, "sk_test_x")
    assert billing.stripe_enabled() and not billing.stripe_live_mode()
    assert billing.stripe_mode() == "test"
    _keys(monkeypatch, "sk_live_x")
    assert billing.stripe_live_mode() and billing.stripe_mode() == "live"
    _keys(monkeypatch, "")
    assert not billing.stripe_live_mode() and billing.stripe_mode() == "off"


def test_payment_options_say_whether_stripe_is_live_without_the_key(monkeypatch):
    _keys(monkeypatch, "sk_test_SECRET")
    opts = billing.payment_options()
    assert opts["stripe_enabled"] is True and opts["stripe_live"] is False
    assert "SECRET" not in json.dumps(opts)
    _keys(monkeypatch, "sk_live_SECRET")
    assert billing.payment_options()["stripe_live"] is True


def test_a_sandbox_subscription_is_never_a_paid_entitlement(monkeypatch):
    """THE MRR BUG. A Stripe-backed PRO row under test keys — the only
    subscription production had."""
    _keys(monkeypatch, "sk_test_x")
    row = _row(stripe_customer_id="cus_sb", stripe_subscription_id="sub_sb",
               current_period_end=datetime.utcnow() + timedelta(days=17))
    assert billing.is_paid_entitlement(row) is False


def test_the_same_row_under_live_keys_is_paid(monkeypatch):
    _keys(monkeypatch, "sk_live_x")
    row = _row(stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
               current_period_end=datetime.utcnow() + timedelta(days=17))
    assert billing.is_paid_entitlement(row) is True


def test_a_manual_activation_is_paid_regardless_of_stripe_mode(monkeypatch):
    """Bank transfer / admin set-plan: no Stripe ids. Someone paid outside
    Stripe and an operator wrote the row — that is revenue in any mode."""
    for key in ("sk_test_x", "sk_live_x", ""):
        _keys(monkeypatch, key)
        row = _row(current_period_end=datetime.utcnow() + timedelta(days=20))
        assert billing.is_paid_entitlement(row) is True, key
        # A manual row with no period end is open-ended (the operator did not
        # set one) — still paid; only a Stripe row needs reconcile to fill it.
        assert billing.is_paid_entitlement(_row()) is True, key


@pytest.mark.parametrize("key", ["sk_test_x", "sk_live_x"])
def test_no_row_free_plan_and_expired_rows_are_never_paid(monkeypatch, key):
    _keys(monkeypatch, key)
    assert billing.is_paid_entitlement(None) is False, "no row = nobody is charging them"
    assert billing.is_paid_entitlement(_row(plan=PlanTier.FREE)) is False
    expired = _row(current_period_end=datetime.utcnow() - timedelta(days=4))
    assert billing.is_paid_entitlement(expired) is False, "past the 3-day grace"
    in_grace = _row(current_period_end=datetime.utcnow() - timedelta(days=2))
    assert billing.is_paid_entitlement(in_grace) is True, "inside the grace, still paid"


def test_the_grace_is_one_number_shared_with_plan_resolution():
    """_get_user_plan and is_paid_entitlement must expire on the same day, or
    a user is FREE to the limits and paid to the KPIs (or the reverse)."""
    assert billing.ENTITLEMENT_GRACE_DAYS == 3
    import inspect
    from app.api import server
    src = inspect.getsource(server._get_user_plan)
    assert "entitlement_expired" in src and "timedelta(days=3)" not in src


def test_a_hosted_deployment_on_test_keys_is_warned_once(monkeypatch, caplog):
    import logging
    from app.config import Settings
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    _keys(monkeypatch, "sk_test_x")
    billing._TEST_MODE_WARNED[0] = False
    with caplog.at_level(logging.WARNING, logger="app.billing"):
        assert billing.warn_if_stripe_test_mode() is True
        assert billing.warn_if_stripe_test_mode() is False, "once per process"
    msgs = [r.getMessage() for r in caplog.records if "TEST mode" in r.getMessage()]
    assert len(msgs) == 1 and "not revenue" in msgs[0]
    billing._TEST_MODE_WARNED[0] = False


def test_live_keys_and_local_dev_get_no_test_mode_warning(monkeypatch):
    from app.config import Settings
    billing._TEST_MODE_WARNED[0] = False
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    _keys(monkeypatch, "sk_live_x")
    assert billing.warn_if_stripe_test_mode() is False
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: False))
    _keys(monkeypatch, "sk_test_x")
    assert billing.warn_if_stripe_test_mode() is False, "local SQLite dev is not a deployment"
    assert billing._TEST_MODE_WARNED[0] is False


# ══════════════════════════════════════════════════════════════════════════
# The period end must land, whichever event arrives
# ══════════════════════════════════════════════════════════════════════════
# The founder's PRO row had current_period_end=NULL — read as "never expires" —
# while Stripe showed next billing 2026-10-04. checkout.session.completed wrote
# it and nothing followed: 0 webhook deliveries that week, and the endpoint was
# never subscribed to invoice events. Three closures: checkout retrieves the
# subscription, invoice.paid is handled, reconcile re-reads drifted rows.

def _sub(sub_id: str, days: int, status: str = "active", items: bool = True) -> dict:
    """A Subscription payload the way this account's API version shapes it:
    current_period_end lives on the ITEMS (2025-03-31.basil onwards)."""
    end = int((datetime.utcnow() + timedelta(days=days)).timestamp())
    sub = {"id": sub_id, "object": "subscription", "status": status}
    if items:
        sub["items"] = {"object": "list", "data": [
            {"id": "si_1", "object": "subscription_item", "current_period_end": end}]}
    else:
        sub["current_period_end"] = end
    return sub


def _days_from_now(dt) -> float:
    return (dt - datetime.utcnow()).total_seconds() / 86400


def test_checkout_completion_fetches_the_period_end_from_the_subscription(fake_stripe):
    fake_stripe["subscriptions"]["sub_1"] = _sub("sub_1", days=30)
    out = billing.handle_webhook(_event("checkout.session.completed", {
        "client_reference_id": _UID, "customer": "cus_1", "subscription": "sub_1"}), "sig")
    assert out["received"] is True
    assert fake_stripe["retrieved"] == ["sub_1"]
    assert _plan_of(_UID) == PlanTier.PRO
    end = _period_end_of(_UID)
    assert end is not None, "the founder's NULL period end, reproduced"
    assert 29 < _days_from_now(end) < 31


def test_checkout_completion_survives_a_failed_retrieve(fake_stripe, caplog):
    """Best effort: the plan is granted, the failure is logged, the webhook is
    2xx (Stripe must not retry a checkout already applied). Reconcile fills
    the period end later."""
    import logging
    with caplog.at_level(logging.WARNING, logger="app.billing"):
        out = billing.handle_webhook(_event("checkout.session.completed", {
            "client_reference_id": _UID, "customer": "cus_1",
            "subscription": "sub_missing"}), "sig")
    assert out == {"received": True, "type": "checkout.session.completed"}
    assert _plan_of(_UID) == PlanTier.PRO
    assert _period_end_of(_UID) is None
    assert any("could not retrieve subscription sub_missing" in r.getMessage()
               for r in caplog.records)


def test_a_real_checkout_reads_the_period_end_off_a_typed_subscription(monkeypatch):
    """With the real SDK: the retrieved object is a typed Subscription, not a
    dict, and its items are a typed ListObject — the same shape that broke
    `.get()` in production."""
    stripe = _real_stripe(monkeypatch)
    payload = _sub("sub_typed", days=30)
    typed = stripe.Subscription.construct_from(payload, "sk_test_regression")
    assert not isinstance(typed, dict)
    monkeypatch.setattr(stripe.Subscription, "retrieve", lambda sub_id, **kw: typed)
    _deliver("checkout.session.completed", {
        "id": "cs_typed", "object": "checkout.session", "client_reference_id": _UID,
        "customer": "cus_typed", "subscription": "sub_typed"})
    end = _period_end_of(_UID)
    assert end is not None and 29 < _days_from_now(end) < 31


def test_a_canceled_subscription_retrieved_at_checkout_does_not_grant_pro(fake_stripe):
    """A checkout event replayed weeks later (a NEW event id — Stripe resends
    on request) for a subscription since cancelled: the retrieve is the truth."""
    fake_stripe["subscriptions"]["sub_1"] = _sub("sub_1", days=-10, status="canceled")
    billing.handle_webhook(_event("checkout.session.completed", {
        "client_reference_id": _UID, "customer": "cus_1", "subscription": "sub_1"}), "sig")
    assert _plan_of(_UID) == PlanTier.FREE


def test_invoice_paid_fills_and_extends_the_period_end(fake_stripe):
    _seed_sub(plan=PlanTier.PRO, stripe_customer_id="cus_1",
              stripe_subscription_id="sub_1", current_period_end=None)
    fake_stripe["subscriptions"]["sub_1"] = _sub("sub_1", days=30)
    billing.handle_webhook(_event("invoice.paid", {
        "id": "in_1", "object": "invoice", "subscription": "sub_1"}), "sig")
    end = _period_end_of(_UID)
    assert end is not None and 29 < _days_from_now(end) < 31, "renewal left NULL"

    # Next month's renewal moves it forward.
    fake_stripe["subscriptions"]["sub_1"] = _sub("sub_1", days=60)
    billing.handle_webhook(_event("invoice.payment_succeeded", {
        "id": "in_2", "object": "invoice", "subscription": "sub_1"}), "sig")
    assert 59 < _days_from_now(_period_end_of(_UID)) < 61
    assert _plan_of(_UID) == PlanTier.PRO


def test_invoice_paid_finds_the_subscription_under_parent_on_new_api_versions(fake_stripe):
    """2025-03-31.basil moved it to parent.subscription_details.subscription;
    this account is on 2026-08-26.dahlia, so the top-level field is absent."""
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_p", current_period_end=None)
    fake_stripe["subscriptions"]["sub_p"] = _sub("sub_p", days=30)
    billing.handle_webhook(_event("invoice.paid", {
        "id": "in_p", "object": "invoice",
        "parent": {"type": "subscription_details",
                   "subscription_details": {"subscription": "sub_p"}}}), "sig")
    assert fake_stripe["retrieved"] == ["sub_p"]
    assert _period_end_of(_UID) is not None


def test_invoice_paid_falls_back_to_the_invoice_line_period_when_retrieve_fails(fake_stripe):
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_1", current_period_end=None)
    end = int((datetime.utcnow() + timedelta(days=30)).timestamp())
    billing.handle_webhook(_event("invoice.paid", {
        "id": "in_1", "object": "invoice", "subscription": "sub_1",
        "lines": {"object": "list", "data": [
            {"id": "il_1", "period": {"start": end - 30 * 86400, "end": end}}]}}), "sig")
    assert fake_stripe["retrieved"] == ["sub_1"], "the subscription was tried first"
    got = _period_end_of(_UID)
    assert got is not None and abs((got - datetime.utcfromtimestamp(end)).total_seconds()) < 2


def test_invoice_paid_for_an_unknown_subscription_is_a_no_op(fake_stripe):
    fake_stripe["subscriptions"]["sub_x"] = _sub("sub_x", days=30)
    out = billing.handle_webhook(_event("invoice.paid", {
        "id": "in_x", "object": "invoice", "subscription": "sub_x"}), "sig")
    assert out["received"] is True and _plan_of(_UID) is None
    assert fake_stripe["retrieved"] == [], "nothing to apply it to — no Stripe call"


def test_a_stale_invoice_paid_cannot_move_entitlement_backwards(fake_stripe):
    """The ordering rule applies to invoices too: a late-delivered renewal
    receipt from LAST month must not overwrite this month's period end."""
    _wipe_events()
    try:
        _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_1",
                  current_period_end=datetime.utcnow() + timedelta(days=50),
                  last_event_at=datetime.utcfromtimestamp(9_000))
        fake_stripe["subscriptions"]["sub_1"] = _sub("sub_1", days=20)
        out = billing.handle_webhook(
            _ev("invoice.paid", {"id": "in_old", "subscription": "sub_1"},
                "evt_test_inv_stale", 8_000), "sig")
        assert out.get("stale") is True
        assert 49 < _days_from_now(_period_end_of(_UID)) < 51
    finally:
        _wipe_events()


def test_invoice_payment_failed_changes_nothing(fake_stripe, caplog):
    """Dunning: Stripe retries; access continues until it says unpaid."""
    import logging
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_1",
              current_period_end=datetime.utcnow() + timedelta(days=5))
    with caplog.at_level(logging.INFO, logger="app.billing"):
        billing.handle_webhook(_event("invoice.payment_failed", {
            "id": "in_f", "object": "invoice", "subscription": "sub_1"}), "sig")
    assert _plan_of(_UID) == PlanTier.PRO
    assert 4 < _days_from_now(_period_end_of(_UID)) < 6
    assert any("payment failed" in r.getMessage() for r in caplog.records)
    assert fake_stripe["retrieved"] == []


def test_customer_subscription_created_is_applied_like_updated(fake_stripe):
    _seed_sub(plan=PlanTier.FREE, stripe_subscription_id="sub_1")
    billing.handle_webhook(_event("customer.subscription.created",
                                  _sub("sub_1", days=30)), "sig")
    assert _plan_of(_UID) == PlanTier.PRO
    assert 29 < _days_from_now(_period_end_of(_UID)) < 31


def test_an_update_without_a_period_end_keeps_the_stored_one(fake_stripe):
    """A payload that carries no period end says nothing about it — the same
    rule set_plan already applies to the checkout event. Previously a bare
    past_due update wiped a known expiry to NULL (= never expires)."""
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_1",
              current_period_end=datetime.utcnow() + timedelta(days=12))
    billing.handle_webhook(_event("customer.subscription.updated",
                                  {"id": "sub_1", "status": "past_due"}), "sig")
    assert _plan_of(_UID) == PlanTier.PRO
    assert 11 < _days_from_now(_period_end_of(_UID)) < 13


def test_an_incomplete_expired_subscription_is_not_pro(fake_stripe):
    """The initial payment never went through and Stripe closed it: a card
    that was never charged must not buy a plan."""
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_1")
    billing.handle_webhook(_event("customer.subscription.updated",
                                  {"id": "sub_1", "status": "incomplete_expired"}), "sig")
    assert _plan_of(_UID) == PlanTier.FREE


# ── reconcile: the backstop for a webhook stream that stopped ────────────────

def test_reconcile_fills_a_null_period_end(fake_stripe):
    """The founder's row, exactly."""
    _seed_sub(plan=PlanTier.PRO, stripe_customer_id="cus_f",
              stripe_subscription_id="sub_f", current_period_end=None)
    fake_stripe["subscriptions"]["sub_f"] = _sub("sub_f", days=17)
    out = billing.reconcile_subscriptions()
    assert out["examined"] >= 1 and out["pro"] >= 1 and out["failed"] == 0
    assert "sub_f" in fake_stripe["retrieved"]
    assert _plan_of(_UID) == PlanTier.PRO
    assert 16 < _days_from_now(_period_end_of(_UID)) < 18


def test_reconcile_downgrades_a_long_expired_row_stripe_says_is_dead(fake_stripe):
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_d",
              current_period_end=datetime.utcnow() - timedelta(days=20))
    fake_stripe["subscriptions"]["sub_d"] = _sub("sub_d", days=-20, status="canceled")
    out = billing.reconcile_subscriptions()
    assert out["free"] >= 1
    assert _plan_of(_UID) == PlanTier.FREE and _period_end_of(_UID) is None


def test_reconcile_renews_a_long_expired_row_stripe_says_is_active(fake_stripe):
    """The renewal webhook never arrived (0 deliveries in a week): the row
    says expired, Stripe says paid. Stripe wins."""
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_r",
              current_period_end=datetime.utcnow() - timedelta(days=10))
    fake_stripe["subscriptions"]["sub_r"] = _sub("sub_r", days=20)
    billing.reconcile_subscriptions()
    assert 19 < _days_from_now(_period_end_of(_UID)) < 21


def test_reconcile_leaves_healthy_rows_alone(fake_stripe):
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_ok",
              current_period_end=datetime.utcnow() + timedelta(days=20))
    billing.reconcile_subscriptions()
    assert "sub_ok" not in fake_stripe["retrieved"], "a healthy row costs no Stripe call"
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id=None,
              current_period_end=None)
    billing.reconcile_subscriptions()
    assert fake_stripe["retrieved"] == [], "a manual activation has nothing to reconcile"


def test_reconcile_never_raises_and_counts_what_failed(fake_stripe):
    _seed_sub(plan=PlanTier.PRO, stripe_subscription_id="sub_gone", current_period_end=None)
    out = billing.reconcile_subscriptions()            # retrieve raises LookupError
    assert out["failed"] >= 1
    assert _plan_of(_UID) == PlanTier.PRO, "a failed read changes nothing"

    def _boom(*a, **k):
        raise RuntimeError("stripe is down")
    sys.modules["stripe"].Subscription.retrieve = staticmethod(_boom)
    out = billing.reconcile_subscriptions()
    assert out["failed"] >= 1 and _plan_of(_UID) == PlanTier.PRO


def test_reconcile_is_skipped_while_stripe_is_off(monkeypatch):
    _keys(monkeypatch, "")
    assert billing.reconcile_subscriptions() == {
        "examined": 0, "pro": 0, "free": 0, "failed": 0, "skipped": True}


def test_the_billing_maintenance_loop_is_scheduled_next_to_registry_maintenance():
    """One create_task line in the startup body, after the lanes_enabled
    return (two replicas must not both poll Stripe)."""
    import inspect
    from app.api import server
    src = inspect.getsource(server)
    assert "asyncio.create_task(_billing_maintenance())" in src
    assert inspect.iscoroutinefunction(server._billing_maintenance)
    body = inspect.getsource(server._billing_maintenance)
    assert "warn_if_stripe_test_mode" in body and "reconcile_subscriptions" in body
    assert "to_thread" in body, "reconcile is blocking HTTP — never on the event loop"


# ══════════════════════════════════════════════════════════════════════════
# Admin KPIs tell revenue from rehearsal, and people from pipeline churn
# ══════════════════════════════════════════════════════════════════════════

_MP = "billing-metrics-"


@pytest.fixture
def metrics(monkeypatch):
    """Call the admin metrics route as the admin; wipe this file's rows."""
    from app.api import server
    from app.db.models import Application, Job
    monkeypatch.setattr(server, "_require_admin_user", lambda request: "admin@test")

    def _wipe():
        with get_session() as s:
            for model in (Application, Job, UserSubscription, UserProfile):
                for r in s.exec(select(model).where(model.user_id.like(f"{_MP}%"))).all():
                    s.delete(r)
            s.commit()
    _wipe()
    yield lambda: server.admin_metrics(request=None)
    _wipe()


def _profile(uid: str, **kw):
    with get_session() as s:
        s.add(UserProfile(user_id=_MP + uid, **kw))
        s.commit()


def _subscription(uid: str, **kw):
    with get_session() as s:
        row = UserSubscription(user_id=_MP + uid, plan=PlanTier.PRO)
        for k, v in kw.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


def test_a_sandbox_subscription_is_zero_mrr_and_counted_as_sandbox(metrics, monkeypatch):
    """The production KPI: one sandbox subscription reported as $100 MRR."""
    _keys(monkeypatch, "sk_test_x")
    before = metrics()
    _profile("founder")
    _subscription("founder", stripe_customer_id="cus_sb", stripe_subscription_id="sub_sb",
                  current_period_end=datetime.utcnow() + timedelta(days=17))
    after = metrics()
    assert after["mrr_usd"] == before["mrr_usd"], "a sandbox subscription is not revenue"
    assert after["arr_usd"] == before["arr_usd"]
    assert after["paid_subscriptions"] == before["paid_subscriptions"]
    assert after["sandbox_subscriptions"] == before["sandbox_subscriptions"] + 1
    assert after["stripe_mode"] == "test"


def test_the_same_subscription_under_live_keys_is_revenue(metrics, monkeypatch):
    _keys(monkeypatch, "sk_live_x")
    before = metrics()
    _profile("payer")
    _subscription("payer", stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                  current_period_end=datetime.utcnow() + timedelta(days=17))
    after = metrics()
    assert after["mrr_usd"] == before["mrr_usd"] + 100
    assert after["arr_usd"] == before["arr_usd"] + 1200
    assert after["paid_subscriptions"] == before["paid_subscriptions"] + 1
    assert after["sandbox_subscriptions"] == before["sandbox_subscriptions"]
    assert after["stripe_mode"] == "live"


def test_a_manual_activation_is_revenue_even_on_test_keys(metrics, monkeypatch):
    _keys(monkeypatch, "sk_test_x")
    before = metrics()
    _profile("bank")
    _subscription("bank", current_period_end=datetime.utcnow() + timedelta(days=20))
    after = metrics()
    assert after["mrr_usd"] == before["mrr_usd"] + 100
    assert after["paid_subscriptions"] == before["paid_subscriptions"] + 1
    assert after["sandbox_subscriptions"] == before["sandbox_subscriptions"]


def test_a_stripe_row_with_no_period_end_is_flagged_for_reconcile(metrics, monkeypatch):
    _keys(monkeypatch, "sk_live_x")
    before = metrics()
    _profile("nullend")
    _subscription("nullend", stripe_subscription_id="sub_n", current_period_end=None)
    after = metrics()
    assert after["subscriptions_missing_period_end"] == before["subscriptions_missing_period_end"] + 1
    # ...and an expired row is neither revenue nor sandbox.
    _profile("lapsed")
    _subscription("lapsed", stripe_subscription_id="sub_l",
                  current_period_end=datetime.utcnow() - timedelta(days=30))
    final = metrics()
    assert final["mrr_usd"] == after["mrr_usd"]
    assert final["sandbox_subscriptions"] == after["sandbox_subscriptions"]


def test_active_users_reads_authenticated_activity_not_pipeline_churn(metrics):
    """Every user's applications showed updated_at 09-15/09-16 — the lanes
    touch them — so 10 users with no sign-in in 7 days were 'active'."""
    from app.db.models import Application, Job, JobSource
    before = metrics()
    _profile("visitor", last_active_at=datetime.utcnow() - timedelta(hours=3))
    _profile("ghost", last_active_at=datetime.utcnow() - timedelta(days=60))
    _profile("untracked")                                   # last_active_at NULL
    with get_session() as s:
        job = Job(user_id=_MP + "ghost", source=JobSource.GREENHOUSE,
                  external_id=_MP + "job", company="Acme", title="Engineer",
                  url="https://example.test/j")
        s.add(job)
        s.commit()
        s.refresh(job)
        s.add(Application(user_id=_MP + "ghost", job_id=job.id,
                          updated_at=datetime.utcnow()))   # a lane bumped it
        s.commit()
    after = metrics()
    assert after["active_users_7d"] == before["active_users_7d"] + 1, \
        "only the profile with an authenticated request in the window counts"
    assert after["users_with_pipeline_activity_7d"] == \
        before["users_with_pipeline_activity_7d"] + 1, "the old number survives, renamed"
    assert after["total_users"] == before["total_users"] + 3


def test_grandfathered_users_are_counted_by_the_same_rule_that_grants_them_pro(metrics, monkeypatch):
    """What silently leaves MRR-eligibility at go-live: profiles with no
    subscription row that _is_grandfathered resolves to PRO."""
    from app.api import server
    _keys(monkeypatch, "sk_test_x")

    def _with_cutoff(raw: str) -> dict:
        monkeypatch.setattr(settings, "plan_grandfather_until", raw, raising=False)
        return metrics()

    before_unset, before_dated = _with_cutoff(""), _with_cutoff("2026-08-01")
    _profile("early", created_at=datetime(2026, 7, 1))
    _profile("late", created_at=datetime(2026, 9, 10))
    _profile("subscribed", created_at=datetime(2026, 7, 1))
    _subscription("subscribed", stripe_subscription_id="sub_s",
                  current_period_end=datetime.utcnow() + timedelta(days=9))

    unset = _with_cutoff("")
    assert unset["grandfathered_users"] == before_unset["grandfathered_users"] + 2, \
        "cutoff unset: every profile without a row rides free"
    assert unset["plan_grandfather_until"] == ""

    dated = _with_cutoff("2026-08-01")
    assert dated["grandfathered_users"] == before_dated["grandfathered_users"] + 1, \
        "cutoff set: only the pre-cutoff signup without a row"
    assert dated["plan_grandfather_until"] == "2026-08-01"
    # The count and the per-user resolver are the same rule.
    assert server._is_grandfathered(_MP + "early") is True
    assert server._is_grandfathered(_MP + "late") is False
    assert server._get_user_plan(_MP + "subscribed") == PlanTier.PRO, "row wins"
