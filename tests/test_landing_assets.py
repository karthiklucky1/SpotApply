"""Compiled landing assets stay wired and in sync with the template.

The landing page uses a COMMITTED compiled Tailwind stylesheet
(app/static/tailwind-landing.css) plus self-hosted AOS instead of the Play CDN
and unpkg. After editing Tailwind classes in app/templates/landing.html,
rebuild with `npm run build` — these tests catch a missing or stale build.
Only the landing page is compiled; the dashboard stays on the CDN because it
builds class strings dynamically in JS.
"""
import re
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
    # Re-pinned for the 2026-09 landing rebuild. Every entry must be a class the
    # CURRENT template really uses, or the guard stops detecting a stale
    # stylesheet and just fails forever. `.text-4xl` and `.sm\:col-span-2` went
    # with the old hero demo; the heading sizes are now set by the page's own
    # `.h2` clamp() rather than by Tailwind size utilities. `.lg\:grid-cols-2`
    # went with the paired screenshot frames, now a single centred figure.
    used = set(re.findall(r'class="([^"]+)"', LANDING))
    for cls in (".text-xs", ".rounded-2xl", ".backdrop-blur-sm",
                r".sm\:grid-cols-5", r".lg\:col-span-4", r".lg\:col-span-8",
                r".lg\:grid-cols-12", r".sm\:flex-row", r".lg\:grid-cols-3",
                r".lg\:grid-cols-4"):
        plain = cls.lstrip(".").replace("\\", "")
        assert any(plain in group.split() for group in used), (
            f"{cls} is pinned here but the template no longer uses it — "
            f"re-pin this list against the current landing page")
        assert cls in css, f"{cls} missing from compiled CSS — run `npm run build`"


def test_dashboard_css_covers_js_built_classes():
    """The dashboard stylesheet is scanned from the template (npm run build).
    These were the classes the stale hand-built file silently dropped."""
    css = (ROOT / "app" / "static" / "tailwind.css").read_text()
    for cls in (r".bg-rose-500\/15", r".border-rose-500\/25",
                r".hover\:bg-slate-700", ".text-slate-300"):
        assert cls in css, f"{cls} missing — run `npm run build`"
    dash = (ROOT / "app" / "templates" / "dashboard.html").read_text()
    for phantom in ("slate-850", "slate-750", "slate-350"):
        assert phantom not in dash, f"nonexistent Tailwind class {phantom} reintroduced"
