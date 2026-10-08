"""The dashboard sidebar opens and collapses (2026-10-08, owner request).

Desktop: a toggle in the brand row narrows the 190px sidebar to an icon rail
and remembers it per browser. Four things are easy to get subtly wrong and are
pinned here, because nothing renders the dashboard in a browser in CI:

- the saved state is applied in <head>, BEFORE first paint — a script at the
  end of <body> would show the sidebar open and then snap it shut;
- every offset beside the sidebar follows ONE width (``--sb-w``) — the 190px
  was written in four places, and a missed one leaves a gap or an overlap;
- collapsed labels are hidden visually, never with display:none, so each
  button keeps its accessible name (the rail is icons only);
- phones keep the slide-in drawer: every collapsed rule is desktop-only.

And a phone bug found on the way: a nav tap closed the drawer from two
handlers, each a plain toggle, so the second one re-opened it.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "app/templates/dashboard.html").read_text()
HEAD = HTML[:HTML.index("</head>")]


def _function(name: str) -> str:
    m = re.search(r"(async\s+)?function\s+" + name + r"\s*\(", HTML)
    assert m, name
    depth, i = 0, HTML.index("{", m.end())
    for j in range(i, len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[j], 0)
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


def _media_block(query: str) -> str:
    """The body of the `@media (<query>) { ... }` block that holds the
    collapsed rules (the first one that mentions sb-collapsed)."""
    for m in re.finditer(r"@media\s*\(" + re.escape(query) + r"\)\s*\{", HTML):
        depth, i = 0, m.end() - 1
        for j in range(i, len(HTML)):
            depth += {"{": 1, "}": -1}.get(HTML[j], 0)
            if depth == 0:
                block = HTML[i:j + 1]
                if "sb-collapsed" in block:
                    return block
                break
    raise AssertionError(f"no @media ({query}) block with the collapsed rules")


def test_the_saved_state_is_restored_before_first_paint():
    script = re.search(r"<script>(.*?)</script>", HEAD, re.S)
    assert script and "hp_sb_collapsed" in script.group(1), \
        "the collapsed class must be set by a classic script in <head>"
    body = script.group(1)
    assert "document.documentElement.classList.add('sb-collapsed')" in body
    assert "try" in body and "catch" in body, "storage can throw (private window)"


def test_one_width_for_everything_beside_the_sidebar():
    assert re.search(r":root\s*\{\s*--sb-w:\s*190px;\s*\}", HTML)
    # The four offsets that used to hard-code it.
    assert re.search(r"#app-sidebar\s*\{[^}]*width:\s*var\(--sb-w\)", HTML)
    assert re.search(r"#main-wrapper\s*\{[^}]*margin-left:\s*var\(--sb-w\)", HTML)
    assert "#jobModalOverlay { left: var(--sb-w); }" in HTML
    assert 'id="top-glow-overlay" style="position:fixed;top:0;left:var(--sb-w);' in HTML
    # No sidebar offset spelled as a literal any more (the shortlist search
    # box's own 190px is unrelated).
    offsets = re.findall(r"(?:left|margin-left):\s*190px", HTML)
    assert offsets == [], offsets


def test_collapsed_rules_are_desktop_only_and_keep_accessible_names():
    block = _media_block("min-width: 769px")
    assert "html.sb-collapsed { --sb-w: 64px; }" in block
    # Every collapsed rule lives in the desktop block, none outside it.
    stray = re.findall(r"^\s*html\.sb-collapsed[^{\n]*\{", HTML.replace(block, ""), re.M)
    assert stray == [], f"collapsed rules outside the desktop block reach phones: {stray}"
    hide = re.search(r"html\.sb-collapsed #app-sidebar \.sb-label,[^{]*\{([^}]*)\}", block)
    assert hide, "labels must be hidden by the visually-hidden rule"
    assert "clip: rect(0, 0, 0, 0)" in hide.group(1)
    assert "display: none" not in hide.group(1)
    # The Account group stays at the bottom when section labels become dividers.
    assert "html.sb-collapsed #app-sidebar .sb-section-label.sb-push-bottom { margin-top: auto; }" in block


def test_the_toggle_says_what_it_does():
    btn = re.search(r'<button[^>]*id="sb-collapse-btn"[^>]*>', HTML)
    assert btn, "the collapse toggle is missing"
    tag = btn.group(0)
    for attr in ('type="button"', 'aria-controls="app-sidebar"', 'aria-expanded="true"',
                 'aria-label="Collapse sidebar"', 'onclick="toggleSidebarCollapsed()"'):
        assert attr in tag, attr
    sync = _function("_syncSidebarToggle")
    assert "aria-expanded" in sync and "Expand sidebar" in sync and "Collapse sidebar" in sync
    setter = _function("setSidebarCollapsed")
    assert "localStorage.setItem('hp_sb_collapsed'" in setter and "catch" in setter
    assert "_syncSidebarToggle()" in setter
    # Synced at parse time too, so a state restored in <head> is announced right.
    assert re.search(r"\n\s*_syncSidebarToggle\(\);", HTML)


def test_collapsing_never_closes_the_open_job():
    """Any click inside the sidebar closes the job drawer — making room for
    the job is the one click that must not."""
    i = HTML.index("The app sidebar stays visible while a job is open")
    listener = HTML[i:i + 600]
    assert "closest('#sb-collapse-btn')" in listener
    assert listener.index("#sb-collapse-btn") < listener.index("closeJobDrawer")


def test_the_rail_has_a_label_on_hover_and_focus():
    show = _function("_showSbTip")
    assert "_sbIsCollapsed()" in show and "innerWidth <= 768" in show
    assert "addEventListener('focusin', _showSbTip)" in HTML
    assert "addEventListener('mouseover', _showSbTip)" in HTML
    # The resume button has an icon and a label for the rail.
    assert re.search(r'class="sb-resume-btn"[^>]*data-tip="Upload / update resume"', HTML)
    assert '<use href="#i-upload"/>' in HTML and 'id="i-upload"' in HTML


def test_a_nav_tap_on_a_phone_closes_the_drawer():
    toggle = _function("toggleSidebar")
    assert "typeof force === 'boolean'" in toggle
    assert "aria-expanded" in toggle
    assert "toggleSidebar(false)" in _function("sbSwitchTab")
    i = HTML.index("Close sidebar when a nav item is clicked on mobile")
    assert "toggleSidebar(false)" in HTML[i:i + 300]
