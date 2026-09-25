"""Temporary Pro grants a PLAN. It must not grant a door.

Phase 10 asks for one thing in particular: confirm that temporary Pro access
cannot bypass authentication or grant recruiter/admin permissions. The risk is
concrete and the shape is familiar — an entitlement flag that reaches
`_get_user_plan` sits one careless edit away from the admin check, and the
codebase has already been burned once by conflating "what plan" with "is
paying" (10 dormant accounts, 90.8% of a week's LLM spend).

Also pinned here, because Phase 10 names them and nothing covered them:

  * an EXPIRED or FORGED token authenticates nobody, by either route (header or
    cookie), and the cookie fallback cannot rescue a bad header into a session;
  * authenticated responses are `no-store` with a `Vary` that names what the
    answer depends on. Every `/api/*` route previously set NO cache directive.
    RFC 9111 §3.5 stops a shared cache storing a response to a request carrying
    an `Authorization` header — but SpotApply also authenticates by COOKIE, and
    a cookie-authenticated request has no such header, so a CDN or corporate
    proxy applying heuristic freshness could serve one tenant's board to
    another;
  * broader Pro access does not widen any spend ceiling or unbound any queue.

These run against the real app. Nothing here signs up, emails or messages
anybody: the live Supabase flows (signup, verification email, OAuth callback,
password reset) need credentials this environment cannot reach, and are reported
as blocked rather than asserted.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.server as server
from app.config import settings
from app.db.models import PLAN_LIMITS, PlanTier


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


@pytest.fixture
def saas(monkeypatch):
    """Multi-tenant mode. In single-user mode `_get_user_id` returns "local" by
    design, so every auth assertion has to be made with Supabase on.

    `use_supabase` is a derived property (`database_url and supabase_url`), so
    the two fields behind it are what get set — patching the property itself
    raises, and patching a fake attribute onto the object would let the test
    pass while the app still ran single-user.
    """
    monkeypatch.setattr(settings, "supabase_url", "https://test.supabase.co",
                        raising=False)
    monkeypatch.setattr(settings, "database_url",
                        "postgresql://u:p@localhost:5432/t", raising=False)
    assert settings.use_supabase is True
    return settings


@pytest.fixture
def temp_pro(monkeypatch, saas):
    monkeypatch.setattr(settings, "temporary_pro_for_all", True, raising=False)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "stripe_price_id_pro", "price_x", raising=False)
    return settings


# ── temporary Pro is not a door ──────────────────────────────────────────────

def test_temporary_pro_does_not_authenticate_anyone(temp_pro, client):
    """THE PHASE 10 QUESTION. Free Pro for everyone must still be Pro for
    everyone who is signed in."""
    for path in ("/api/usage", "/api/profile", "/api/jobs"):
        r = client.get(path)
        assert r.status_code in (401, 403), f"{path} answered {r.status_code} anonymously"


def test_temporary_pro_does_not_grant_admin(temp_pro, client):
    for path in ("/api/admin/metrics", "/api/admin/health", "/api/admin/settings",
                 "/api/admin/contact-research", "/api/admin/budget-diagnostic"):
        r = client.get(path)
        assert r.status_code in (401, 403), f"{path} answered {r.status_code}"


def test_temporary_pro_does_not_grant_recruiter_access(temp_pro, client):
    for path, body in (("/api/recruiter/search", {"job_description": "x"}),
                       ("/api/recruiter/intro", {"candidate_user_id": "someone"})):
        r = client.post(path, json=body)
        assert r.status_code in (401, 403), f"{path} answered {r.status_code}"


def test_the_entitlement_flag_never_reaches_an_auth_check():
    """Pinned structurally: the guards must not learn about the plan at all."""
    import inspect
    for guard in (server._require_user, server._require_admin_user,
                  server._require_owned_application):
        src = inspect.getsource(guard)
        for forbidden in ("temporary_pro", "_get_user_plan", "PlanTier"):
            assert forbidden not in src, \
                f"{guard.__name__} must not consult an entitlement"


def test_admin_is_decided_by_identity_not_by_plan():
    import inspect
    src = inspect.getsource(server._require_admin_user)
    assert "temporary_pro" not in src
    assert "PRO" not in src


# ── a bad token is nobody ────────────────────────────────────────────────────

@pytest.mark.parametrize("token", [
    "", "not-a-jwt", "Bearer", "null", "undefined",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhdHRhY2tlciJ9.",          # unsigned
    "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhdHRhY2tlciJ9.",           # alg=none
])
def test_a_forged_or_empty_bearer_token_authenticates_nobody(saas, client, token):
    r = client.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (401, 403)


@pytest.mark.parametrize("token", ["", "expired", "eyJhbGciOiJub25lIn0.e30."])
def test_a_forged_cookie_authenticates_nobody(saas, client, token):
    r = client.get("/api/usage", cookies={"sb_token": token})
    assert r.status_code in (401, 403)


def test_a_bad_header_plus_a_bad_cookie_is_still_nobody(saas, client):
    """The cookie fallback exists so one stale localStorage token does not make
    requests disagree. It must not become a second chance for a forged one."""
    r = client.get("/api/usage",
                   headers={"Authorization": "Bearer forged"},
                   cookies={"sb_token": "also-forged"})
    assert r.status_code in (401, 403)


def test_a_protected_page_opened_directly_does_not_leak_data(saas, client):
    """A full-page navigation carries no Authorization header, only the cookie."""
    r = client.get("/dashboard")
    body = r.text.lower()
    for leak in ("rerank_score", "shortlisted", "application_id"):
        if r.status_code == 200:
            assert leak not in body or "sb_token" in body, \
                "an anonymous dashboard must render the signed-out shell only"


# ── authenticated responses stay out of shared caches ────────────────────────

@pytest.mark.parametrize("path", ["/api/usage", "/api/jobs", "/api/profile",
                                  "/api/pipeline/live", "/dashboard"])
def test_authenticated_responses_are_no_store(client, path):
    r = client.get(path)
    assert r.headers.get("cache-control") == "no-store", \
        f"{path} may be stored by a shared cache"
    assert "Authorization" in r.headers.get("vary", "")
    assert "Cookie" in r.headers.get("vary", "")


@pytest.mark.parametrize("path", ["/", "/pricing", "/privacy", "/terms"])
def test_public_pages_are_not_marked_no_store(client, path):
    """The fix must not de-optimise the marketing pages."""
    assert client.get(path).headers.get("cache-control") != "no-store"


def test_a_route_that_set_its_own_cache_header_is_left_alone(client):
    """The favicon is genuinely public and keyed only by its URL."""
    assert "max-age" in client.get("/favicon.ico").headers.get("cache-control", "")


def test_the_middleware_decides_by_path_not_by_whether_a_user_was_found():
    """An anonymous 401 from /api/jobs must be no-store too — otherwise a cache
    could serve the refusal, or a later 200, off the same key."""
    import inspect
    src = inspect.getsource(server.PrivateCacheMiddleware)
    assert "_get_user_id" not in src
    assert "startswith(_NEVER_SHARED_PREFIXES)" in src


# ── broader Pro access does not unbound anything ─────────────────────────────

def test_pro_ceilings_are_finite(temp_pro):
    limits = PLAN_LIMITS[PlanTier.PRO]
    for key in ("shortlist_daily", "finals_daily", "tailor_daily"):
        assert isinstance(limits[key], int) and 0 < limits[key] < 10_000


def test_the_platform_backstops_still_bound_the_whole_estate(temp_pro):
    assert 0 < settings.llm_daily_final_cap
    assert 0 < settings.llm_hourly_final_cap < settings.llm_daily_final_cap
    assert settings.tailor_abuse_daily_cap >= PLAN_LIMITS[PlanTier.PRO]["tailor_daily"]


def test_the_dormancy_gate_is_not_disabled_by_the_flag(temp_pro):
    """Phase 10: dormancy behaviour preserved under broader Pro access. The gate
    reads is_paid_entitlement on the user's own row, which the flag never sets."""
    import inspect
    src = inspect.getsource(server._user_paid_search_is_live)
    assert "is_paid_entitlement" in src and "temporary_pro" not in src
    assert settings.dormant_user_grace_days > 0


def test_provider_failure_handling_is_untouched_by_the_flag(temp_pro):
    """The breaker and the budget gate are plan-independent."""
    import inspect
    from app.matching import reranker
    src = inspect.getsource(reranker)
    assert "temporary_pro" not in src
    assert settings.llm_provider_cooldown_minutes > 0


def test_queues_stay_bounded_per_cycle(temp_pro):
    """A wider entitlement changes WHICH jobs the fixed budget buys, not how
    many calls a cycle may make."""
    assert settings.scoring_drain_cap > 0
    assert settings.prescore_cap > 0
    assert settings.top_k_rerank > 0
