"""One brand: the blue the dashboard uses, on the dashboard AND in the extension.

Founder, 2026-11-04: "the colour combination is bad". The brand moved to blue
in October (dashboard tokens: navy ink #0C2A3E, accent #0077C2, ok/caution/warn
greens, ambers and reds that read on white), but two places kept the old
HirePath purple: the extension's panel on every job form — a dark navy card
with a purple border and lavender, mint, amber and pink text — and ~120 inline
purples in dashboard pop-ups, which the light theme's class remap could not
reach (inline styles, script-built HTML, shadow/ring/gradient utilities).

Pinned here: none of the old purples, and no dark-navy card, in the
extension's UI files; none in the dashboard outside `HP_THEMES`, where violet
and indigo remain accent themes a user can CHOOSE.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PURPLE = re.compile(
    r"#(?:6366f1|4f46e5|6c4bf5|7c3aed|8b5cf6|a855f7|818cf8|a5b4fc|c4b5fd|a78bfa|c7d2fe|4a2bc7|4338ca|3730a3)\b"
    r"|rgba?\(\s*(?:99,\s*102,\s*241|79,\s*70,\s*229|108,\s*75,\s*245|124,\s*58,\s*237|139,\s*92,\s*246"
    r"|167,\s*139,\s*250|129,\s*140,\s*248|168,\s*85,\s*247)\b",
    re.I)
DARK_CARD = re.compile(r"rgba\(\s*15,\s*23,\s*42|#0f0f1a\b", re.I)


def test_the_extension_ui_carries_no_old_purple_and_no_dark_card():
    for name in ("content.js", "popup.html", "popup.js"):
        src = (ROOT / "extension" / name).read_text()
        assert not PURPLE.findall(src), f"extension/{name}: {sorted(set(PURPLE.findall(src)))[:5]}"
        assert not DARK_CARD.findall(src), f"extension/{name} still draws a dark-navy card"


def test_the_extension_panel_is_the_light_brand_card():
    src = (ROOT / "extension" / "content.js").read_text()
    fn = src[src.index("function showOverlay("):]
    style = fn[fn.index("<style>"):fn.index("</style>")]
    assert "background: #FFFFFF" in style and "color: #0C2A3E" in style
    # status colours that read on white (the dashboard's --ok/--caution/--warn)
    step = src[src.index("function showStepOverlay("):src.index("function hasNextStepButton(")]
    for colour in ("#047857", "#B45309", "#B42318"):
        assert colour in step, colour


def test_the_dashboard_has_no_purple_outside_the_themes_a_user_picks():
    src = (ROOT / "app" / "templates" / "dashboard.html").read_text()
    themes = re.search(r"const HP_THEMES = \{.*?\n        \};", src, re.S)
    assert themes, "HP_THEMES block not found"
    rest = src.replace(themes.group(0), "")
    assert not PURPLE.findall(rest), sorted(set(PURPLE.findall(rest)))[:8]
    # violet and indigo stay a CHOICE; the default is the blue brand
    assert "violet:" in themes.group(0) and "HP_THEME_DEFAULT = 'sky'" in src


def test_purple_utility_classes_follow_the_accent_in_the_light_theme():
    src = (ROOT / "app" / "templates" / "dashboard.html").read_text()
    for prefix in ("text", "bg", "border", "shadow", "ring", "accent", "from", "to"):
        assert f'[class*="{prefix}-indigo-"]' in src, prefix
