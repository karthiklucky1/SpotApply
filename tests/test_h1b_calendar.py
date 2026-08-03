"""Guard: the H-1B cap calendar must stay arithmetically honest.

The load-bearing rule is that registering in March of year Y is what makes an
Oct 1 of year Y start possible — so from April onward the earliest reachable
cap-subject start is Oct 1 of the FOLLOWING year. Getting that off by a year
would tell an international candidate to wait when they should be applying to
cap-exempt employers, or the reverse.
"""
from datetime import date

import pytest

from app.intelligence.h1b_calendar import phase


@pytest.mark.parametrize(
    "today,expected_phase",
    [
        (date(2026, 1, 15), "pre_registration"),
        (date(2026, 2, 28), "pre_registration"),
        (date(2026, 3, 1), "registration_open"),
        (date(2026, 3, 21), "registration_open"),
        (date(2026, 3, 25), "selection"),
        (date(2026, 3, 31), "selection"),
        (date(2026, 4, 1), "petition_filing"),
        (date(2026, 9, 30), "petition_filing"),
        (date(2026, 10, 1), "between_cycles"),
        (date(2026, 12, 31), "between_cycles"),
    ],
)
def test_phase_boundaries(today, expected_phase):
    assert phase(today).phase == expected_phase


def test_registering_in_march_targets_october_of_the_same_year():
    """FY2027: registration March 2026 -> employment start 1 Oct 2026."""
    p = phase(date(2026, 3, 10))
    assert p.next_employment_start == "2026-10-01"
    assert p.days_to_registration == 0


def test_after_registration_closes_the_next_start_slips_a_full_year():
    """The fact that reframes an April job search."""
    p = phase(date(2026, 4, 2))
    assert p.next_employment_start == "2027-10-01"
    assert p.registration_opens == "2027-03-01"


def test_pre_registration_counts_down_to_this_years_window():
    p = phase(date(2026, 2, 1))
    assert p.registration_opens == "2026-03-01"
    assert p.days_to_registration == 28
    assert p.next_employment_start == "2026-10-01"


def test_days_to_registration_is_never_negative():
    d = date(2026, 1, 1)
    while d < date(2028, 1, 1):
        assert phase(d).days_to_registration >= 0, d
        d = date.fromordinal(d.toordinal() + 1)


def test_cap_exempt_route_is_offered_whenever_the_lottery_cannot_help():
    """From April to March the cap-subject door is shut; say so and name the exit."""
    for d in (date(2026, 4, 2), date(2026, 11, 15)):
        assert "cap-exempt" in phase(d).message.lower(), d


def test_payload_is_json_safe_and_flagged_approximate():
    import json

    p = phase(date(2026, 5, 5)).as_dict()
    json.dumps(p)  # must not raise — this goes straight into an API response
    assert p["is_approximate"] is True
    assert p["source_url"].startswith("https://www.uscis.gov/")
