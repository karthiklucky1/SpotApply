"""Quality and compliance guards for the auth pages and the four small public
pages (extension, recruiter, messages, public profile).

What these pin, and why:

* No third-party CDN on any of them. The Tailwind PLAY CDN (a dev tool that
  compiles in the browser, blocks render, and hands every visitor's IP address
  to a third party), jsDelivr for supabase-js, and Google Fonts are all
  replaced by files we serve: tailwind-public.css, a pinned
  /static/vendor/supabase-js-<version>.esm.js and /static/vendor/fonts.
* The "Start free" deep link (/login?mode=signup). `switchTab` used to be
  defined inside the Supabase module AFTER `await sb.auth.getSession()`, while
  the deep link ran on DOMContentLoaded — which a module's top-level await does
  not hold back — so every acquisition CTA landed on the Sign In tab with
  "window.switchTab is not a function". Tab switching is now plain DOM code in a
  classic script that runs before the module.
* Every form label is tied to its input (`for=`), and forms cancel the native
  submit before anything else, so a click before the module loads can neither
  reload the page nor throw.
* Each page has its own accurate meta description and the shared favicon set.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"

PAGES = ["auth.html", "auth_reset.html", "auth_callback.html", "extension.html",
         "recruiter.html", "messages.html", "public_profile.html"]
PLAY_CDN_MIGRATED = ["extension.html", "recruiter.html", "messages.html", "public_profile.html"]
SUPABASE_PAGES = ["auth.html", "auth_reset.html", "auth_callback.html"]
VENDORED_SUPABASE = "/static/vendor/supabase-js-2.117.2.esm.js"


def _html(page: str) -> str:
    return (TEMPLATES / page).read_text(encoding="utf-8")


def _scripts(html: str) -> list[tuple[str, str]]:
    """(attributes, body) for every inline <script> (no src)."""
    return [(a, b) for a, b in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S)
            if "src=" not in a]


# ── third-party hosts ────────────────────────────────────────────────────────

@pytest.mark.parametrize("page", PAGES)
def test_no_third_party_cdn(page):
    html = _html(page)
    for host in ("cdn.tailwindcss.com", "cdn.jsdelivr.net", "fonts.googleapis.com",
                 "fonts.gstatic.com", "unpkg.com"):
        assert host not in html, f"{page} still loads {host}"


@pytest.mark.parametrize("page", SUPABASE_PAGES)
def test_supabase_is_the_pinned_self_hosted_build(page):
    html = _html(page)
    assert f"from '{VENDORED_SUPABASE}'" in html
    assert (ROOT / "app" / VENDORED_SUPABASE.lstrip("/")).stat().st_size > 50_000


@pytest.mark.parametrize("page", PLAY_CDN_MIGRATED)
def test_migrated_pages_use_the_compiled_public_stylesheet(page):
    html = _html(page)
    assert '<link rel="stylesheet" href="/static/tailwind-public.css">' in html
    assert "tailwind.config" not in html, "inline Play-CDN config left behind"
    config = (ROOT / "tailwind.public.config.js").read_text()
    assert f'"./app/templates/{page}"' in config, \
        f"{page} is not scanned — its classes would be missing from tailwind-public.css"


def test_the_public_stylesheet_has_classes_only_the_migrated_pages_use():
    css = (ROOT / "app" / "static" / "tailwind-public.css").read_text()
    for cls in (r".md\:grid-cols-3", r".max-h-\[520px\]", r".border-amber-300",
                r".hover\:shadow-teal-500\/20", r".text-\[var\(--sage-800\)\]"):
        assert cls in css, f"{cls} missing — run `npm run build:css:public`"


# ── head metadata ────────────────────────────────────────────────────────────

def _description(html: str) -> str:
    m = re.search(r'<meta name="description" content="([^"]+)"', html)
    return m.group(1) if m else ""


@pytest.mark.parametrize("page", PAGES)
def test_meta_description_and_icons(page):
    html = _html(page)
    assert len(_description(html)) >= 40, f"{page} has no useful meta description"
    assert '<link rel="icon" href="/favicon.ico" sizes="any">' in html
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in html
    assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in html


def test_meta_descriptions_are_unique():
    descs = [_description(_html(p)) for p in PAGES]
    assert len(set(descs)) == len(descs), descs


@pytest.mark.parametrize("page,path", [("auth.html", "/login"), ("extension.html", "/extension")])
def test_share_card_and_canonical(page, path):
    html = _html(page)
    assert f'<link rel="canonical" href="https://app.spotapply.ai{path}">' in html
    for tag in ('property="og:title"', 'property="og:description"',
                'property="og:image" content="https://app.spotapply.ai/static/og-card.png"',
                'name="twitter:card" content="summary_large_image"'):
        assert tag in html, f"{page} is missing {tag}"
    assert (ROOT / "app" / "static" / "og-card.png").exists()


def test_public_profile_stays_out_of_search_indexes():
    assert '<meta name="robots" content="noindex, nofollow">' in _html("public_profile.html")


# ── sign-in page behaviour ───────────────────────────────────────────────────

def test_signup_deep_link_lives_in_a_classic_script_before_the_module():
    html = _html("auth.html")
    scripts = _scripts(html)
    classic = [(i, b) for i, (a, b) in enumerate(scripts) if 'type="module"' not in a]
    module = [(i, b) for i, (a, b) in enumerate(scripts) if 'type="module"' in a]
    assert len(module) == 1
    owner = [(i, b) for i, b in classic if "window.switchTab = function" in b]
    assert owner, "switchTab must be defined in a classic (non-module) script"
    i, body = owner[0]
    assert i < module[0][0], "the classic script must run before the module"
    assert re.search(r"get\('mode'\)\s*===\s*'signup'", body) and "#signup" in body
    assert "window.switchTab('signup')" in body
    # Never again only inside the module, and never on a DOMContentLoaded race.
    assert "switchTab =" not in module[0][1]
    for m in re.finditer(r"addEventListener\('DOMContentLoaded'", html):
        handler = html[m.end():html.index("});", m.end())]
        assert "switchTab" not in handler, "deep link is back on the DOMContentLoaded race"


def test_auth_forms_never_submit_natively():
    html = _html("auth.html")
    forms = re.findall(r"<form [^>]*>", html)
    assert len(forms) == 2
    for form in forms:
        m = re.search(r'onsubmit="([^"]+)"', form)
        assert m and m.group(1).startswith("event.preventDefault();"), form
    reset = re.search(r'<form id="reset-form"[^>]*>', _html("auth_reset.html")).group(0)
    assert 'onsubmit="event.preventDefault();' in reset


def test_show_msg_is_reachable_outside_the_module():
    """The saved-resume prompt called a `showMsg` that existed only inside the
    module scope — a ReferenceError from the classic script."""
    scripts = _scripts(_html("auth.html"))
    classic = "".join(b for a, b in scripts if 'type="module"' not in a)
    assert "window.showMsg = function" in classic
    for line in classic.splitlines():
        if re.search(r"(?<![.\w])showMsg\(", line):
            raise AssertionError(f"bare showMsg() in a classic script: {line.strip()}")


@pytest.mark.parametrize("page", ["auth.html", "auth_reset.html"])
def test_every_label_is_tied_to_an_existing_input(page):
    html = _html(page)
    labels = re.findall(r"<label\b([^>]*)>", html)
    assert labels
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for attrs in labels:
        m = re.search(r'\bfor="([^"]+)"', attrs)
        assert m, f"{page}: <label{attrs}> has no for="
        assert m.group(1) in ids, f"{page}: label for={m.group(1)} names no element"


def test_placeholder_only_fields_have_accessible_names():
    for page in ("recruiter.html", "messages.html"):
        for tag in re.findall(r"<(?:input|textarea)\b[^>]*>", _html(page)):
            assert "aria-label=" in tag, f"{page}: {tag}"


def test_demo_carousel_is_labelled_and_names_no_real_company():
    html = _html("auth.html")
    assert "Stripe" not in html
    assert "Northwind Labs" in html
    assert ">Example</span>" in html
    assert "spotapply.ai/simulation" not in html
    dots = re.findall(r'<[a-z]+ [^>]*class="sim-dot[^"]*"[^>]*>', html)
    assert len(dots) == 3 and all(d.startswith('<button type="button"') for d in dots)


def test_messages_controls_are_keyboard_operable():
    html = _html("messages.html")
    assert '<button type="button" class="convo-card' in html
    assert '<button type="button" class="star-btn' in html
    assert '<div class="convo-card' not in html


@pytest.mark.parametrize("page", ["auth.html", "auth_reset.html", "extension.html",
                                  "recruiter.html", "messages.html", "public_profile.html"])
def test_brand_links_home(page):
    assert re.search(r'<a href="/"[^>]*aria-label="SpotApply home"', _html(page))


@pytest.mark.parametrize("page", ["auth.html", "auth_reset.html", "extension.html", "recruiter.html"])
def test_legal_footer(page):
    html = _html(page)
    assert "© {{ current_year_now() }} SpotApply LLC" in html
    for href in ("/terms", "/privacy", "/refunds", "/cookies"):
        assert f'href="{href}"' in html, f"{page} footer misses {href}"


@pytest.mark.parametrize("page", ["auth.html", "auth_reset.html"])
def test_white_text_hover_and_success_colours_meet_contrast(page):
    html = _html(page)
    hover = re.search(r"\.hp-btn-primary:hover\s*\{[^}]*background:\s*var\(--sage-800\)", html)
    assert hover, "white on #1F9C8F is 3.4:1 — the hover must use --sage-800 (#0F6E66)"
    assert "--sage-800: #0F6E66" in html.replace("--sage-800:  #", "--sage-800: #")
    assert "color:var(--sage-700)" not in html
    assert ":focus-visible" in html


def test_rendered_pages_carry_the_served_year():
    from fastapi.testclient import TestClient
    from app.api.server import app
    client = TestClient(app)
    year = str(datetime.utcnow().year)
    for path in ("/login", "/login?mode=signup", "/auth/reset", "/extension", "/recruiter"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert f"© {year} SpotApply LLC" in r.text, path
