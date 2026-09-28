"""SpotApply is for every field, not only tech (2026-09-28).

Production (counts only): a mechanical-engineering student got 83 mechanical
roles out of 107 while active — departments already worked for engineering. But
three doors still closed on other fields:

- no department for nursing, pharmacy, therapy, education, HR, sales or legal,
  so those users got no suggested roles and no discovery fallback;
- the title filter's "junk" list (HR, recruiter, account executive, customer
  success) rejected those titles for EVERY user — an HR or sales user could
  never receive their own jobs;
- Workday and SmartRecruiters (the ATSs most hospitals use) skipped every nurse,
  medical, accounting, HR and legal posting before it reached the shared pool.

Synthetic titles and profiles only.
"""
from __future__ import annotations

import pytest

from app.api import server
from app.discovery import smartrecruiters, title_filter, workday
from app.discovery.title_filter import (is_obvious_non_tech, matches_title,
                                        role_title_match, set_title_demand)


class _Prof:
    def __init__(self, **kw):
        self.industry = ""
        self.degree = ""
        self.current_title = ""
        self.key_skills = ""
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture(autouse=True)
def _no_demand():
    """The demand is process-wide; never leak it into another test."""
    before = title_filter.title_demand()
    set_title_demand([])
    yield
    set_title_demand(list(before))


# ── departments: every dropdown value maps to its own field ──────────────────

@pytest.mark.parametrize("industry,dept,first_role", [
    ("Nursing", "nursing", "Registered Nurse"),
    ("Pharmacy", "pharmacy", "Pharmacist"),
    ("Physical / Occupational Therapy & Allied Health", "allied_health", "Physical Therapist"),
    ("Healthcare / Public Health", "healthcare", "Healthcare Data Analyst"),
    ("Education / Teaching", "education", "Teacher"),
    ("Human Resources", "hr", "Human Resources Generalist"),
    ("Sales / Business Development", "sales", "Sales Representative"),
    ("Legal / Paralegal", "legal", "Paralegal"),
    ("Civil Engineering", "civil", "Civil Engineer"),
    ("Mechanical Engineering", "mechanical", "Mechanical Engineer"),
    ("Electrical Engineering", "electrical", "Electrical Engineer"),
])
def test_each_department_has_its_own_roles(industry, dept, first_role):
    p = _Prof(industry=industry)
    assert server._detect_department(p) == dept
    assert server._suggest_roles(p)[0] == first_role


def test_every_dropdown_option_is_recognised():
    """A department in the form that the server cannot read is a user who
    silently gets software suggestions."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app/templates/dashboard.html").read_text()
    select = re.search(r'<select name="industry".*?</select>', html, re.S).group(0)
    values = [v.replace("&amp;", "&") for v in re.findall(r'<option value="([^"]+)"', select)]
    assert len(values) >= 20
    unread = [v for v in values
              if v not in ("Other", "Software Engineering", "Data Science & Analytics")
              and server._detect_department(_Prof(industry=v)) is None]
    assert unread == []


def test_a_nursing_degree_is_nursing_not_generic_healthcare():
    p = _Prof(industry="Healthcare / Public Health", degree="Bachelor of Science in Nursing")
    assert server._detect_department(p) == "nursing"


# ── the title filter keeps each field's own jobs ─────────────────────────────

NURSE = ["Registered Nurse", "ICU Nurse"]
HR = ["Human Resources Generalist", "Recruiter", "Talent Acquisition Specialist"]
SALES = ["Account Executive", "Sales Representative", "Customer Success Manager"]
LEGAL = ["Paralegal", "Legal Assistant"]
TEACH = ["Teacher", "Special Education Teacher"]


@pytest.mark.parametrize("title,roles", [
    ("Registered Nurse - ICU (Nights)", NURSE),
    ("Nurse Manager, Emergency Department", NURSE),
    ("HR Generalist", HR),
    ("Human Resources Generalist II", HR),
    ("Technical Recruiter", HR),
    ("Account Executive, Mid-Market", SALES),
    ("Customer Success Manager", SALES),
    ("Paralegal - Litigation", LEGAL),
    ("Special Education Teacher (K-5)", TEACH),
])
def test_a_field_user_receives_their_own_titles(title, roles):
    assert matches_title(title, roles), "the discovery/role gate"
    assert role_title_match(title, roles), "the routing gate"


def test_junk_stays_junk_for_everyone_else():
    # Pinned in test_department_discovery too: a mechanical user's "HVAC"
    # never rescues a sales job, and nobody's default feed gets HR titles.
    assert not matches_title("Sales Engineer - HVAC Systems", ["HVAC Engineer"])
    assert not matches_title("HR Generalist", ["Software Engineer"])
    assert not matches_title("Account Executive")
    # An HR user does not start receiving SALES roles either.
    assert not matches_title("Account Executive, Mid-Market", HR)


# ── the board scrapers keep what somebody is looking for ─────────────────────

def test_scrapers_share_one_gate():
    assert workday._is_obvious_non_tech is is_obvious_non_tech
    assert smartrecruiters._is_obvious_non_tech is is_obvious_non_tech


def test_nobody_asking_keeps_the_old_skip():
    assert is_obvious_non_tech("Registered Nurse - ICU")
    assert is_obvious_non_tech("Staff Accountant")
    assert not is_obvious_non_tech("Software Engineer")


def test_demand_keeps_a_nurse_posting_on_workday():
    set_title_demand(["Software Engineer", "Registered Nurse"])
    assert not is_obvious_non_tech("Registered Nurse - ICU")
    # ...and only what is asked for: the accountant posting is still skipped.
    assert is_obvious_non_tech("Staff Accountant")


def test_an_explicit_keyword_list_wins_over_demand():
    set_title_demand(["Registered Nurse"])
    assert is_obvious_non_tech("Registered Nurse - ICU", [])
    assert not is_obvious_non_tech("Staff Accountant", ["Staff Accountant"])


def test_the_upsert_door_verdict_is_unchanged():
    """pipeline.is_obvious_non_tech is the bare regex; the door pairs it with
    the caller's own keywords, never with the process-wide demand."""
    from app.discovery.pipeline import is_obvious_non_tech as door
    set_title_demand(["Registered Nurse"])
    assert door("Registered Nurse - ICU") is True


def test_the_shared_discovery_pass_publishes_demand(monkeypatch):
    from app.discovery import pipeline
    seen = {}

    class _Stop(Exception):
        pass

    def _publish(keywords):
        seen["demand"] = list(keywords)
        raise _Stop       # published before any network or DB work

    monkeypatch.setattr(title_filter, "set_title_demand", _publish)
    with pytest.raises(_Stop):
        pipeline.run_discovery(user_id=pipeline.SHARED_POOL_USER,
                               keywords=["Registered Nurse", "Paralegal"])
    assert seen["demand"] == ["Registered Nurse", "Paralegal"]
