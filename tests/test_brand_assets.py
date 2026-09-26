""""No logo in Chrome" that the developer cannot reproduce.

`/favicon.ico` returned the SVG FILE with `media_type="image/svg+xml"` — SVG
bytes, an SVG content type, at a `.ico` URL. Modern Chrome will often render
that anyway, which is exactly why it survived: the developer's browser had the
mark cached from a page visit and looked fine. The clients that trust the
extension over the content type — older Chrome, several crawlers, bookmark and
tab-restore surfaces — get an image they cannot decode and fall back to a blank
or generic icon.

So `/favicon.ico` now serves a real ICO (three PNG frames, 16/32/48) generated
FROM `favicon.svg` by `scripts/build_favicon_ico.py`, so the two marks cannot
drift apart. These tests fail if that file stops being a valid multi-size ICO,
if a route starts serving the wrong content type, or if a brand asset a page
references stops existing.

Everything here is checked through the real app, on paths a signed-out visitor
hits — no asset may require authentication.
"""
from __future__ import annotations

import pathlib
import struct

import pytest
from fastapi.testclient import TestClient

import app.api.server as server

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


# ── the .ico is a real .ico ──────────────────────────────────────────────────

def test_the_committed_ico_is_a_valid_multi_size_icon():
    data = (STATIC / "favicon.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0 and kind == 1, "not an ICO container"
    assert count >= 2, "a single-size icon defeats the point of an .ico"
    sizes = []
    for i in range(count):
        w, h, _c, _r, _planes, bpp, size, off = struct.unpack(
            "<BBBBHHII", data[6 + 16 * i: 22 + 16 * i])
        payload = data[off:off + size]
        assert payload[:8] == PNG_MAGIC, f"frame {w}x{h} is not a PNG"
        # The directory must not lie about the frame: a viewer that trusts it
        # and then decodes a different size renders a smear.
        png_w, png_h = struct.unpack(">II", payload[16:24])
        assert (png_w, png_h) == (w, h), f"directory says {w}x{h}, PNG is {png_w}x{png_h}"
        assert bpp == 32
        sizes.append(w)
    assert 16 in sizes and 32 in sizes, "16 and 32 are the sizes browsers ask for"


def test_the_ico_is_small_enough_to_serve_uncompressed():
    assert (STATIC / "favicon.ico").stat().st_size < 64 * 1024


def test_the_ico_is_generated_from_the_svg():
    """Two marks that can drift apart will. The generator is committed and the
    route's docstring points at it."""
    import inspect
    src = inspect.getsource(server.serve_favicon_ico)
    assert "build_favicon_ico" in src
    assert (ROOT / "scripts" / "build_favicon_ico.py").exists()


# ── the routes ───────────────────────────────────────────────────────────────

def test_favicon_ico_serves_icon_bytes_with_an_icon_content_type(client):
    """THE DEFECT. It served SVG bytes under image/svg+xml at a .ico URL."""
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/x-icon")
    assert r.content[:4] == b"\x00\x00\x01\x00", "not ICO bytes"
    assert b"<svg" not in r.content[:200]


def test_favicon_svg_still_serves_svg(client):
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content


@pytest.mark.parametrize("path", ["/favicon.ico", "/favicon.svg", "/robots.txt",
                                  "/sitemap.xml"])
def test_root_assets_need_no_authentication(client, path):
    """A crawler and a signed-out visitor must both get the asset. An icon behind
    auth is an icon nobody outside the app ever sees."""
    r = client.get(path)
    assert r.status_code == 200
    assert r.content


@pytest.mark.parametrize("path", ["/favicon.ico", "/favicon.svg"])
def test_the_icons_are_cacheable(client, path):
    """Without a cache header every tab open re-fetches the mark."""
    assert "max-age" in client.get(path).headers.get("cache-control", "")


def test_a_missing_ico_falls_back_rather_than_500ing(client, monkeypatch, tmp_path):
    """An icon must never be able to error a page."""
    import os
    real = os.path.exists
    monkeypatch.setattr(os.path, "exists",
                        lambda p: False if str(p).endswith("favicon.ico") else real(p))
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert b"<svg" in r.content


# ── the assets every page references actually exist ──────────────────────────

@pytest.mark.parametrize("asset", ["favicon.svg", "favicon.ico", "icon-192.svg",
                                   "icon-512.svg", "manifest.json", "og-card.png"])
def test_referenced_brand_assets_exist(asset):
    assert (STATIC / asset).exists(), f"{asset} is referenced but not on disk"


def test_every_manifest_icon_is_on_disk():
    """An install icon that 404s makes the PWA install prompt fail silently."""
    import json
    manifest = json.loads((STATIC / "manifest.json").read_text())
    assert manifest["icons"], "a manifest with no icons cannot be installed"
    for icon in manifest["icons"]:
        src = icon["src"]
        assert src.startswith("/static/"), f"{src} is not served by the static mount"
        assert (STATIC / src[len("/static/"):]).exists(), f"{src} missing on disk"


def test_manifest_and_social_card_are_served(client):
    for path in ("/static/manifest.json", "/static/og-card.png",
                 "/static/icon-192.svg", "/static/icon-512.svg"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert r.content


def test_static_paths_are_case_exact():
    """A path that works on a case-insensitive laptop and 404s on Linux is the
    classic "works for me" asset bug."""
    import json
    manifest = json.loads((STATIC / "manifest.json").read_text())
    on_disk = {p.name for p in STATIC.iterdir()}
    for icon in manifest["icons"]:
        name = icon["src"].rsplit("/", 1)[-1]
        assert name in on_disk, f"{name} differs in case from what is on disk"


# ── dates and footers ────────────────────────────────────────────────────────

def test_the_privacy_policy_declares_the_work_authorization_data_we_store():
    """The product stores work_authorization, visa_status, ead_end_date,
    requires_sponsorship and stem_opt. The policy named none of it."""
    html = (ROOT / "app" / "templates" / "privacy.html").read_text()
    assert "Work-authorisation and preference data" in html
    for claim in ("work-authorisation status", "EAD end date", "sponsorship",
                  "relocate to"):
        assert claim in html
    # And it must not over-promise: the fields are user-entered, not inferred.
    assert "none of them is inferred from your resume" in html


def test_the_privacy_date_moved_with_the_revision():
    html = (ROOT / "app" / "templates" / "privacy.html").read_text()
    assert "Last updated: September 2026" in html


def test_the_terms_date_is_left_alone():
    """Phase 9: preserve genuinely historical effective dates. The terms body
    has not been substantively revised, so its date must not be touched."""
    html = (ROOT / "app" / "templates" / "terms.html").read_text()
    assert "Last updated: June 2025" in html


@pytest.mark.parametrize("page", ["landing.html", "pricing.html"])
def test_no_hard_coded_copyright_year_remains(page):
    """Correct the day it was typed, wrong every 1 January after."""
    html = (ROOT / "app" / "templates" / page).read_text()
    assert "© 2026" not in html and "&copy; 2026" not in html
    assert "{{ current_year }}" in html


def test_the_year_comes_from_the_server(client):
    from datetime import datetime
    year = str(datetime.utcnow().year)
    for path in ("/", "/pricing"):
        r = client.get(path)
        assert r.status_code == 200
        assert f"© {year}" in r.text, f"{path} does not render the served year"


# ── social metadata on every public page ─────────────────────────────────────

@pytest.mark.parametrize("page,path", [
    ("landing.html", "/"), ("pricing.html", "/pricing"),
    ("privacy.html", "/privacy"), ("terms.html", "/terms"),
])
def test_every_public_page_has_a_share_card_and_a_canonical(page, path):
    """Three of these had neither: a shared link rendered as a bare URL, and
    with no canonical the trailing-slash and query-string variants of one page
    can split indexing between them."""
    html = (ROOT / "app" / "templates" / page).read_text()
    for tag in ('rel="canonical"', "og:title", "og:description", "og:url",
                "og:image", "twitter:card"):
        assert tag in html, f"{page} is missing {tag}"
    assert f'href="https://app.spotapply.ai{path}"' in html


@pytest.mark.parametrize("path", ["/", "/pricing", "/privacy", "/terms"])
def test_the_share_image_each_page_points_at_is_actually_served(client, path):
    """An og:image that 404s is worse than none — the card renders broken."""
    import re
    html = client.get(path).text
    m = re.search(r'property="og:image" content="([^"]+)"', html)
    assert m, f"{path} has no og:image"
    served = client.get(m.group(1).replace("https://app.spotapply.ai", ""))
    assert served.status_code == 200 and served.content


# ── page weight ──────────────────────────────────────────────────────────────

def test_every_landing_screenshot_has_a_webp_beside_it():
    """The hero PNG was 305KB of a 362KB page — 86% of a first visit's transfer.
    Regenerate with `python -m scripts.build_webp_shots`."""
    shots = STATIC / "shots"
    pngs = sorted(shots.glob("*.png"))
    assert pngs, "the landing screenshots are missing"
    for png in pngs:
        webp = png.with_suffix(".webp")
        assert webp.exists(), f"{png.name} has no WebP — run scripts/build_webp_shots.py"
        assert webp.stat().st_size < png.stat().st_size, \
            f"{webp.name} is not smaller than its PNG; re-encoding gained nothing"
        assert webp.read_bytes()[:4] == b"RIFF", f"{webp.name} is not a WebP"


def test_the_hero_offers_webp_first_and_keeps_the_png_as_a_fallback():
    """Adding a format must never remove one: a browser that cannot decode WebP
    still has to get the original."""
    html = (ROOT / "app" / "templates" / "landing.html").read_text()
    assert html.count("<picture>") == html.count("</picture>") >= 2
    assert 'type="image/webp"' in html
    assert "/static/shots/qualified-board.webp" in html
    assert "/static/shots/qualified-board.png" in html, "the PNG fallback was dropped"
    # The LCP image keeps its priority hints and explicit dimensions.
    assert 'fetchpriority="high"' in html
    assert 'width="1278" height="700"' in html


def test_every_referenced_shot_is_actually_served(client):
    import re
    html = client.get("/").text
    found = set(re.findall(r'/static/shots/[\w-]+\.(?:png|webp|jpg|jpeg|avif)', html))
    assert found, "the landing page references no screenshots at all"
    for path in sorted(found):
        r = client.get(path)
        assert r.status_code == 200 and r.content, f"{path} -> {r.status_code}"


@pytest.mark.parametrize("page", ["pricing.html", "privacy.html", "terms.html"])
def test_cdn_pages_preconnect_to_the_origins_they_block_on(page):
    """These pages load a third-party stylesheet and the Tailwind CDN, both
    render-blocking, and neither had a preconnect — so the TLS handshake only
    started after the HTML was parsed."""
    html = (ROOT / "app" / "templates" / page).read_text()
    for origin in ("https://fonts.googleapis.com", "https://fonts.gstatic.com",
                   "https://cdn.tailwindcss.com"):
        assert f'rel="preconnect" href="{origin}"' in html, f"{page} lacks {origin}"


def test_every_page_links_the_shared_favicon_not_an_inline_copy():
    """AUDIT 2026-09-25 (finding 13): pricing, privacy, terms and the reset page
    carried an old inline data-URI icon, so the mark differed page to page and
    the generated /favicon.ico never reached them."""
    from pathlib import Path
    for page in Path("app/templates").glob("*.html"):
        html = page.read_text(encoding="utf-8")
        for line in html.splitlines():
            if 'rel="icon"' in line:
                assert "data:image" not in line, f"{page.name} inlines its favicon"
