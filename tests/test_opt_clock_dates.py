"""The OPT clock speaks only from the user's own, plausible dates (2026-10-28).

A founder's report: "OPT clock showing wrong days — I didn't give OPT start or
end date". Two causes: the clock started for anyone whose status read "OPT"
(90 unemployment days left, from a default of 0 used), and a 2002 date slipped
into the authorization-end field through the date picker ("EAD expired 8,966
days ago"). Now: no saved, plausible end date = no clock; the profile route
refuses an implausible date; the forms bound the picker and show a stored
impossible date blank so the next save clears it.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api import server
from app.intelligence.work_auth import plausible_end_date

DASH = (Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html").read_text()


def _p(**kw):
    base = dict(work_authorization="F-1 OPT", visa_status="", ead_end_date="",
                stem_opt=False, opt_unemployment_days_used=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_no_dates_no_clock_even_on_opt():
    assert server._opt_clock_payload(_p()) is None


def test_an_impossible_date_is_no_clock():
    assert server._opt_clock_payload(_p(ead_end_date="2002-04-11")) is None
    assert server._opt_clock_payload(_p(ead_end_date="2099-01-01")) is None
    assert server._opt_clock_payload(_p(ead_end_date="not a date")) is None


def test_a_real_date_gives_the_clock_from_it():
    end = date.today() + timedelta(days=200)
    oc = server._opt_clock_payload(_p(ead_end_date=end.isoformat(), opt_unemployment_days_used=30))
    assert oc["days_to_ead_end"] == 200 and oc["ead_end_date"] == end.isoformat()
    assert oc["unemployment_days_left"] == 60
    # A card that ran out recently is still the user's own fact.
    past = date.today() - timedelta(days=20)
    assert server._opt_clock_payload(_p(ead_end_date=past.isoformat()))["days_to_ead_end"] == -20


def test_the_window_is_two_years_back_seven_ahead():
    t = date.today()
    assert plausible_end_date((t - timedelta(days=729)).isoformat())
    assert plausible_end_date((t - timedelta(days=731)).isoformat()) is None
    assert plausible_end_date((t + timedelta(days=7 * 365)).isoformat())
    assert plausible_end_date((t + timedelta(days=7 * 365 + 1)).isoformat()) is None
    assert plausible_end_date("") is None


def test_the_profile_route_refuses_an_impossible_date_and_normalises_a_real_one():
    with pytest.raises(ValidationError) as e:
        server.ProfileUpdate(ead_end_date="2002-04-11")
    assert "real card date" in str(e.value)
    end = date.today() + timedelta(days=90)
    assert server.ProfileUpdate(ead_end_date=end.strftime("%m/%d/%Y")).ead_end_date == end.isoformat()
    assert server.ProfileUpdate(ead_end_date="").ead_end_date == ""        # clearing is allowed
    assert server.ProfileUpdate().ead_end_date is None


def test_the_forms_bound_the_picker_and_blank_a_stored_impossible_date():
    assert "function _plausibleEad(" in DASH and "function _boundEadInput(" in DASH
    m = re.search(r"async function openProfileModal\(\)[\s\S]*?Object\.keys\(data\)", DASH)
    assert m and "_plausibleEad(data.ead_end_date)" in m.group(0)
    assert "_boundEadInput(document.getElementById('opt-ead'))" in DASH
