"""Regression: a scheme-less SUPABASE_URL must not break auth, and the favicon
must be served at the canonical paths.

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
    from fastapi.testclient import TestClient
    from app.api.server import app
    c = TestClient(app)
    for path in ("/favicon.ico", "/favicon.svg"):
        r = c.get(path)
        assert r.status_code == 200
        assert "svg" in r.headers.get("content-type", "")
        assert b"<svg" in r.content
