"""Every route is consciously public or consciously guarded — no third option.

Two unauthenticated cross-tenant leaks were found by hand-reviewing routes, in
the same file, on the same day:

  * GET  /api/resume/file       served the master résumé to anonymous callers
  * DELETE /run/discovery       cancelled any tenant's discovery run
  * GET  /api/discovery/last-run returned the newest DiscoveryRun of ANY tenant

All three shared one shape. The handler reads `_get_user_id(request)`, which
returns None when unauthenticated, and then scopes with

    if uid and uid != "local":
        q = q.where(Model.user_id == uid)

That condition **fails open**: `uid is None` skips the filter entirely, so the
query runs unscoped across all tenants. It is only safe when the handler has
already refused the anonymous case.

Hand-review does not scale to 139 routes and missed the third instance twice.
This test enumerates the app's own route table instead, so the only way to add
an unguarded route is to add its path to PUBLIC_PATHS on purpose.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute

from app.api import server

# Anything that must answer before login. Adding a path here is a deliberate,
# reviewable decision that this endpoint is safe to serve to the whole internet.
PUBLIC_PATHS = {
    # Marketing / legal pages, crawler files, static assets, liveness probe.
    "/", "/login", "/pricing", "/privacy", "/terms", "/extension",
    "/auth/callback", "/auth/reset",
    "/robots.txt", "/sitemap.xml", "/favicon.ico", "/api/favicon", "/favicon.svg",
    "/health",
    # Server-rendered page SHELLS. These return HTML only; every byte of tenant
    # data on them is fetched client-side from the guarded APIs below. /dashboard
    # is the one that also renders data server-side, and it wraps that entire
    # block in `if not (settings.use_supabase and not uid)` — fail closed.
    "/dashboard", "/admin", "/messages", "/recruiter", "/u/{handle}",
    # Deliberately anonymous top-of-funnel: the free job check and demo match are
    # the landing page's live demos, and featured reviews are its testimonials.
    "/api/public/job-check", "/api/public/demo-match", "/api/public/freshness",
    "/api/reviews", "/api/extension/download", "/api/billing/options",
    # Stripe calls this one with no session. It must NOT require a user — it
    # authenticates by HMAC signature instead (see tests/test_billing.py).
    "/api/billing/webhook",
    # Answer only about the caller: "am I logged in / am I an admin". Both return
    # a safe negative for anonymous callers rather than reading tenant rows.
    "/api/account", "/api/admin/whoami",
    # Clearing your own cookie needs no session to clear.
    "/auth/logout",
}

# Guard helpers that raise (401/403/404) rather than degrading to "no filter".
GUARD_NAMES = (
    "_require_user",
    "_require_user_id",
    "_require_owned_application",
    "_require_admin_user",
    "_require_admin",
)


def _api_routes():
    return [r for r in server.app.routes if isinstance(r, APIRoute)]


def _methods(route: APIRoute) -> str:
    return ",".join(sorted(m for m in route.methods if m not in ("HEAD", "OPTIONS")))


def _source(route: APIRoute) -> str:
    try:
        return inspect.getsource(route.endpoint)
    except (OSError, TypeError):  # pragma: no cover - source always available here
        return ""


def _is_guarded(src: str) -> bool:
    """True when the handler refuses the anonymous case instead of falling through."""
    if any(f"{g}(" in src for g in GUARD_NAMES):
        return True
    # Hand-rolled equivalent, e.g. `if settings.use_supabase and not uid: raise 401`
    return "status_code=401" in src or "status_code=403" in src


def _handles_anonymous(src: str) -> bool:
    """The handler branches on the anonymous case explicitly, so the fail-open
    scoping below it is unreachable while unauthenticated. Weaker than a guard —
    it may return an empty/negative result rather than a 401 — but it is a
    conscious decision, not a fall-through."""
    return "use_supabase and not uid" in src


def _ids(routes):
    return [f"{_methods(r)} {r.path}" for r in routes]


_PRIVATE = [r for r in _api_routes() if r.path not in PUBLIC_PATHS]


def test_the_route_table_is_actually_populated():
    """Guard the guard: an import failure that empties app.routes must not make
    every assertion below vacuously pass."""
    assert len(_api_routes()) > 100, (
        f"only {len(_api_routes())} APIRoutes registered — server.py did not "
        f"import fully, so this file is testing nothing"
    )


@pytest.mark.parametrize("route", _PRIVATE, ids=_ids(_PRIVATE))
def test_every_non_public_route_refuses_anonymous_callers(route):
    src = _source(route)
    assert _is_guarded(src), (
        f"{_methods(route)} {route.path} ({route.endpoint.__name__}) neither calls "
        f"a guard from {GUARD_NAMES} nor raises 401/403. If it is meant to be "
        f"reachable without a session, add its path to PUBLIC_PATHS with a reason; "
        f"otherwise use _require_user(request) — note that _get_user_id returns "
        f"None for anonymous callers and `if uid and uid != 'local'` then skips "
        f"tenant scoping entirely, serving every tenant's rows."
    )


@pytest.mark.parametrize("route", _PRIVATE, ids=_ids(_PRIVATE))
def test_fail_open_scoping_is_always_preceded_by_a_refusal(route):
    """The specific defect: `if uid and uid != "local"` in a handler that never
    refused the anonymous case. Reading a uid at all is fine; reading one and
    then optionally scoping on it is only fine after a 401."""
    src = _source(route)
    if "_get_user_id(" not in src:
        return
    fail_open = 'uid and uid != "local"' in src or "uid and uid != 'local'" in src
    if not fail_open:
        return
    assert _is_guarded(src) or _handles_anonymous(src), (
        f"{_methods(route)} {route.path} ({route.endpoint.__name__}) scopes with "
        f"`if uid and uid != \"local\"` but never rejects an unauthenticated "
        f"caller — uid=None skips the filter and the query returns every "
        f"tenant's rows."
    )


def test_public_paths_all_exist():
    """A stale allowlist silently exempts nothing — but it also hides typos that
    would otherwise be caught the moment a real path is renamed."""
    known = {r.path for r in _api_routes()}
    missing = PUBLIC_PATHS - known
    assert not missing, (
        f"PUBLIC_PATHS lists paths that no longer exist: {sorted(missing)}. "
        f"Remove them so the allowlist keeps meaning what it says."
    )
