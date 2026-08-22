"""app/billing.py + plan resolution — both had zero tests, and both gate revenue.

Two things are pinned here.

The webhook. An unverified webhook is a free-upgrade vulnerability: anyone who
can POST /api/billing/webhook could hand themselves PRO. The code is written
correctly (refuses with no secret, raises on a bad signature) and these tests
keep it that way, because "we removed the verification to debug a webhook" is a
very normal Tuesday.

Plan resolution, which contains a cliff. `_get_user_plan` returns PRO for
everyone while Stripe is unconfigured — correct pre-revenue, since there is
nothing to buy — but no existing user has a user_subscription row, so the instant
STRIPE_SECRET_KEY and STRIPE_PRICE_ID_PRO are set every one of them silently
drops to FREE: 50 → 15 finals/day, unlimited → 5 tailors/day, unlimited → 2
autofills/week. That is worth knowing before flipping the switch on a user base
you spent months acquiring, so both sides of the flip are asserted explicitly and
PLAN_GRANDFATHER_UNTIL exists to defuse it.
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


def test_turning_stripe_on_drops_users_without_a_subscription_to_free(monkeypatch):
    """THE CLIFF, asserted so it is a known decision and not a surprise.

    Flip these two env vars in production and every existing user loses 35
    finals/day the same second.
    """
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
def test_grandfathering_fails_closed_on_a_missing_or_unparseable_date(monkeypatch, raw):
    """Never hand out PRO because a config value was fat-fingered."""
    from app.api.server import _get_user_plan
    _set_stripe(monkeypatch, True)
    with get_session() as s:
        s.add(UserProfile(user_id=_UID, created_at=datetime(2020, 1, 1)))
        s.commit()
    monkeypatch.setattr(settings, "plan_grandfather_until", raw, raising=False)
    assert _get_user_plan(_UID) == PlanTier.FREE


def test_grandfathering_is_off_by_default():
    assert settings.plan_grandfather_until == ""


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
    assert PLAN_LIMITS[PlanTier.FREE]["finals_daily"] == 15
    assert PLAN_LIMITS[PlanTier.PRO]["finals_daily"] == 50
    assert PLAN_LIMITS[PlanTier.AGENCY]["finals_daily"] == 100
    assert PLAN_LIMITS[PlanTier.FREE]["tailor_daily"] == 5
    # A real number, not None: "unlimited apart from the 25/day abuse ceiling"
    # was the default for every user while Stripe is unconfigured, because
    # _get_user_plan puts everyone on PRO. 12/day is past real human use.
    assert PLAN_LIMITS[PlanTier.PRO]["tailor_daily"] == 12
    assert PLAN_LIMITS[PlanTier.FREE]["autofill_weekly"] == 2


def test_payment_options_never_leak_a_secret(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_SECRET", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_SECRET", raising=False)
    blob = json.dumps(billing.payment_options())
    assert "SECRET" not in blob
