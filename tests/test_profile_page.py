"""My profile is a PAGE beside the sidebar, not a pop-up (owner, 2026-11-14).

Three requests in one: the Trust Profile panel leaves the profile, the profile
opens as a view of the dashboard (the sidebar stays, the rest of the screen is
the profile), and it gains an "Auto-fill data" editor for the answers
application forms ask for.

The behaviour was driven in Chromium when it was built (open, add a job, add a
skill, jump from a completion chip, save, Back/Forward, leave and return with
unsaved edits, discard). What is pinned here is the structure those depend on,
so the next edit to this very large template cannot quietly turn the page back
into a pop-up, lose a field, or hide the views behind it for good.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "app" / "templates" / "dashboard.html").read_text()

_START = DASH.index('<div id="view-content-profile"')
_END = DASH.index('<header class="mb-4 hp-band">')
PAGE = DASH[_START:_END]
STYLE = PAGE[PAGE.index("<style>"):PAGE.index("</style>")]


def _function(name: str) -> str:
    m = re.search(r"(?:async )?function %s\(" % re.escape(name), DASH)
    assert m, name
    depth, i = 0, DASH.index("{", m.end())
    for j in range(i, len(DASH)):
        if DASH[j] == "{":
            depth += 1
        elif DASH[j] == "}":
            depth -= 1
            if depth == 0:
                return DASH[m.start():j + 1]
    raise AssertionError(name)


# ── a page, not a pop-up ─────────────────────────────────────────────────────

def test_the_pop_up_is_gone():
    assert 'id="profile-modal"' not in DASH and 'id="profile-shell"' not in DASH
    # No backdrop to click away from: leaving is choosing another view.
    assert "closeProfileModal()" not in PAGE


def test_the_page_sits_in_the_main_column_right_after_the_top_bar():
    """A direct child of the dashboard's content column, like every other
    view, so the sidebar and the top bar stay where they are."""
    assert DASH.index('<div id="app-topbar">') < _START < _END
    assert 'id="profile-form"' in PAGE and "</form>" in PAGE
    between = DASH[DASH.index('<div id="app-topbar">'):_START]
    assert between.count("<div") == between.count("</div>"), "the page is nested inside the top bar"


def test_opening_it_hides_every_other_view_and_nothing_else():
    """One rule, with !important: the lanes' own scripts keep toggling
    `style.display` on the banners and views while the profile is open."""
    rule = ("body.pp-open #main-wrapper > .max-w-7xl > "
            "*:not(#app-topbar):not(#view-content-profile):not(footer) { display: none !important; }")
    assert rule in STYLE
    enter = _function("_ppEnter")
    assert "classList.add('pp-open')" in enter and "sb-nav-profile" in enter


def test_any_other_view_takes_the_screen_back():
    body = _function("switchViewTab")
    assert "_ppLeave()" in body.split("['kanban', 'explorer'")[0], "the first thing a tab switch does"
    leave = _function("_ppLeave")
    assert "classList.remove('pp-open')" in leave and "replaceState" in leave


def test_the_sidebar_has_an_entry_and_every_old_door_still_opens_it():
    assert re.search(r'<button id="sb-nav-profile" class="sb-btn" onclick="openProfileModal\(\)" data-tip="My Profile">', DASH)
    assert 'id="header-profile-btn" data-tour="profile" onclick="openProfileModal()"' in DASH
    assert "onclick=\"closeSettings(); openProfileModal()\"" in DASH          # Settings › Edit profile
    assert "if ((location.hash || '').indexOf('#profile') === 0) openProfileModal();" in DASH


def test_unsaved_edits_survive_leaving_and_the_bar_says_so():
    opener = _function("openProfileModal")
    assert "_serializeProfileForm(form) !== _profileSnapshot" in opener.split("fetch('/api/profile'")[0], \
        "reopening must not reload the saved profile over unsaved edits"
    assert 'id="pp-savebar"' in PAGE and "Unsaved changes" in PAGE
    assert 'type="submit" id="profile-save-btn"' in PAGE
    assert "bar.style.display = dirty ? 'flex' : 'none'" in _function("_refreshProfileDirty")
    # Saving stays on the page.
    assert "closeProfileModal" not in _function("saveProfile")


# ── the Trust Profile panel left the profile ─────────────────────────────────

def test_no_trust_profile_panel():
    for gone in ('id="trust-panel"', 'id="trust-dimensions"', 'id="trust-tier"', "Trust Profile</span>"):
        assert gone not in DASH, gone
    # Nothing a user READS on the page mentions it (the public-link box keeps
    # its old element id and copy handler).
    visible = re.sub(r"<style>.*?</style>|<!--.*?-->|<[^>]+>", " ", PAGE, flags=re.S)
    assert "trust" not in visible.lower()
    body = _function("loadTrustProfile")
    assert "trust-dimensions" not in body and "trust-panel" not in body
    assert "loadTrustProfile(false)" not in _function("openProfileModal")


def test_the_public_link_moved_beside_the_public_profile_setting():
    settings = PAGE[PAGE.index('id="pp-sec-settings"'):]
    assert settings.index('name="public_profile"') < settings.index('id="trust-share-url"')
    assert "if (share) share.value = d.public_profile ? (d.share_url || '') : '';" in _function("loadTrustProfile")


# ── the layout the owner described ───────────────────────────────────────────

def test_the_sections_come_in_the_order_asked_for():
    order = ['<h1>My profile</h1>', 'id="pp-add-btn"', 'id="pp-completion"', "Manage auto-fill data",
             'class="pp-card pp-hero"', 'id="pp-sec-exp"', 'id="pp-sec-edu"', 'id="pp-sec-cert"',
             'id="pp-sec-skill"', 'id="pp-sec-proj"']
    at = [PAGE.index(x) for x in order]
    assert at == sorted(at), dict(zip(order, at))


def test_the_hero_has_its_four_tiles_and_link_buttons():
    hero = PAGE[PAGE.index('class="pp-card pp-hero"'):PAGE.index('id="pp-sec-exp"')]
    for tile in ("Skills", "Roles", "Certs", "Education"):
        assert f"<small>{tile}</small>" in hero
    for link in ("pp-link-linkedin", "pp-link-github", "pp-link-website"):
        assert f'id="{link}" target="_blank" rel="noopener noreferrer"' in hero
    assert ">About<" in hero and "Edit profile" in hero
    # A link the user typed is only ever opened as an http(s) address.
    href = _function("_ppHref")
    assert "p.protocol === 'https:' || p.protocol === 'http:'" in href


def test_columns_line_up_and_collapse_on_small_screens():
    """Label, box and hint share rows across the columns (subgrid), three
    columns wide, two on a tablet, one on a phone."""
    assert ".pp-grid { display: grid; grid-template-columns: repeat(3, minmax(0,1fr));" in STYLE
    assert ".pp-field { display: grid; grid-template-rows: subgrid; grid-row: span 3;" in STYLE
    assert "@supports not (grid-template-rows: subgrid)" in STYLE
    assert re.search(r"@media \(max-width: 1100px\) \{\s*\.pp-grid \{ grid-template-columns: repeat\(2,", STYLE)
    assert re.search(r"@media \(max-width: 680px\) \{\s*\.pp-grid, \.pp-grid\.pp-grid-2 \{ grid-template-columns: minmax\(0,1fr\);", STYLE)


def test_the_page_uses_the_theme_tokens_and_its_own_classes():
    """One blue brand (test_brand_colours): the accent comes from the theme
    variables, so the page follows whichever theme the user picked. And no new
    Tailwind class: the compiled stylesheet does not need this page."""
    assert "var(--sage)" in STYLE and "var(--surface)" in STYLE and "var(--border)" in STYLE
    assert not re.search(r"#0077C2|#0369A1|#0EA5E9|orange", STYLE, re.I)
    markup = PAGE[PAGE.index("</style>"):]
    classes = set(re.findall(r'class="([^"]+)"', markup))
    stray = sorted(c for cl in classes for c in cl.split() if not (c.startswith("pp-") or c == "ic"))
    assert stray == [], stray


def test_copy_follows_the_house_spelling():
    visible = re.sub(r"<!--.*?-->", "", PAGE, flags=re.S)
    assert "résumé" not in visible.lower()


# ── the auto-fill editor ─────────────────────────────────────────────────────

def test_the_editor_is_drawn_from_the_servers_catalog():
    assert "const PP_CATALOG = {{ autofill_catalog|tojson }};" in DASH
    build = _function("ppBuildEditor")
    assert "PP_CATALOG.sections.forEach" in build and "_ppFieldHtml" in build
    field = _function("_ppFieldHtml")
    assert "f.column || ('af.' + f.key)" in field, "a column field keeps its column name; the rest are af.<key>"
    save = _function("saveProfile")
    assert "k.indexOf('af.') === 0" in save and "data.autofill = autofill" in save


def test_the_dashboard_is_served_with_the_catalog_and_no_user_data_in_it():
    from fastapi.testclient import TestClient

    from app.api.server import app
    from app.autofill import autofill_fields as af
    html = TestClient(app).get("/dashboard").text
    blob = html[html.index("const PP_CATALOG = "):]
    blob = blob[:blob.index("\n")]
    for key in ("postal_code", "non_compete", "highest_education", "languages"):
        assert f'"{key}"' in blob, key
    assert len(re.findall(r'"key": ?"', blob)) >= len(af.FIELDS)
    assert "\\\\b" not in blob, "the matching patterns stay on the server"


def test_every_hand_written_block_has_a_section_to_live_in():
    from app.autofill import autofill_fields as af
    sections = {s.key for s in af.SECTIONS}
    extras = re.findall(r'<div data-pp-extra="([a-z]+)"([^>]*)>', PAGE)
    assert {k for k, _ in extras} == {"authorization", "availability", "heard", "preferences", "selfid"}
    for key, attrs in extras:
        assert key in sections or 'data-title="' in attrs, f"{key}: no catalog section and no title"


@pytest.mark.parametrize("needle", ['type="password"', "password", "signature", "arbitration"])
def test_the_page_never_asks_for_what_the_product_refuses_to_hold(needle):
    fields = re.sub(r"<p class=\"pp-(hint|note)\">.*?</p>|<h1>.*?</p>", "", PAGE[PAGE.index("</style>"):], flags=re.S)
    assert needle not in fields.lower(), needle


def test_the_work_authorization_questions_keep_their_one_interpreter():
    """The status, the sponsorship box and the dates are the same inputs as
    before, in the editor's Work authorization section; no second answer."""
    block = PAGE[PAGE.index('data-pp-extra="authorization"'):PAGE.index('data-pp-extra="availability"')]
    for name in ("work_auth_status", "work_authorization", "requires_sponsorship", "ead_end_date",
                 "opt_unemployment_days_used", "stem_opt"):
        assert f'name="{name}"' in block, name
    assert 'onchange="_syncSponsorshipFromStatus(this)"' in block
    assert "are answered from this status" in block


def test_entries_are_saved_from_their_own_state_never_from_named_inputs():
    """Jobs, schools, certifications, projects and languages are lists. Their
    edit boxes carry no name, so FormData cannot send half an entry."""
    for helper in ("_ppIn", "_ppSel", "_ppChk", "_ppArea"):
        assert "name=" not in _function(helper), helper
    save = _function("saveProfile")
    assert "data.education = _eduState.filter" in save and "data.experience = _expState.filter" in save
    assert "Object.keys(_afLists).forEach" in save
