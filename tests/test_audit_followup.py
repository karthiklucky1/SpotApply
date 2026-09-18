"""Regression cases missed by the September production-audit suite."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app import billing
from app.analytics import spend
from app.common import account_purge
from app.config import settings
from app.db.init_db import get_session
from app.db.models import BillingEvent, LlmSpend, PlanTier, UserSubscription

UID = "audit-followup-user"
EVENT = "evt_audit_followup"


@pytest.fixture(autouse=True)
def isolated_rows():
    def clean():
        with get_session() as session:
            session.exec(
                delete(UserSubscription).where(UserSubscription.user_id == UID)
            )
            session.exec(delete(LlmSpend).where(LlmSpend.user_id == UID))
            session.exec(delete(BillingEvent).where(BillingEvent.event_id == EVENT))
            session.commit()

    clean()
    yield
    clean()


def deliver(monkeypatch, event):
    def unavailable(*args, **kwargs):
        raise RuntimeError("Stripe temporarily unavailable")

    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_fake")
    monkeypatch.setattr(
        billing,
        "_stripe",
        lambda: SimpleNamespace(
            Webhook=SimpleNamespace(construct_event=lambda *args: event),
            Subscription=SimpleNamespace(retrieve=unavailable),
        ),
    )
    return billing.handle_webhook(json.dumps(event).encode(), "sig")


def checkout_event(created=1000):
    return {
        "id": EVENT,
        "type": "checkout.session.completed",
        "created": created,
        "livemode": False,
        "data": {
            "object": {
                "client_reference_id": UID,
                "customer": "cus_audit",
                "subscription": "sub_audit",
            }
        },
    }


def test_failed_plan_write_does_not_consume_webhook(monkeypatch):
    original = billing.set_plan

    def fail(*args, **kwargs):
        raise RuntimeError("temporary database failure")

    monkeypatch.setattr(billing, "set_plan", fail)
    with pytest.raises(RuntimeError, match="database failure"):
        deliver(monkeypatch, checkout_event())
    monkeypatch.setattr(billing, "set_plan", original)
    result = deliver(monkeypatch, checkout_event())
    assert not result.get("duplicate"), "a failed update must be retried"
    with get_session() as session:
        row = session.exec(
            select(UserSubscription).where(UserSubscription.user_id == UID)
        ).one()
        assert row.plan == PlanTier.PRO


def test_late_checkout_cannot_overwrite_a_newer_subscription(monkeypatch):
    billing.set_plan(
        UID,
        PlanTier.PRO,
        stripe_subscription_id="sub_new",
        last_event_at=datetime.utcfromtimestamp(2000),
    )
    result = deliver(monkeypatch, checkout_event())
    with get_session() as session:
        row = session.exec(
            select(UserSubscription).where(UserSubscription.user_id == UID)
        ).one()
        assert row.stripe_subscription_id == "sub_new"
        assert row.last_event_at == datetime.utcfromtimestamp(2000)
    assert result.get("stale")


@pytest.mark.parametrize(
    "message,status,code",
    [
        ("API route not found", 404, None),
        ("Upstream host not found", 503, None),
        ("user_not_found", None, None),
        ("Session not found", 404, "session_not_found"),
    ],
)
def test_only_explicit_auth_user_not_found_can_authorize_purge(message, status, code):
    class Error(Exception):
        pass

    error = Error(message)
    error.status, error.code = status, code

    def lookup(uid):
        raise error

    sb = SimpleNamespace(
        auth=SimpleNamespace(admin=SimpleNamespace(get_user_by_id=lookup))
    )
    assert account_purge._auth_user_gone(sb, UID) is None


def test_parallel_spend_writes_preserve_every_call():
    spend.record_llm_spend(
        UID,
        "score_final",
        provider="openai",
        model="gpt-4o-mini",
        usage={"input": 1000},
    )
    start = threading.Barrier(12)

    def record(_):
        start.wait(timeout=10)
        for _ in range(3):
            spend.record_llm_spend(
                UID,
                "score_final",
                provider="openai",
                model="gpt-4o-mini",
                usage={"input": 1000},
            )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(record, range(12)))
    with get_session() as session:
        rows = session.exec(select(LlmSpend).where(LlmSpend.user_id == UID)).all()
        assert sum(row.calls for row in rows) == 37
        assert sum(row.input_tokens for row in rows) == 37000


def test_buffer_charges_the_utc_day_the_call_happened(monkeypatch):
    clock = [date(2026, 9, 17)]

    class Clock(date):
        @classmethod
        def today(cls):
            return clock[0]

    monkeypatch.setattr(spend, "date", Clock)
    monkeypatch.setattr(spend, "_utc_day", lambda: clock[0], raising=False)
    buffer = spend.SpendBuffer()
    buffer.add(UID, "score_final", calls=2)
    clock[0] = date(2026, 9, 18)
    buffer.add(UID, "score_final", calls=3)
    buffer.flush()
    with get_session() as session:
        rows = session.exec(select(LlmSpend).where(LlmSpend.user_id == UID)).all()
        assert {row.day: row.calls for row in rows} == {
            date(2026, 9, 17): 2,
            date(2026, 9, 18): 3,
        }


def test_switching_to_live_keys_does_not_turn_a_sandbox_payment_into_revenue(
    monkeypatch,
):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_fake")
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_fake")
    deliver(monkeypatch, checkout_event())
    with get_session() as session:
        row = session.exec(
            select(UserSubscription).where(UserSubscription.user_id == UID)
        ).one()
        assert billing.is_paid_entitlement(row) is False


def test_unverified_legacy_stripe_row_is_not_assumed_to_be_live(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_fake")
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_fake")
    row = UserSubscription(
        user_id=UID,
        plan=PlanTier.PRO,
        stripe_subscription_id="sub_unknown",
        current_period_end=datetime.utcnow() + timedelta(days=30),
    )
    assert billing.is_paid_entitlement(row) is False


def test_storage_cleanup_deletes_all_pages_and_nested_files():
    class Storage:
        def __init__(self):
            self.removed = []

        def list(self, prefix, options):
            assert not self.removed, (
                "enumerate before deleting to avoid shifted offsets"
            )
            entries = (
                (
                    [{"name": f"{i:03}.pdf", "id": str(i)} for i in range(205)]
                    + [{"name": "nested", "id": None}]
                )
                if prefix == UID
                else [{"name": "resume.pdf", "id": "nested-file"}]
            )
            offset = options["offset"]
            # A gateway may clamp the requested page length.
            return entries[offset : offset + 60]

        def remove(self, paths):
            assert len(paths) <= 100
            self.removed.extend(paths)

    buckets = {name: Storage() for name in account_purge.STORAGE_BUCKETS}
    sb = SimpleNamespace(storage=SimpleNamespace(from_=lambda name: buckets[name]))
    assert all(account_purge.purge_user_storage(UID, sb).values())
    for bucket in buckets.values():
        assert len(bucket.removed) == 206
        assert f"{UID}/nested/resume.pdf" in bucket.removed
