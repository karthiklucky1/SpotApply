"""Canonical-host redirect: alternate hostnames 301 to the canonical host.

Deliberately SETTINGS-DRIVEN rather than hardcoding hostnames. These tests used
to assert `app.spotapply.ai` while config had since moved to
`canonical_host="spotapply.ai"` (www → apex), so four of them failed
permanently while indicating no real defect — a stale test is worse than no
test. They now pin the MECHANISM, which is what can actually break: an auth
callback losing its ?code= would break login on the alternate domain, and a
canonical host that redirects to itself would be an infinite loop.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import settings


@pytest.fixture(scope="module")
def client():
    from app.api.server import app
    return TestClient(app, follow_redirects=False)


def _redirect_hosts() -> list[str]:
    raw = settings.canonical_redirect_hosts or ""
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


pytestmark = pytest.mark.skipif(
    not (settings.canonical_host and _redirect_hosts()),
    reason="canonical redirect not configured",
)


def test_alternate_hosts_redirect_to_canonical(client):
    for host in _redirect_hosts():
        r = client.get("/pricing", headers={"host": host})
        assert r.status_code == 301, f"{host} should 301 to the canonical host"
        assert r.headers["location"] == f"https://{settings.canonical_host}/pricing"


def test_query_string_preserved(client):
    """An auth callback must keep ?code=/?state= or login breaks on that host."""
    host = _redirect_hosts()[0]
    r = client.get("/auth/callback?code=abc&state=xyz", headers={"host": host})
    assert r.status_code == 301
    assert r.headers["location"] == (
        f"https://{settings.canonical_host}/auth/callback?code=abc&state=xyz"
    )


def test_host_with_port_still_redirects(client):
    host = _redirect_hosts()[0]
    r = client.get("/", headers={"host": f"{host}:443"})
    assert r.status_code == 301
    assert r.headers["location"] == f"https://{settings.canonical_host}/"


def test_canonical_host_not_redirected(client):
    """No redirect loop: the canonical host itself serves directly."""
    r = client.get("/pricing", headers={"host": settings.canonical_host})
    assert r.status_code != 301


def test_localhost_not_redirected(client):
    r = client.get("/pricing", headers={"host": "localhost"})
    assert r.status_code != 301
