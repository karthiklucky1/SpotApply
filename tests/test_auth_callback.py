"""The OAuth callback page must never be able to hang.

Production, 2026-09-04 17:01-17:04 UTC. Google sign-in worked perfectly every
time — `/auth/callback` 200 in 211ms, `/api/profile` 200 in 1.3s — and the user
still sat on "Signing you in…" for minutes. The callback's last act is
`window.location.href = '/dashboard'`, and `/dashboard` was exceeding the
database statement timeout (Railway HTTP log: 499 after 48.8s, then 103.9s,
never a 200). A browser keeps painting the CURRENT document until the next one
arrives, so a dashboard that never answers presents as a sign-in page frozen
forever — with no error, no retry, and no code of ours even running.

Nothing about that is visible from the server side, and no unit test of the
handler catches it, so these are structural guards on the page itself. They
encode one rule: **every wait in the sign-in path is bounded, and every outcome
ends on a screen that tells the user what happened.** All of them fail against
the pre-fix template.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.api.server import app


@pytest.fixture(scope="module")
def page() -> str:
    return TestClient(app).get("/auth/callback").text


@pytest.fixture(scope="module")
def script(page: str) -> str:
    """The page's module script — where the whole sign-in flow lives."""
    blocks = re.findall(r"<script[^>]*type=\"module\"[^>]*>(.*?)</script>", page, re.DOTALL)
    assert len(blocks) == 1, f"expected exactly one module script, found {len(blocks)}"
    return blocks[0]


def test_the_callback_page_renders(page: str):
    assert "Signing you in" in page


def test_every_fetch_in_the_sign_in_path_is_bounded(script: str):
    """An unbounded `await fetch(...)` in the sign-in path can hold the user on
    the spinner for as long as the server takes to answer. Both profile calls
    were unbounded before this; only the dashboard being slower hid it."""
    calls = list(re.finditer(r"fetch\(", script))
    assert calls, "no fetch() calls found — did the sign-in flow move?"
    for call in calls:
        # Take the balanced-ish window after each fetch( up to the call's end.
        tail = script[call.end():call.end() + 400]
        assert "signal:" in tail, (
            "a fetch() in the callback has no AbortController signal — an "
            f"unbounded wait in the sign-in path:\n...{tail[:160]}"
        )


def test_a_watchdog_can_end_every_wait(script: str):
    """Bounding the fetches is not enough: the final navigation to /dashboard
    is not a fetch and cannot be aborted, so a timer has to be able to speak
    over it. This is the guard that the actual production hang is covered."""
    assert re.search(r"setTimeout\(\s*\(\)\s*=>\s*stuck\(", script), (
        "no watchdog that surfaces the stuck state — a slow /dashboard would "
        "again present as an endless 'Signing you in…'"
    )
    assert "NAV_MS" in script and "window.location.href = '/dashboard'" in script, (
        "the navigation to /dashboard must arm its own timeout"
    )


def test_the_page_has_a_visible_way_out(page: str):
    """A message the user can act on, not just a spinner that stops moving."""
    assert 'id="stuck"' in page and "hidden" in page, "no fallback UI on the page"
    assert 'href="/dashboard"' in page, "no way to retry the dashboard"
    assert 'href="/login"' in page, "no way back to sign-in"


def test_a_provider_error_is_explained_not_swallowed(script: str):
    """Supabase/Google report failures in the URL — `#error=` (implicit) or
    `?error=` (PKCE). Both used to fall through to the silent no-session branch
    and bounce to /login with nothing said."""
    assert "location.hash" in script and "location.search" in script, (
        "the callback does not inspect the URL for a provider error"
    )
    assert re.search(r"get\(['\"]error['\"]\)", script), "no error parameter handling"
    assert re.search(r"error_description", script), "the provider's reason is discarded"


def test_a_failed_profile_bootstrap_still_signs_the_user_in(script: str):
    """Filling in a name from Google metadata is a convenience. It must never
    be able to fail a sign-in whose session is already stored."""
    body = script[script.index("onAuthStateChange"):]
    assert "goToDashboard()" in body, "the dashboard navigation left the session branch"
    catch_at = body.index("bootstrapping profile")
    nav_at = body.index("goToDashboard()")
    assert nav_at > catch_at, (
        "the navigation must sit AFTER the bootstrap's catch, so a failed or "
        "timed-out profile call still reaches the dashboard"
    )
