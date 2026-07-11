"""Favicon proxy: icon hits stream through, misses 204 (no console noise)."""
from __future__ import annotations

import pytest


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400  # >200 bytes, real-logo-sized


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


class FakeResp:
    def __init__(self, status_code=200, content=b"", ctype="image/png"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": ctype}


def test_favicon_hit_and_cache(client, monkeypatch):
    import app.api.server as srv
    srv._FAVICON_CACHE.clear()
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return FakeResp(200, PNG)

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    r = client.get("/api/favicon", params={"domain": "github.com"})
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["cache-control"].startswith("public")
    # Clearbit (tried first) served it, so only ONE upstream call, not two.
    assert calls["n"] == 1
    # Second request served from cache — no new upstream fetch
    r2 = client.get("/api/favicon", params={"domain": "github.com"})
    assert r2.status_code == 200 and calls["n"] == 1


def test_clearbit_first_then_google_fallback(client, monkeypatch):
    """Clearbit miss (404) → Google favicon succeeds → real image returned."""
    import app.api.server as srv
    srv._FAVICON_CACHE.clear()
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        if "clearbit.com" in url:
            return FakeResp(404, b"", "text/html")
        return FakeResp(200, PNG)  # google favicon

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    r = client.get("/api/favicon", params={"domain": "smallco.com"})
    assert r.status_code == 200 and r.content == PNG
    assert any("clearbit.com" in u for u in seen)
    assert any("google.com/s2/favicons" in u for u in seen)


def test_tiny_placeholder_rejected(client, monkeypatch):
    """A sub-200-byte placeholder (Clearbit/Google default globe) is treated as
    a miss, so cards fall back to the letter avatar instead of a junk icon."""
    import app.api.server as srv
    srv._FAVICON_CACHE.clear()
    import httpx
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: FakeResp(200, b"\x89PNG" + b"\x00" * 20, "image/png"))
    r = client.get("/api/favicon", params={"domain": "unknownbrand.com"})
    assert r.status_code == 204


def test_favicon_miss_is_silent_204(client, monkeypatch):
    import app.api.server as srv
    srv._FAVICON_CACHE.clear()
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(404, b"Not Found", "text/html"))
    r = client.get("/api/favicon", params={"domain": "nofavicon-here.com"})
    assert r.status_code == 204
    assert r.content == b""


def test_favicon_rejects_garbage_domain(client):
    for bad in ("<script>", "a", "", "foo..bar", "x" * 200):
        r = client.get("/api/favicon", params={"domain": bad})
        assert r.status_code == 204, bad


def test_domain_guess_strips_career_suffixes():
    """ATS slug names like 'Bjakcareer' must resolve to the real company domain
    (bjak.com) so Clearbit finds a logo instead of the letter fallback."""
    from app.api.server import _company_domain_filter as f
    assert f("Bjakcareer") == "bjak.com"
    assert f("Acme Careers") == "acme.com"
    assert f("StripeJobs") == "stripe.com"
    assert f("Netflix") == "netflix.com"          # unchanged when no suffix
    assert f("Stealth Startup") == ""             # anonymous → no lookup
