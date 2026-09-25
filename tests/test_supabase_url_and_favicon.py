"""Regression: a scheme-less SUPABASE_URL must not break auth, and each favicon
path must return the format its URL promises.

Root cause of a prod outage: SUPABASE_URL was set to 'auth.spotapply.ai' (no
https://). supabase-js threw 'Invalid supabaseUrl' client-side, aborting the
auth script before handleGoogle was defined (Google + email login dead), and
the server Storage client failed too (résumés stopped loading → zero scoring).
"""
import pytest

from app.config import Settings


@pytest.mark.parametrize("raw,expected", [
    ("auth.spotapply.ai", "https://auth.spotapply.ai"),
    ("abc.supabase.co", "https://abc.supabase.co"),
    ("https://abc.supabase.co/", "https://abc.supabase.co"),
    ("http://localhost:54321", "http://localhost:54321"),
    ("  abc.supabase.co  ", "https://abc.supabase.co"),
    ("", ""),
])
def test_supabase_url_normalized(raw, expected):
    assert Settings(supabase_url=raw).supabase_url == expected


def test_favicon_routes_served():
    """Both canonical paths answer — each with the format its URL promises.

    This test used to assert that BOTH paths returned SVG, which is what the
    single shared route did: SVG bytes under `image/svg+xml` at a `.ico` URL.
    That assertion encoded the defect. Clients that trust the extension over the
    content type (older Chrome, several crawlers, bookmark and tab-restore
    surfaces) cannot decode it and fall back to a blank icon, which is what the
    "no logo in Chrome" report was. See `tests/test_brand_assets.py` for the
    full surface-by-surface check.
    """
    from fastapi.testclient import TestClient
    from app.api.server import app
    c = TestClient(app)

    r = c.get("/favicon.svg")
    assert r.status_code == 200
    assert "svg" in r.headers.get("content-type", "")
    assert b"<svg" in r.content

    r = c.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/x-icon")
    assert r.content[:4] == b"\x00\x00\x01\x00", "a .ico URL must return ICO bytes"
