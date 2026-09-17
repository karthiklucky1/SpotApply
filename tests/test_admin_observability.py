"""/api/admin/settings and /api/admin/health — what the audit could not read.

The 2026-09-16 audit spent four hours against production and had to mark the
effective runtime settings UNAVAILABLE (the Railway variables page exposes
secret values, so it was off limits), could not tell which LLM provider was
serving finals (Anthropic had been rejecting every call for two days while the
ledger booked them to Claude), and had to infer that Stripe was in test mode
from a portal URL. These routes answer all three without a shell — and the
first test is the one that matters: no secret VALUE may ever appear in either
payload, whatever the field is called.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import server
from app.config import Settings, settings

_SECRETS = {
    "anthropic_api_key": "sk-ant-api03-VERYSECRET-A",
    "openai_api_key": "sk-proj-VERYSECRET-B",
    "stripe_secret_key": "sk_test_VERYSECRET-C",
    "stripe_webhook_secret": "whsec_VERYSECRET-D",
    "supabase_service_role_key": "eyJVERYSECRET-E.payload.sig",
    "supabase_anon_key": "eyJVERYSECRET-F.payload.sig",
    "database_url": "postgresql://postgres:VERYSECRET-G@db.example.supabase.co:5432/postgres",
    "payment_bank_details": "IBAN DE00 VERYSECRET-H",
    "admin_token": "VERYSECRET-I",
    "github_token": "ghp_VERYSECRET-J",
}


@pytest.fixture
def secrets_set(monkeypatch):
    for name, value in _SECRETS.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    # A Price id is not a secret, but stripe_enabled() needs it alongside the
    # key for the sk_test_ key above to read as "test" mode.
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_test_123", raising=False)
    return _SECRETS


def _restore_breaker(rr, saved) -> None:
    rr._provider_down_until.clear()
    rr._provider_down_until.update(saved[0])
    rr._provider_down_since.clear()
    rr._provider_down_since.update(saved[1])
    rr._provider_last_error.clear()
    rr._provider_last_error.update(saved[2])


@pytest.fixture
def client(monkeypatch):
    # SQLite mode: _require_admin_user returns "local-owner" without a token.
    monkeypatch.setattr(server, "_get_user_id", lambda request: "local")
    return TestClient(server.app)


# ── redaction ────────────────────────────────────────────────────────────────

def test_no_secret_value_appears_anywhere_in_the_settings_payload(secrets_set):
    out = server._redacted_settings()
    blob = json.dumps(out)
    for name, value in secrets_set.items():
        assert value not in blob, f"{name}'s value leaked into /api/admin/settings"
        # A secret that is SET reads as configured, one that is empty as not.
        assert out[name] == {"configured": True}, name
    assert "VERYSECRET" not in blob


def test_an_unset_secret_reads_as_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "", raising=False)
    assert server._redacted_settings()["stripe_secret_key"] == {"configured": False}


def test_non_secret_settings_are_verbatim():
    out = server._redacted_settings()
    assert out["dormant_user_grace_days"] == settings.dormant_user_grace_days
    assert out["scoring_model"] == settings.scoring_model
    assert out["pulse_max_boards_per_tick"] == settings.pulse_max_boards_per_tick
    assert out["shortlist_score_threshold"] == settings.shortlist_score_threshold


def test_a_credentialed_url_is_a_secret_whatever_the_field_is_called():
    assert server._looks_secret("some_endpoint", "postgresql://u:p@host/db")
    assert not server._looks_secret("some_endpoint", "https://api.example.com/v1")
    assert not server._looks_secret("supabase_url", "https://x.supabase.co")


def test_value_prefixes_that_are_always_keys_are_redacted():
    for v in ("sk_live_abc", "sk-ant-abc", "whsec_abc", "eyJhbGciOi"):
        assert server._looks_secret("mystery_field", v), v


def test_the_settings_route_answers_with_derived_flags(client, secrets_set):
    r = client.get("/api/admin/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["derived"]["stripe_mode"] == "test"      # sk_test_ key above
    assert body["stripe_secret_key"] == {"configured": True}
    assert "VERYSECRET" not in r.text


# ── health ───────────────────────────────────────────────────────────────────

def test_the_health_route_names_who_is_serving_and_never_a_secret(client, secrets_set):
    r = client.get("/api/admin/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body["providers"]) >= {"anthropic", "openai"}
    for p in ("anthropic", "openai"):
        assert "available" in body["providers"][p]
    assert body["billing"]["stripe_mode"] in ("off", "test", "live")
    assert body["dormancy"]["grace_days"] == settings.dormant_user_grace_days
    assert "scoring_cycle" in body["lanes"] and "pulse_tick" in body["lanes"]
    assert "generated_at" in body
    assert "VERYSECRET" not in r.text


def test_health_reports_a_tripped_breaker(client, monkeypatch):
    import app.matching.reranker as rr
    monkeypatch.setattr(settings, "llm_provider_cooldown_minutes", 30, raising=False)
    saved = (dict(rr._provider_down_until), dict(rr._provider_down_since),
             dict(rr._provider_last_error))
    try:
        rr._mark_provider_down("anthropic", "Your credit balance is too low sk-ant-hidden")
        body = client.get("/api/admin/health").json()
        a = body["providers"]["anthropic"]
        assert a["available"] is False
        assert a.get("down_since") is not None
        assert "sk-ant-hidden" not in json.dumps(body), "the error text must be sanitised"
    finally:
        _restore_breaker(rr, saved)


# ── the guard ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route", [server.admin_settings, server.admin_health])
def test_a_non_admin_is_refused(monkeypatch, route):
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    monkeypatch.setattr(server, "_get_user_email", lambda request: "someone@example.com")
    with pytest.raises(HTTPException) as ei:
        route(request=None)
    assert ei.value.status_code == 403


@pytest.mark.parametrize("route", [server.admin_settings, server.admin_health])
def test_an_anonymous_caller_is_refused(monkeypatch, route):
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    monkeypatch.setattr(server, "_get_user_email", lambda request: None)
    with pytest.raises(HTTPException) as ei:
        route(request=None)
    assert ei.value.status_code == 403
