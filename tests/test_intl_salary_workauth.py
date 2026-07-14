"""Currency-aware salary gate + country-aware work-auth framing regressions."""
from types import SimpleNamespace

from app.intelligence.work_auth import assess_profile
from app.matching.filters.rule_filter import RuleFilter, _extract_salary_range


def _profile(**kw):
    base = dict(preferred_country="", requires_sponsorship=False,
                work_authorization="", visa_status="", salary_min=0,
                salary_max=0, salary_currency="USD", key_skills="",
                degree="", years_experience=5, target_roles="engineer",
                remote_ok=True, job_type_preference="full_time")
    base.update(kw)
    return SimpleNamespace(**base)


def _job(desc):
    return SimpleNamespace(title="Software Engineer", company="Acme",
                           location="Remote", remote=True, description=desc,
                           url="https://x", job_type=None)


def test_inr_range_detected():
    assert _extract_salary_range("salary ₹1,500,000 - ₹2,500,000") == (
        1_500_000.0, 2_500_000.0, "INR")


def test_cross_currency_salary_not_filtered():
    # Indian user (INR floor 15L) must NOT reject a $120k-$150k posting.
    f = RuleFilter(profile=_profile(salary_min=1_500_000, salary_currency="INR"))
    res = f.filter(_job("Base pay $120k-$150k. Python, AWS."))
    assert res.passed, res.reason


def test_same_currency_salary_still_filtered():
    f = RuleFilter(profile=_profile(salary_min=200_000, salary_currency="USD"))
    res = f.filter(_job("Base pay $120k-$150k. Python, AWS."))
    assert not res.passed and "Salary too low" in res.reason


def test_workauth_non_us_never_claims_us_authorization():
    fr = assess_profile(_profile(preferred_country="Germany",
                                 work_authorization="EU Blue Card"))
    assert "U.S." not in fr.headline and "Germany" in fr.headline


def test_workauth_non_us_sponsorship_no_h1b_framing():
    fr = assess_profile(_profile(preferred_country="United Kingdom",
                                 requires_sponsorship=True))
    assert "cap-exempt" not in fr.headline
    assert "United Kingdom" in fr.headline


def test_workauth_us_default_unchanged():
    fr = assess_profile(_profile(preferred_country="United States"))
    assert "the U.S." in fr.headline
