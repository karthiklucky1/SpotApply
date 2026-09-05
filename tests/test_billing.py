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
    """A stripe module whose signature check we control."""
    mod = types.ModuleType("stripe")
    mod.api_key = None
    state = {"verify": True, "event": None}

    class Webhook:
        @staticmethod
        def construct_event(payload, signature, secret):
            if not state["verify"]:
                raise ValueError("Invalid signature")
            return state["event"] or json.loads(payload)

    mod.Webhook = Webhook
    monkeypatch.setitem(sys.modules, "stripe", mod)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
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


def _real_stripe(monkeypatch):
    """The genuine `stripe` module, with our two settings pointed at it."""
    import stripe
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_regression", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_regression", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", _WHSEC, raising=False)
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
