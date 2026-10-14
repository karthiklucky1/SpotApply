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
    # went with the paired screenshot frames, now a single centred figure, and
    # `.text-xs` / `.sm\:grid-cols-5` / `.lg\:grid-cols-3` went with the two
    # rows of matching cards that the hairline steps and the process rail
    # replaced.
    used = set(re.findall(r'class="([^"]+)"', LANDING))
    for cls in (".rounded-2xl", ".backdrop-blur-sm", ".grid-cols-3",
                r".lg\:col-span-4", r".lg\:col-span-5", r".lg\:col-span-7",
                r".lg\:col-span-8", r".lg\:grid-cols-12", r".sm\:flex-row",
                r".sm\:hidden"):
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


def _templates_reached_from(name: str, seen: set) -> set:
    """Every template `name` includes or imports, transitively."""
    text = (ROOT / "app" / "templates" / name).read_text()
    for dep in re.findall(r'\{%-?\s*(?:include|import|from|extends)\s+"([^"]+)"', text):
        if dep not in seen:
            seen.add(dep)
            _templates_reached_from(dep, seen)
    return seen


def test_dashboard_build_scans_every_template_the_dashboard_renders():
    """The board panes moved out of dashboard.html into _board_pane.html /
    _board_macros.html (2026-10-10) while the build kept scanning
    dashboard.html alone: the committed CSS still had the cards' classes only
    because it predated the move, and the next `npm run build` would have
    dropped every class only a card uses."""
    config = (ROOT / "tailwind.dashboard.config.js").read_text()
    globs = re.findall(r'"\./app/templates/([^"]+)"', config)
    reached = _templates_reached_from("dashboard.html", {"dashboard.html"})
    assert len(reached) > 1, "dashboard.html includes no template any more — re-check this guard"
    for tpl in reached:
        assert any(Path(tpl).match(g) for g in globs), (
            f"{tpl} is rendered by the dashboard but tailwind.dashboard.config.js does not scan it")
    css = (ROOT / "app" / "static" / "tailwind.css").read_text()
    # Used ONLY by the board templates (never by dashboard.html itself).
    for cls in (".line-clamp-2", ".opacity-60", r".text-\[8\.5px\]", r".px-0\.5"):
        assert cls in css, f"{cls} missing — the board templates were not scanned; run `npm run build`"
