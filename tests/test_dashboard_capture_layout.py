"""Layout findings from the live captures of 2026-10-14 (the real app on
fictional seed data, phone 430x932 and desktop 1280x800), one test group per
finding. Each pins the rule that makes the whole class of mistake fail, not
only the one screen it was seen on.

1. Desktop: the "Apply today — early window" divider took ONE cell of the
   two-column shortlist grid, so the first card started in the right column.
2. Phone: the job drawer's Auto-Fill / Mark Applied sat on top of the title
   ("Senior Backend Eng…") and the company wrapped a word a line.
3. Phone: the Tailoring Studio's title got ~0px (it vanished; the subtitle
   went a word a line) and its pills ran off the right edge.
4. Phone: the documents modal (View Documents) sat under the fixed menu
   button with the page's top bar showing around it, and its header buttons
   (Auto-fill Form, Download .docx, Download PDF, close) ran off the edge.
5. Phone: "People & team" squeezed each value to ~40px between its label and
   its evidence chip, so it broke mid-word ("Platfor / m Enginee / ring").
6. Phone: the outreach drafts' job URLs ran past the right edge of the card.
7. Outreach "Sent" list: a note sent 24.5 hours earlier read "Sent today".
   The server sends naive UTC ISO strings and `new Date()` reads those as
   LOCAL time (5 hours off in Chicago). Seven other call sites each carried
   their own "append Z" fix with three different regexes; _agoLabel had none.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/templates/dashboard.html").read_text()
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


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


# ── 3. the Tailoring Studio header wraps ────────────────────────────────────

def _ts_head() -> str:
    m = re.search(r'<div id="ts-head"[^>]*>(.*?)\n     </div>\n     <div id="ts-stages"', HTML, re.S)
    assert m, "the Tailoring Studio header moved — re-read this group"
    return m.group(0)


def test_the_studio_title_claims_space_and_the_pills_wrap():
    head = _ts_head()
    # The pills sit in their own wrapping row, never a shrink-0 one, and the
    # close button is outside it so a phone can keep it beside the title.
    pills = re.search(r'<div id="ts-pills" class="([^"]*)"', head)
    assert pills and "shrink-0" not in pills.group(1).split()
    for pill in ("ts-attempts", "ts-score", "ts-coverage", "ts-grounding"):
        assert f'id="{pill}"' in head
    pills_html = head[pills.start():head.index('id="ts-close"')]
    assert "closeTailorStudio()" not in pills_html
    assert re.search(r"#ts-head \{ flex-wrap: wrap;", HTML)
    assert re.search(r"#ts-head-text \{ flex: 1 1 \d+px; \}", HTML)
    assert re.search(r"#ts-pills \{ flex-wrap: wrap;[^}]*min-width: 0;", HTML)


def test_on_a_phone_the_studio_pills_take_their_own_row():
    css = _phone_css(767)
    assert re.search(r"#ts-close \{ order: 1; \}", css)
    assert re.search(r"#ts-pills \{ order: 2; flex: 1 1 100%;", css)
    # The footer's four buttons wrap too instead of squeezing to three lines each.
    assert re.search(r"#ts-actions \{ flex-wrap: wrap; \}", css)
    assert 'id="ts-actions"' in HTML


# ── 4. the documents modal is above the menu button and its header wraps ────

def _css_z(selector_re: str) -> int:
    """z-index of the first rule whose selector-and-brace matches `selector_re`."""
    m = re.search(selector_re + r"[^}]*z-index: (\d+)", HTML)
    assert m, selector_re
    return int(m.group(1))


def test_the_documents_modal_stacks_above_the_phone_menu_button():
    menu = _css_z(r"#mobile-menu-btn \{")
    modal = _css_z(r"#modal \{")
    assert modal > menu, f"#modal z-index {modal} is under the fixed menu button ({menu})"
    # ...and stays below what may open over it or above the page: the
    # toast stack and the confirm dialog.
    assert modal < 100
    # A real backdrop: the class it carries had no CSS, so the page showed
    # through as if the modal were part of it.
    assert re.search(r'<div id="modal" class="[^"]*\bmodal-backdrop\b', HTML)
    assert re.search(r"#modal\.modal-backdrop \{ background: rgba\(", HTML)
    # openModal steps the job drawer (z-90) aside, so nothing it shares the
    # screen with is higher.
    assert "closeJobDrawer()" in _function("openModal")


def test_the_documents_modal_header_wraps():
    head = re.search(r'<div id="modal-head"(.*?)<!-- Tabs', HTML, re.S)
    assert head, "the documents modal header moved — re-read this group"
    head = head.group(0)
    actions = re.search(r'<div id="modal-actions" class="([^"]*)"', head)
    assert actions and "shrink-0" not in actions.group(1).split()
    # The close button is outside the action row, so a phone keeps it beside
    # the title while the actions wrap below.
    action_html = head[actions.start():head.index('id="modal-close"')]
    assert "closeModal()" not in action_html
    assert re.search(r"#modal-head \{ flex-wrap: wrap;", HTML)
    assert re.search(r"#modal-actions \{ flex-wrap: wrap;[^}]*min-width: 0;", HTML)
    css = _phone_css()
    assert re.search(r"#modal-close \{ order: 1; \}", css)
    assert re.search(r"#modal-actions \{ order: 2; flex: 1 1 100%;", css)
    # The documents themselves wrap rather than scrolling sideways.
    assert re.search(r"#tab-content-resume, #tab-content-cover \{ white-space: pre-wrap;", HTML)


# ── 5. People & team values wrap at word boundaries ─────────────────────────

def test_people_and_team_rows_wrap_instead_of_squeezing_the_value():
    fn = _function("loadHiringContext")
    row = re.search(r"'<div class=\"hc-row\" style=\"([^\"]*)\"", fn)
    assert row, "the People & team row is no longer built in loadHiringContext"
    assert "flex-wrap:wrap" in row.group(1)
    spans = re.findall(r"'<span (?:class=\"[^\"]*\" )?style=\"([^\"]*)\">' \+ _esc\((label|f\.value|evLabel)\)", fn)
    styles = {name: style for style, name in spans}
    assert set(styles) == {"label", "f.value", "evLabel"}, styles
    # The value has a floor, so the chip wraps under it rather than the value
    # shrinking to a few letters a line...
    assert re.search(r"flex:1 1 (1[2-9]\d|[2-9]\d\d)px", styles["f.value"]), styles["f.value"]
    assert "min-width:0" in styles["f.value"]
    # ...and breaks at word boundaries (word-break:break-word broke anywhere).
    assert "overflow-wrap:break-word" in styles["f.value"]
    assert "word-break" not in styles["f.value"]
    assert "flex-shrink:0" in styles["evLabel"]


# ── 6. outreach text wraps long URLs inside its card ────────────────────────

def test_outreach_free_text_breaks_long_urls():
    fn = _function("renderOutreachKit")
    # Every paragraph that prints text we did not write (the draft, the
    # verify-before-sending note, a named person, a quoted snippet) can hold a
    # URL or an address with no break opportunity; each must break it.
    for expr in ("_esc(d.body || '')", "_esc(d.verify_before_sending)",
                 "_esc(p.label)}: ${_esc(who)", "_esc(p.evidence.slice(0, 160))"):
        i = fn.index(expr)
        p_open = fn.rindex("<p ", 0, i)
        assert "overflow-wrap:anywhere" in fn[p_open:i], expr


# ── 7. server timestamps are UTC, relative labels follow the calendar ───────

def _node_in(tz: str, script: str):
    env = dict(os.environ, TZ=tz)
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


_TIME_FNS = "\n".join(_function(n) for n in ("_serverTime", "_agoLabel"))


@needs_node
def test_a_note_sent_24_and_a_half_hours_ago_is_not_sent_today():
    # The capture: seeded 2026-10-14 21:18:21 UTC, the LinkedIn note 24.5 h
    # earlier, the viewer in Chicago (UTC-5 in October).
    out = _node_in("America/Chicago", _TIME_FNS + """
