"""Every accent theme the dashboard offers has to actually exist.

Twice now a hex sweep ran BEFORE the edit that was meant to add a theme key,
so the add matched nothing and did nothing — and the swatch shipped pointing
at a key that was not there. `changeTheme('sky')` then reads
`HP_THEMES['sky']`, falls through to `HP_THEMES[HP_THEME_DEFAULT]`, and if
that is missing too it throws on `t.sage`. Neither failure is visible in a
screenshot; you only find it by clicking the swatch.

These are cheap string checks, but they cover the exact class of mistake
that got through: a reference with nothing behind it.
"""
import re
from pathlib import Path

DASH = (Path(__file__).resolve().parents[1]
        / "app" / "templates" / "dashboard.html").read_text()

_BLOCK = re.search(r"const HP_THEMES = \{(.*?)\n        \};", DASH, re.S)


def _keys():
    assert _BLOCK, "HP_THEMES block not found — did the shape change?"
    return set(re.findall(r"^\s*(\w+)\s*:\s*\{", _BLOCK.group(1), re.M))


def test_every_swatch_points_at_a_real_theme():
    used = set(re.findall(r"changeTheme\('(\w+)'\)", DASH))
    assert used, "no theme buttons found — the selector was removed?"
    orphans = used - _keys()
    assert not orphans, (
        f"theme button(s) {sorted(orphans)} call changeTheme with a key that is "
        f"not in HP_THEMES {sorted(_keys())} — clicking one throws")


def test_the_default_theme_exists():
    m = re.search(r"HP_THEME_DEFAULT = '(\w+)'", DASH)
    assert m, "HP_THEME_DEFAULT is gone; changeTheme has no fallback"
    assert m.group(1) in _keys(), (
        f"default theme {m.group(1)!r} is not in HP_THEMES {sorted(_keys())} — "
        f"the fallback in changeTheme resolves to undefined")


def test_every_theme_carries_the_full_token_set():
    """A theme missing a key leaves that CSS variable at the previous theme's
    value, so switching gives a half-repainted UI rather than a clean swap."""
    for line in _BLOCK.group(1).strip().splitlines():
        if ":" not in line or "{" not in line:
            continue
        name = line.split(":", 1)[0].strip()
        for token in ("sage:", "sage600:", "sage700:", "sageTint:", "sageLite:"):
            assert token in line, f"theme {name!r} is missing {token}"


def test_change_theme_falls_back_through_the_named_default():
    """The fallback must go through the constant, not a hard-coded key that
    can drift from the list above it."""
    assert "HP_THEMES[themeName] || HP_THEMES[HP_THEME_DEFAULT]" in DASH, (
        "changeTheme should fall back via HP_THEME_DEFAULT so the fallback "
        "cannot name a theme that no longer exists")
