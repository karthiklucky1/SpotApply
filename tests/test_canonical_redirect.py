"""Canonical-host redirect: apex/www → app.spotapply.ai with real TLS via Railway."""
import pytest
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.api.server import app
    return TestClient(app, follow_redirects=False)


def test_apex_redirects_to_canonical(client):
    r = client.get("/pricing", headers={"host": "spotapply.ai"})
    assert r.status_code == 301
    assert r.headers["location"] == "https://app.spotapply.ai/pricing"


def test_www_redirects_to_canonical(client):
    r = client.get("/", headers={"host": "www.spotapply.ai"})
    assert r.status_code == 301
    assert r.headers["location"] == "https://app.spotapply.ai/"


def test_query_string_preserved(client):
    r = client.get("/auth/callback?code=abc&state=xyz", headers={"host": "spotapply.ai"})
    assert r.status_code == 301
    assert r.headers["location"] == "https://app.spotapply.ai/auth/callback?code=abc&state=xyz"


def test_host_with_port_still_redirects(client):
    r = client.get("/", headers={"host": "spotapply.ai:443"})
    assert r.status_code == 301


def test_canonical_host_not_redirected(client):
    r = client.get("/health", headers={"host": "app.spotapply.ai"})
    assert r.status_code != 301


def test_localhost_not_redirected(client):
    r = client.get("/health", headers={"host": "localhost"})
    assert r.status_code != 301