Date.now = () => Date.parse('2026-10-14T21:18:21Z');
console.log(JSON.stringify({
  sent: _agoLabel('2026-10-13T20:48:21.776128'),
  reply: _agoLabel('2026-10-14T18:18:21.776128'),
  three: _agoLabel('2026-10-11T22:00:00'),
}));
""")
    assert out == {"sent": "yesterday", "reply": "today", "three": "3 days ago"}


@needs_node
def test_today_and_yesterday_follow_the_viewers_calendar():
    # 00:10 in Chicago; a note sent at 23:50 the evening before (20 minutes
    # ago) is "yesterday", and one sent at 00:05 is "today".
    out = _node_in("America/Chicago", _TIME_FNS + """
Date.now = () => Date.parse('2026-10-14T05:10:00Z');
console.log(JSON.stringify([_agoLabel('2026-10-14T04:50:00'), _agoLabel('2026-10-14T05:05:00')]));
""")
    assert out == ["yesterday", "today"]


@needs_node
def test_server_time_reads_a_naive_timestamp_as_utc_and_keeps_explicit_offsets():
    out = _node_in("America/Chicago", _TIME_FNS + """
const t = s => { const d = _serverTime(s); return d ? d.toISOString() : null; };
console.log(JSON.stringify({
  naive: t('2026-10-13T20:48:21'), micro: t('2026-10-13T20:48:21.776128'),
  space: t('2026-10-13 20:48:21'), z: t('2026-10-13T20:48:21Z'),
  utc: t('2026-10-13T20:48:21+00:00'), cdt: t('2026-10-13T15:48:21-05:00'),
  compact: t('2026-10-13T15:48:21-0500'), dateOnly: t('2026-10-13'),
  bad: t('not a date'), empty: t(''), none: t(null), label: _agoLabel('garbage'),
}));
""")
    same = "2026-10-13T20:48:21.000Z"
    assert out["naive"] == out["space"] == out["z"] == out["utc"] == out["cdt"] == out["compact"] == same
    assert out["micro"] == "2026-10-13T20:48:21.776Z"
    assert out["dateOnly"] == "2026-10-13T00:00:00.000Z"
    assert out["bad"] is None and out["empty"] is None and out["none"] is None
    assert out["label"] == ""


def test_every_server_timestamp_is_parsed_by_the_one_helper():
    # The class: a bare new Date(<server string>) reads a naive timestamp as
    # local time. The only "append Z" left is inside _serverTime, and every
    # other new Date(...) in the dashboard builds a date from numbers/now.
    helper = _function("_serverTime")
    rest = HTML.replace(helper, "")
    assert "+ 'Z'" not in rest and '+ "Z"' not in rest, "a call site appends Z itself — use _serverTime"
    allowed = {"", "Date.now()", "x.getFullYear(), x.getMonth(), x.getDate()"}
    args = re.findall(r"new Date\(([^()]*(?:\([^()]*\)[^()]*)*)\)", rest)
    stray = [a for a in args if a.strip() not in allowed]
    assert not stray, f"parse server timestamps with _serverTime(), not new Date(...): {stray}"
    for caller in ("_agoLabel", "sortShortlist", "loadNotifications"):
        assert "_serverTime(" in _function(caller), caller
