"""Layout findings from the live captures of 2026-10-14 (the real app on
fictional seed data, phone 430x932 and desktop 1280x800), one test group per
finding. Each pins the rule that makes the whole class of mistake fail, not
only the one screen it was seen on.

1. Desktop: the "Apply today — early window" divider took ONE cell of the
   two-column shortlist grid, so the first card started in the right column.
2. Phone: the job drawer's Auto-Fill / Mark Applied sat on top of the title
   ("Senior Backend Eng…") and the company wrapped a word a line.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text()


def _phone_css(max_px: int = 640) -> str:
    """Every `@media (max-width: N px)` block with N <= max_px, brace-balanced."""
    out = []
    for m in re.finditer(r"@media \(max-width: (\d+)px\) \{", HTML):
        if int(m.group(1)) > max_px:
            continue
        depth = 0
        for j in range(m.end() - 1, len(HTML)):
            depth += {"{": 1, "}": -1}.get(HTML[j], 0)
            if depth == 0:
                out.append(HTML[m.end():j])
                break
    return "\n".join(out)


def _function(name: str) -> str:
    m = re.search(r"(async\s+)?function\s+" + name + r"\s*\(", HTML)
    assert m, name
    depth, i = 0, HTML.index("{", m.end())
    for j in range(i, len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[j], 0)
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


# ── 1. a divider in the board grid spans the row ────────────────────────────

_BOARD_GRIDS = ("#shortlisted-container", "#submitted-container", "#interviewing-container")


def test_the_board_grids_are_two_columns_wide_on_desktop():
    # The premise of the finding: if the grid stops being two columns this
    # group is moot and should be re-read, not silently kept.
    assert re.search(r"@media \(min-width: 1280px\) \{ #shortlisted-container, #submitted-container, "
                     r"#interviewing-container \{ grid-template-columns: 1fr 1fr; \} \}", HTML)


def test_every_non_card_child_of_a_board_grid_spans_the_row():
    # One rule for the class: the early-window divider, the month's earliness
    # line, the empty states and "Show All" — and whatever is added next.
    rule = re.search(r"((?:#\w+-container > :not\(\.jobrow\),?\s*){3})\{ grid-column: 1 / -1; \}", HTML)
    assert rule, "the board grids lost their span-the-row rule for non-card children"
    for grid in _BOARD_GRIDS:
        assert f"{grid} > :not(.jobrow)" in rule.group(1)


def test_the_early_window_divider_spans_the_row_itself():
    # It is built in JS and must not depend on where the stylesheet puts it.
    sort = _function("sortShortlist")
    css = re.search(r"div\.id = 'early-window-divider';.*?div\.style\.cssText = '([^']*)'", sort, re.S)
    assert css, "the early-window divider is no longer styled where it is built"
    assert "grid-column:1 / -1" in css.group(1)
    # Its two phrases wrap on a phone instead of overflowing the row.
    assert "flex-wrap:wrap" in css.group(1)


# ── 2. the job drawer's header stacks on a phone ────────────────────────────

def test_the_drawer_header_actions_take_their_own_row_on_a_phone():
    head = re.search(r'<div class="jd-head">(.*?)<div id="jobDrawerBody"', HTML, re.S).group(1)
    # The actions live in the title row (renderJobDetail moves them there)...
    assert 'id="jobModalHeaderActions"' in head and 'id="jobModalTitle"' in head
    assert "headerActions.appendChild(autoFillBtn)" in _function("renderJobDetail")
    # ...so on a phone the row wraps and the actions get a full-width row
    # after the close button, instead of squeezing the title.
    css = _phone_css()
    assert re.search(r"#jobModal \.jd-head \{[^}]*flex-wrap: wrap", css)
    assert re.search(r"#jobModalHeaderActions \{[^}]*order: 3;[^}]*flex: 1 1 100%;[^}]*flex-wrap: wrap", css)
