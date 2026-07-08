"""ResumeDoctor integrity anchors must come from the user's OWN master resume,
never from a hardcoded candidate (the old list pinned Home Depot / University
of Cincinnati for every tenant)."""
import re

from app.tailoring.doctor import _derive_anchors, _anchor_pattern

UK_MASTER = """# JANE SMITH
London, UK | jane@example.com

## PROFESSIONAL EXPERIENCE
**Software Engineer** | Barclays | Jan 2021 - Present | London, UK
- Built payment APIs serving 2M requests/day.

**Junior Developer** | Acme Ltd | Sep 2018 - Dec 2020 | Remote
- Built internal dashboards.

## EDUCATION
**Bachelor of Science** | 2018
Imperial College London
"""

US_MASTER = """# JOHN DOE
Austin, TX | john@example.com

## EXPERIENCE
**Data Engineer** | Dell | Mar 2019 - Jun 2023 | Austin, TX
- Owned ETL pipelines over 40TB.

## EDUCATION
**Master of Science** | 2019
University of Texas
"""


def test_anchors_derived_from_own_resume():
    uk = _derive_anchors(UK_MASTER)
    descs = " | ".join(d for _, d in uk)
    assert "Barclays" in descs
    assert "Acme Ltd" in descs
    assert "degree name" in descs
    assert "education institution" in descs
    # Nothing from the old hardcoded owner list leaks in.
    assert "home depot" not in descs.lower()
    assert "cincinnati" not in descs.lower()


def test_anchors_self_match_and_differ_per_user():
    for master in (UK_MASTER, US_MASTER):
        for pattern, desc in _derive_anchors(master):
            assert re.search(pattern, master.lower(), re.IGNORECASE), \
                f"anchor '{desc}' must match its own master resume"

    # A tailored resume that swapped employers/dates fails the other's anchors.
    us_patterns = [p for p, _ in _derive_anchors(US_MASTER)]
    assert not all(re.search(p, UK_MASTER.lower(), re.IGNORECASE) for p in us_patterns)


def test_anchor_pattern_tolerates_dash_and_spacing_variants():
    # Mirrors real usage: _derive_anchors lowercases the anchor text and
    # ResumeDoctor.check searches with re.IGNORECASE.
    pattern = _anchor_pattern("jan 2021 - present")
    assert re.search(pattern, "jan 2021 – present", re.IGNORECASE)   # en-dash
    assert re.search(pattern, "jan  2021 - present", re.IGNORECASE)  # double space
    assert not re.search(pattern, "jan 2021 - past", re.IGNORECASE)


def test_no_anchors_for_unstructured_text():
    assert _derive_anchors("Just a paragraph about skills, no history.") == []


def test_doctor_check_uses_master_anchors():
    from app.tailoring.doctor import ResumeDoctor

    doctor = ResumeDoctor()
    jd = "We need Python and APIs."
    # Tailored copy keeps Jane's employers/dates → no integrity issues.
    report_ok = doctor.check(UK_MASTER, UK_MASTER, jd)
    assert report_ok.integrity_issues == []

    # Tailored copy that dropped her employer must be flagged.
    tampered = UK_MASTER.replace("Barclays", "Google")
    report_bad = doctor.check(tampered, UK_MASTER, jd)
    assert any("Barclays" in i for i in report_bad.integrity_issues)
