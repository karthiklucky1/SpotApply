"""browser_client routing: remote service vs local Playwright, and the failure modes.

The whole value of this module is that flipping BROWSER_SERVICE_URL moves
Chromium's ~400MB out of the web container with no code change. These tests pin
the routing decisions and — more importantly — the four failure modes, because
getting those wrong either silently reintroduces a local browser (the OOM) or
turns a transient service blip into broken discovery.
"""
from __future__ import annotations

import asyncio

import pytest

from app.common import browser_client as bc
from app.config import settings


@pytest.fixture(autouse=True)
def _restore():
    url = settings.browser_service_url
    token = settings.browser_service_token
    fallback = settings.browser_service_fallback_local
    original_local = bc._local_render
    yield
    settings.browser_service_url = url
    settings.browser_service_token = token
    settings.browser_service_fallback_local = fallback
    bc._local_render = original_local


def _stub_local(result=("local text", ["http://local/a"])):
    """Replace the local renderer and record whether it ran."""
    calls = []

    async def fake(url, **kw):
        calls.append((url, kw))
        return result

    bc._local_render = fake
    return calls


def _stub_remote(payload):
    """Replace the HTTP call. payload=None means 'service did not answer'."""
    calls = []

    async def fake(path, body, timeout_ms):
        calls.append((path, body))
        return payload

    bc._remote = fake
    return calls


# ── routing ──────────────────────────────────────────────────────────────────

def test_no_url_configured_uses_local():
    settings.browser_service_url = ""
    assert bc.remote_enabled() is False
    local = _stub_local()
    text = asyncio.run(bc.render_text("http://x/y"))
    assert text == "local text"
    assert len(local) == 1


def test_url_configured_uses_remote_and_skips_local_entirely():
    """The point of the split: with the service on, no browser is launched here."""
    settings.browser_service_url = "http://svc:8080"
    local = _stub_local()
    remote = _stub_remote({"ok": True, "text": "remote text", "links": []})

    text = asyncio.run(bc.render_text("http://x/y"))

    assert text == "remote text"
    assert local == [], "a local browser was launched despite the service being configured"
    assert remote[0][0] == "/render"


def test_render_request_carries_through_the_caller_options():
    settings.browser_service_url = "http://svc:8080"
    remote = _stub_remote({"ok": True, "text": "t", "links": []})
    asyncio.run(bc.render_text("http://x/y", wait_until="networkidle",
                               timeout_ms=12345, settle_ms=750))
    body = remote[0][1]
    assert body["url"] == "http://x/y"
    assert body["wait_until"] == "networkidle"
    assert body["timeout_ms"] == 12345
    assert body["settle_ms"] == 750
    assert body["extract"] == "text"


def test_render_links_asks_for_links():
    settings.browser_service_url = "http://svc:8080"
    remote = _stub_remote({"ok": True, "text": "", "links": ["http://a", "http://b"]})
    links = asyncio.run(bc.render_links("http://x/y"))
    assert links == ["http://a", "http://b"]
    assert remote[0][1]["extract"] == "links"


def test_search_hits_the_search_endpoint():
    settings.browser_service_url = "http://svc:8080"
    remote = _stub_remote({"ok": True, "links": ["http://r1"]})
    links = asyncio.run(bc.search_links("ml engineer", engine="bing"))
    assert links == ["http://r1"]
    path, body = remote[0]
    assert path == "/search"
    assert body["query"] == "ml engineer"
    assert body["engine"] == "bing"


# ── failure modes ────────────────────────────────────────────────────────────

def test_page_failure_does_not_fall_back_to_local():
    """The service rendered and the PAGE failed (dead link, bot wall). That is an
    authoritative answer — retrying locally burns 400MB to fail the same way."""
    settings.browser_service_url = "http://svc:8080"
    settings.browser_service_fallback_local = True
    local = _stub_local()
    _stub_remote({"ok": False, "error": "TimeoutError: navigation timeout"})

    with pytest.raises(RuntimeError, match="could not render"):
        asyncio.run(bc.render_text("http://x/y"))
    assert local == [], "a page-level failure incorrectly triggered a local render"


def test_service_unreachable_falls_back_when_allowed():
    """A service outage degrades discovery to 'slower and heavier', not broken."""
    settings.browser_service_url = "http://svc:8080"
    settings.browser_service_fallback_local = True
    local = _stub_local()
    _stub_remote(None)          # transport failure

    text = asyncio.run(bc.render_text("http://x/y"))
    assert text == "local text"
    assert len(local) == 1


def test_service_unreachable_raises_when_fallback_disabled():
    """On a container with no room for a local Chromium, failing is correct —
    falling back is exactly the OOM this whole change exists to prevent."""
    settings.browser_service_url = "http://svc:8080"
    settings.browser_service_fallback_local = False
    local = _stub_local()
    _stub_remote(None)

    with pytest.raises(RuntimeError, match="fallback is disabled"):
        asyncio.run(bc.render_text("http://x/y"))
    assert local == []


def test_search_degrades_to_empty_list_never_raises():
    """Callers treat [] as 'this source found nothing this pass'. A bot-walled or
    unreachable search engine must look the same, not blow up a discovery run."""
    settings.browser_service_url = "http://svc:8080"

    settings.browser_service_fallback_local = False
    _stub_remote(None)
    assert asyncio.run(bc.search_links("q")) == []

    _stub_remote({"ok": False, "error": "blocked"})
    assert asyncio.run(bc.search_links("q")) == []


def test_auth_header_only_sent_when_token_configured():
    settings.browser_service_token = ""
    assert bc._headers() == {}
    settings.browser_service_token = "s3cret"
    assert bc._headers() == {"Authorization": "Bearer s3cret"}


def test_endpoint_join_tolerates_trailing_slash():
    settings.browser_service_url = "http://svc:8080/"
    assert bc._endpoint("/render") == "http://svc:8080/render"
    settings.browser_service_url = "http://svc:8080"
    assert bc._endpoint("/render") == "http://svc:8080/render"
