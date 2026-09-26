"""Relocation choices in the profile, the contact line, and the new-resume
reminder (2026-09-26)."""
from pathlib import Path

from app.tailoring.tailor import polish_contact_line

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "app" / "templates" / "dashboard.html").read_text()
SERVER = (ROOT / "app" / "api" / "server.py").read_text()
EXT = (ROOT / "extension" / "content.js").read_text()

HEADER = """# CANDIDATE NAME
(513) 400 -3765 | person@example.com | linkedin.com/in/x
## SUMMARY
Scaled a service to 2024 nodes; ticket 555-1234 closed."""


def test_phone_is_formatted_one_way():
    out = polish_contact_line(HEADER, "")
    assert "(513) 400-3765 |" in out


def test_profile_city_is_added_when_the_header_has_none():
    out = polish_contact_line(HEADER, "Cincinnati, OH")
    assert out.split("\n")[1].startswith("Cincinnati, OH | (513) 400-3765")


def test_city_is_not_duplicated():
    md = HEADER.replace("(513) 400 -3765", "Cincinnati, OH | (513) 400-3765")
    assert polish_contact_line(md, "Cincinnati, OH").count("Cincinnati") == 1


def test_body_is_never_touched():
    out = polish_contact_line(HEADER, "Cincinnati, OH")
    assert out.split("## SUMMARY")[1] == HEADER.split("## SUMMARY")[1]


def test_profile_read_returns_the_relocation_fields():
    """The form ticks only what the read returns: without these, "Open to
    relocation" rendered unticked and the next save switched it off."""
    for line in ('"open_to_relocation": bool(getattr(profile, "open_to_relocation", False)),',
                 '"relocation_resume_optin": bool(getattr(profile, "relocation_resume_optin", False)),',
                 '"relocation_targets": getattr(profile, "relocation_targets", "") or "",',
                 '"availability": getattr(profile, "availability", "") or "",'):
        assert line in SERVER, line


def test_relocation_options_appear_with_the_checkbox():
    assert 'id="reloc-options"' in DASH and 'name="resume_location_mode"' in DASH
    assert "function _syncRelocationOptions()" in DASH
    assert 'name="relocation_targets"' in DASH
    # Not open to relocation = never print a relocation line.
    assert "data.resume_use_job_city = open && mode === 'job';" in DASH


def test_extension_answers_willingness_only_from_the_profile():
    block = EXT.split("function classifyScreeningQuestion")[1].split("const NEVER")[0]
    assert "pack.open_to_relocation === true ? true : null" in block
    assert "assist|package|benefit" in block          # never ask for a relocation package


def test_new_resume_triggers_a_review_of_roles_and_profile():
    assert '"roles_pinned": _roles_pinned' in SERVER and '"roles_before": _roles_before' in SERVER
    assert "function _afterResumeChange(d)" in DASH and "_afterResumeChange(data)" in DASH
    assert "Review target roles" in DASH and "Review profile" in DASH


def test_job_city_replaces_the_header_location_only():
    md = "# N\nCincinnati, OH | (513) 400 -3765 | a@b.com\n## SUMMARY\nMoved from Cincinnati, OH in 2020."
    out = polish_contact_line(md, "Cincinnati, OH", show_location="New York, NY")
    assert out.split("\n")[1] == "New York, NY | (513) 400-3765 | a@b.com"
    assert "Moved from Cincinnati, OH in 2020." in out          # body untouched


def test_job_city_is_added_when_the_header_had_no_location():
    out = polish_contact_line("# N\na@b.com\n## S\nx", "Cincinnati, OH", show_location="New York, NY")
    assert out.split("\n")[1] == "New York, NY | a@b.com"


def test_no_partial_word_replacement():
    out = polish_contact_line("# N\nCincinnati, Ohio | a@b.com\n## S\nx", "Cincinnati, OH",
                              show_location="New York, NY")
    assert out.split("\n")[1] == "New York, NY | a@b.com"


def test_tailor_uses_the_job_city_only_when_chosen_and_verified():
    src = (ROOT / "app" / "tailoring" / "tailor.py").read_text()
    assert 'getattr(_prof, "resume_use_job_city", False)' in src
    assert 'getattr(_prof, "open_to_relocation", False)' in src
    assert 'if _dest and "," in _dest.destination:' in src          # a real City, ST only
    assert "show_location=job_city_location" in src


def test_profile_offers_three_location_choices():
    for label in ("Keep my current location", "Keep my location + add", "Use the job's city as my location"):
        assert label in DASH
    assert '"resume_use_job_city": bool(getattr(profile, "resume_use_job_city", False)),' in SERVER
