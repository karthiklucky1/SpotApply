"""Every way to get the extension leads to its Chrome Web Store listing.

The extension was approved on the Store (2026-10). Before, every install
surface sent people to a .zip and Chrome's Developer-mode "Load unpacked"
flow. One setting (`chrome_web_store_url`) is the only link; the .zip stays as
a developer fallback on /extension. Two copies (Store + unpacked) both fill a
form, so the install page tells .zip users to remove the old copy.

Also pinned: a Store link is not a job. The dashboard's "did you apply?"
prompt captures every external link clicked after a job was opened, so without
an exclusion, adding the extension right after viewing a job asked the user
on return whether they had applied to it.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings, settings

ROOT = Path(__file__).resolve().parent.parent
DASH = (ROOT / "app/templates/dashboard.html").read_text()
EXT = (ROOT / "app/templates/extension.html").read_text()
STORE = "https://chromewebstore.google.com/detail/acmmmmipgplcigghmcafkmjlenflbgcp"


def test_one_store_link_without_tracking_params():
    assert Settings.model_fields["chrome_web_store_url"].default == STORE
    assert "utm_" not in STORE


def test_the_install_page_leads_with_the_store():
    from fastapi.testclient import TestClient
    from app.api.server import app
    r = TestClient(app).get("/extension")
    assert r.status_code == 200
    html = r.text
    assert settings.chrome_web_store_url in html
    assert html.index("Add to Chrome") < html.index("/api/extension/download"), \
        "the .zip must be the fallback, not the first thing offered"
    assert "Installed the .zip version before?" in html     # two copies both fill
    assert "<details" in html and "Load unpacked" in html     # developer path kept


def test_the_dashboard_banner_and_settings_link_to_the_store():
    banner = DASH[DASH.index('id="ext-install-banner"'):]
    banner = banner[:banner.index("</div> <!-- Onboarding checklist")]
    assert 'href="{{ chrome_web_store_url }}"' in banner and 'rel="noopener"' in banner
    assert 'href="/extension"' not in banner
    settings_rows = DASH[DASH.index(">Browser Extension</p>"):][:900]
    assert "{{ chrome_web_store_url }}" in settings_rows
    assert "/api/extension/download" not in settings_rows
    assert "const CHROME_WEB_STORE_URL = {{ chrome_web_store_url|tojson }};" in DASH


def test_a_store_link_never_asks_did_you_apply():
    i = DASH.index("Capture clicks on \"open posting / apply\" links")
    capture = DASH[i:i + 2400]
    assert "data-no-apply-track" in capture and "chromewebstore.google.com" in capture
    # Every Store link we render carries the marker too.
    for m in re.finditer(r"<a [^>]*chrome_web_store_url[^>]*>", DASH):
        assert "data-no-apply-track" in m.group(0), m.group(0)


def test_no_claims_the_extension_does_not_make():
    """The extension is hands-off on LinkedIn and Indeed (content.js), and the
    listing is public now: the banner and the tour must not claim them."""
    banner = DASH[DASH.index('id="ext-install-banner"'):][:1400]
    assert "LinkedIn" not in banner and "Indeed" not in banner
    tour = DASH[DASH.index("'#ext-install-banner', title:"):][:400]
    assert "LinkedIn" not in tour and "Chrome Web Store" in tour


def test_the_store_page_is_also_the_install_route_on_the_public_list():
    """/extension stays public (no sign-in to read install steps)."""
    from tests.test_route_auth_inventory import PUBLIC_PATHS
    assert "/extension" in PUBLIC_PATHS
    assert "Add SpotApply to Chrome" in EXT
