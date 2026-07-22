"""Compiled landing assets stay wired and in sync with the template.

The landing page uses a COMMITTED compiled Tailwind stylesheet
(app/static/tailwind-landing.css) plus self-hosted AOS instead of the Play CDN
and unpkg. After editing Tailwind classes in app/templates/landing.html,
rebuild with `npm run build` — these tests catch a missing or stale build.
Only the landing page is compiled; the dashboard stays on the CDN because it
builds class strings dynamically in JS.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANDING = (ROOT / "app" / "templates" / "landing.html").read_text()


def test_landing_references_local_assets_not_cdns():
    assert "cdn.tailwindcss.com" not in LANDING
    assert "unpkg.com" not in LANDING
    assert "/static/tailwind-landing.css" in LANDING
    assert "/static/vendor/aos.css" in LANDING
    assert "/static/vendor/aos.js" in LANDING


def test_vendored_assets_exist():
    for rel in ("app/static/tailwind-landing.css",
                "app/static/vendor/aos.css",
                "app/static/vendor/aos.js"):
        p = ROOT / rel
        assert p.exists() and p.stat().st_size > 5_000, (
            f"{rel} missing/empty — run `npm run build` and commit the output")


def test_compiled_css_covers_landing_classes():
    css = (ROOT / "app" / "static" / "tailwind-landing.css").read_text()
    # Distinctive utilities the template actually uses (incl. recently added
    # responsive variants). A miss means the stylesheet wasn't rebuilt after a
    # template edit — the class would silently render unstyled in prod.
    for cls in (".text-4xl", ".rounded-2xl", ".backdrop-blur-sm",
                r".sm\:grid-cols-5", r".sm\:col-span-2", r".lg\:col-span-8",
                r".sm\:flex-row", r".sm\:text-xs", r".lg\:grid-cols-2"):
        assert cls in css, f"{cls} missing from compiled CSS — run `npm run build`"
