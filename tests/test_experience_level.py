"""A posting is matched to the person's experience level — one decision.

Production, 2026-11-24 (counts only): 31 of 35 profiles had 0-4 years of
experience, 14 of them 0. A role paying $347K-$385K was delivered at 80 to a
new graduate; "Lead Engineer" and Senior roles that state no years went to
people with 3. Since 2026-08-15 a quarter of everything delivered (770 of
3,151) carried "Senior" in its title, and people with 3-4 years who OPENED
those acted on 23% of them, against 59% for postings with no level word.

Level was judged in three places, and every one of them skipped a profile
showing 0 years — which is what a new graduate's resume honestly says. The
rule now lives in app/common/level.py; these tests pin it:

  * what "0 years" means (read resume = not worked yet; empty profile = unknown);
  * a number the posting STATES governs, whatever its title says;
  * with none stated, the title's level word decides: Senior from 4 years,
    Lead from 5, Staff+ from 6 — one year short is a stretch (scored, held
    under the bar), further short is never scored;
  * a pay band starting at senior level is a stretch for someone below it;
  * the rule filter drops only what is out of reach, the final check caps the
    stretch, and Tier-1 is not overruled into a final that would be capped;
  * what is NOT a level: Technical Staff, Staff Accountant, Senior Living,
    Lead Generation, a Shift Lead.

Synthetic profiles and postings only.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.common import level
from app.config import settings
from app.db.models import Job, JobSource, UserProfile
from app.matching import reranker as rr
from app.matching.filters.rule_filter import RuleFilter


def _profile(years=4, *, read=True, **kw):
    """A person with ``years`` of experience. ``read`` = their resume has been
    read (the upload stamp) — what tells 0 years apart from an empty profile."""
    base = dict(user_id="lvl-user", years_experience=years, degree="Master of Science",
                key_skills="Python, SQL, Pandas, scikit-learn, FastAPI",
                target_roles="Data Scientist, Machine Learning Engineer, Data Analyst",
                current_title="", preferred_country="United States", remote_ok=True,
                open_to_relocation=True, job_type_preference="full_time",
                work_auth_status="Citizen", work_authorization="US Citizen",
                resume_uploaded_at=datetime(2026, 11, 1) if read else None)
    base.update(kw)
    return UserProfile(**base)


def _job(title, desc="Build models in Python and SQL.", salary=None):
    return Job(source=JobSource.GREENHOUSE, external_id="lvl-x", company="Acme", title=title,
               url="https://x.co/j", location="Chicago, IL", description=desc,
               salary_text=salary, eligibility="eligible")


def _code(years, title, desc="Build models in Python and SQL.", salary="", **kw):
    return level.judge(_profile(years, **kw), title, desc, salary).code


# ── what "0 years" means ─────────────────────────────────────────────────────

def test_zero_years_on_a_read_resume_is_someone_who_has_not_worked_yet():
    assert level.known_years(_profile(0)) == 0.0
    assert level.known_years(_profile(0, read=False, education_json='[{"school": "State U"}]')) == 0.0
    assert level.known_years(_profile(0, read=False, experience_json='[{"title": "Intern"}]')) == 0.0


def test_zero_years_on_an_empty_profile_is_unknown():
    """Nobody has told us anything: skills typed by hand are not a resume, and
    an empty list is not a history."""
    assert level.known_years(_profile(0, read=False)) is None
    assert level.known_years(_profile(0, read=False, education_json="[]", experience_json="")) is None
    assert level.known_years(None) is None
    assert level.known_years(_profile(4, read=False)) == 4.0


# ── the title's level word, when the posting states no years ────────────────

@pytest.mark.parametrize("years,title,expected", [
    # Senior is in reach from 4 years; one short is a stretch; further, not scored.
    (0, "Senior Data Scientist", level.OUT_OF_REACH),
    (2, "Senior Data Scientist", level.OUT_OF_REACH),
    (3, "Senior Data Scientist", level.STRETCH),
    (4, "Senior Data Scientist", ""),
    (4, "Sr. Software Engineer – AI and Automation", ""),
    # Lead from 5.
    (3, "Lead Engineer, Machine Learning", level.OUT_OF_REACH),
    (4, "Lead Engineer, Machine Learning", level.STRETCH),
    (5, "Lead Data Scientist", ""),
    # Staff / Principal / Director / VP / Head of from 6 — never a stretch.
    (0, "Staff ML Engineer", level.OUT_OF_REACH),
    (5, "Principal Engineer", level.OUT_OF_REACH),
    (5, "Director of Data Science", level.OUT_OF_REACH),
    (6, "Staff ML Engineer", ""),
    (16, "Senior Engineering Manager", ""),
    # No level word: nothing to say, whoever is asking.
    (0, "Data Scientist", ""),
    (0, "Junior Data Analyst", ""),
    (0, "2027 University Graduate - Machine Learning Engineer", ""),
    (1, "Software Engineer III, AI/ML", ""),
])
def test_the_title_level_table(years, title, expected):
    assert _code(years, title) == expected


def test_someone_who_targets_or_holds_the_level_gets_it_a_year_sooner():
    """A hand-picked "Senior Data Scientist" target, or a current Senior title,
    is the person saying where they are — one year of benefit, not two."""
    targets = dict(target_roles="Senior Data Scientist, Machine Learning Engineer")
    assert _code(3, "Senior Data Scientist", **targets) == ""
    assert _code(2, "Senior Data Scientist", **targets) == level.STRETCH
    assert _code(1, "Senior Data Scientist", **targets) == level.OUT_OF_REACH
    assert _code(3, "Senior ML Engineer", current_title="Senior Software Engineer") == ""
    # Naming one level does not buy the next one up.
    assert _code(3, "Lead Data Scientist", **targets) == level.OUT_OF_REACH
    # And staff-level titles are not for sale at all.
    assert _code(4, "Principal Engineer", target_roles="Principal Engineer") == level.OUT_OF_REACH


@pytest.mark.parametrize("title", [
    "Member of Technical Staff", "Member of Technical Staff, Applied AI",
    "Staff Accountant", "Staff Nurse - ICU", "Staffing Coordinator", "Chief of Staff",
    "Senior Living Sales Counselor", "Lead Generation Specialist", "Shift Lead", "Lead Cashier",
    "School Principal", "Seniority Analyst",
])
def test_words_that_look_like_a_level_and_are_not_one(title):
    assert level.title_level(title) == ""
    assert _code(0, title) == ""


@pytest.mark.parametrize("title,expected", [
    ("Senior Staff Software Engineer", level.STAFF),
    ("Software Engineer, Staff", level.STAFF),
    ("Associate Principal Engineer - Senior .NET", level.STAFF),
    ("Engineering Manager, Platform", level.STAFF),
    ("Remote-First Lead SRE", level.LEAD),
    ("Data Science Team Lead", level.LEAD),
    ("AI Consulting Engineer - Senior Associate", level.SENIOR),
])
def test_the_highest_level_word_wins(title, expected):
    assert level.title_level(title) == expected


# ── a number the posting states governs ──────────────────────────────────────

def test_stated_years_govern_whatever_the_title_says():
    four = "You have 4+ years of experience designing backend systems in Python."
    # "Senior ..., 4+ years" asks for 4 — in reach at 3 (one short is the scorer's call).
    assert _code(3, "Senior Software Engineer", four) == ""
    assert _code(4, "Senior Software Engineer", four) == ""
    # Two short of a stated number is a stretch; four short is not scored.
    assert _code(3, "Software Engineer", "5+ years of experience in software engineering.") == level.STRETCH
    assert _code(3, "Software Engineer", "Minimum of 7 years of professional experience.") == level.OUT_OF_REACH


def test_a_new_graduate_is_held_to_stated_years_too():
    """THE gap. Every years rule was skipped for a profile showing 0."""
    assert _code(0, "Data Scientist", "2+ years of professional data science experience.") == level.STRETCH
    assert _code(0, "Data Scientist", "5+ years of experience building ML systems.") == level.OUT_OF_REACH
    assert _code(0, "Data Scientist", "1+ years of experience, internships count.") == ""
    assert _code(0, "Data Scientist", "0-2 years of experience.") == ""


def test_an_empty_profile_is_judged_on_nothing_but_staff_titles():
    """Unknown years: exactly what the rule filter did before."""
    unknown = dict(read=False)
    assert _code(0, "Data Scientist", "5+ years of experience building ML systems.", **unknown) == ""
    assert _code(0, "Senior Data Scientist", **unknown) == ""
    assert _code(0, "Staff ML Engineer", **unknown) == level.OUT_OF_REACH


# ── pay that starts at a senior band ────────────────────────────────────────

def test_a_senior_pay_band_is_a_stretch_below_senior_level():
    pay = "$347K – $385K • Offers Equity"
    assert _code(0, "Data Scientist, Analytics Enablement", salary=pay) == level.STRETCH
    assert _code(3, "Applied Data Scientist", salary=pay) == level.STRETCH
    assert _code(4, "Applied Data Scientist", salary=pay) == ""
    # Ordinary pay, a single figure, an hourly range and another currency say nothing.
    for ordinary in ("$165,000 - $195,000", "$137,500", "$60 - $80 per hour", "€300,000 - €400,000", ""):
        assert _code(0, "Data Scientist", salary=ordinary) == ""
    # A stated number or a level word is better evidence, and is read instead.
    assert _code(3, "Data Scientist", "2+ years of experience.", salary=pay) == ""
    assert _code(3, "Senior Data Scientist", salary=pay) == level.STRETCH   # by the title


def test_the_pay_rule_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "level_senior_pay_floor_usd", 0)
    assert _code(0, "Data Scientist", salary="$347K – $385K") == ""


# ── the rule filter drops only what is out of reach ──────────────────────────

def test_the_rule_filter_stamps_out_of_reach_before_any_paid_call():
    grad = RuleFilter(profile=_profile(0))
    res = grad.filter(_job("Senior Data Scientist"))
    assert not res.passed and res.reason.startswith("Title pre-filtered") and res.score_override == 15
    res = grad.filter(_job("Data Scientist", "5+ years of experience building ML systems."))
    assert not res.passed and res.reason.startswith("Experience pre-filtered")
    assert "candidate has 0" in res.reason
    # In reach: goes to the scorer.
    assert grad.filter(_job("Data Scientist")).passed
    assert grad.filter(_job("Junior Data Analyst", "0-2 years of experience.")).passed


def test_the_rule_filter_leaves_a_stretch_for_the_scorer():
    """One level short is scored, so All Jobs can show it with its reasons."""
    three = RuleFilter(profile=_profile(3))
    assert three.filter(_job("Senior Data Scientist")).passed
    assert three.filter(_job("Data Scientist", "5+ years of experience.")).passed
    assert not three.filter(_job("Lead Data Scientist")).passed          # two short


def test_the_rule_filter_keeps_its_old_answers():
    # Unknown years: no years gate, Staff+ still dropped.
    unknown = RuleFilter(profile=_profile(0, read=False))
    assert unknown.filter(_job("Senior Data Scientist")).passed
    assert unknown.filter(_job("Data Scientist", "5+ years of experience.")).passed
    assert "Title pre-filtered" in unknown.filter(_job("Staff ML Engineer")).reason
    # Four years: +4 on stated years, Staff+ under six.
    four = RuleFilter(profile=_profile(4))
    assert four.filter(_job("Software Engineer", "7+ years of experience.")).passed
    assert "requires 8+ years (candidate has 4)" in four.filter(
        _job("Software Engineer", "8+ years of experience.")).reason
    assert "Title pre-filtered" in four.filter(_job("Principal Engineer")).reason
    assert four.filter(_job("Member of Technical Staff")).passed


def test_staff_is_not_a_senior_level_outside_engineering():
    """The bare "staff" substring dropped Staff Accountant and Staff Nurse for
    anyone under six years — entry and ordinary titles in their fields."""
    accountant = RuleFilter(profile=_profile(
        2, target_roles="Staff Accountant, Accountant", key_skills="GAAP, Excel, QuickBooks"))
    assert accountant.filter(_job("Staff Accountant", "Month-end close and reconciliations.")).passed
    assert accountant.filter(_job("Staffing Coordinator", "Schedule interviews.")).passed


# ── the final check holds a stretch under the bar ───────────────────────────

def _final(years, title, desc="Build models in Python and SQL.", score=82.0, salary="", **kw):
    return rr.reconcile_with_profile((score, "fit", ["gap"], {}), _profile(years, **kw), desc,
                                     title=title, salary_text=salary)


def test_a_stretch_is_scored_and_held_under_the_bar():
    score, _r, concerns, _b = _final(3, "Senior Data Scientist")
    assert score == rr.STRETCH_OVERALL_CAP < settings.shortlist_score_threshold
    assert concerns[0] == "Senior-level title; resume shows about 3 years"

    score, _r, concerns, _b = _final(0, "Data Scientist, Analytics Enablement",
                                     salary="$347K – $385K • Offers Equity", score=80.0)
    assert score == rr.STRETCH_OVERALL_CAP
    assert concerns[0].startswith("Pay starts at $347,000, a senior-level band")

    score, _r, concerns, _b = _final(0, "Data Scientist", "2+ years of professional experience.")
    assert score == rr.STRETCH_OVERALL_CAP
    assert concerns[0] == "Posting asks 2+ years; resume shows about 0"


def test_the_final_check_is_also_the_backstop_for_out_of_reach():
    """Normally stamped by the rule filter; a score that arrives anyway (the
    one-time re-check of a board) is held just the same."""
    score, _r, concerns, _b = _final(0, "Senior Data Scientist")
    assert score == rr.STRETCH_OVERALL_CAP and concerns[0].startswith("Senior-level title")
    score, _r, concerns, _b = _final(4, "Principal Engineer")
    assert score == rr.STRETCH_OVERALL_CAP and concerns[0].startswith("Senior/staff-level title")


def test_what_the_final_check_leaves_alone():
    assert _final(4, "Senior Data Scientist")[0] == 82.0
    assert _final(3, "Data Scientist")[0] == 82.0
    assert _final(0, "Data Scientist")[0] == 82.0
    assert _final(0, "Junior Data Analyst", "0-2 years of experience.", score=90.0)[0] == 90.0
    assert _final(3, "Senior Data Scientist",
                  target_roles="Senior Data Scientist, Data Scientist")[0] == 82.0
    # A score already under the cap is never raised to it.
    assert _final(0, "Senior Data Scientist", score=40.0)[0] == 40.0
    # Unknown years: no level cap at all.
    assert _final(0, "Senior Data Scientist", read=False)[0] == 82.0


def test_tier_one_is_not_overruled_into_a_final_that_would_be_capped():
    """`_years_gap_settled` True means "advance this to the final scorer"."""
    senior = _job("Senior Data Scientist")
    assert rr._years_gap_settled(senior, _profile(3)) is False
    assert rr._years_gap_settled(senior, _profile(4)) is True
    assert rr._years_gap_settled(_job("Data Scientist"), _profile(3)) is True
    assert rr._years_gap_settled(
        _job("Data Scientist", salary="$347K – $385K"), _profile(0)) is False


# ── the numbers ──────────────────────────────────────────────────────────────

def test_the_level_thresholds():
    """Senior from 4, Lead from 5, Staff+ from 6 (the rule filter's number
    since the start); the pay floor is far above ordinary pay on purpose."""
    assert (settings.level_senior_min_years, settings.level_lead_min_years,
            settings.level_staff_min_years) == (4, 5, 6)
    assert settings.level_senior_pay_floor_usd == 300_000
    assert rr.STRETCH_OVERALL_CAP < settings.shortlist_score_threshold


def test_the_reader_never_hides_a_job_by_failing():
    class Broken:
        years_experience = 3
        resume_uploaded_at = datetime(2026, 11, 1)

        @property
        def degree(self):
            raise RuntimeError("boom")
    assert not level.judge(Broken(), "Senior Data Scientist", "5+ years of experience.")
