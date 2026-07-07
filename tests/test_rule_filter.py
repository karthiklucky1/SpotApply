import pytest
from app.db.models import Job, JobSource
from app.matching.filters.rule_filter import RuleFilter

def test_rule_filter_non_us_location():
    filter_engine = RuleFilter()
    
    # Inside US (or empty) should pass
    job_us = Job(
        source=JobSource.GREENHOUSE,
        external_id="123",
        company="TestCo",
        title="Software Engineer",
        location="San Francisco, CA",
        url="http://test.com",
        description="We are looking for a Software Engineer. Python/ML is a plus."
    )
    res = filter_engine.filter(job_us)
    assert res.passed is True
    
    # Outside US should fail
    job_london = Job(
        source=JobSource.GREENHOUSE,
        external_id="124",
        company="TestCo",
        title="Software Engineer",
        location="London, UK",
        url="http://test.com",
        description="We are looking for a Software Engineer."
    )
    res = filter_engine.filter(job_london)
    assert res.passed is False
    assert "Location pre-filtered" in res.reason

def test_rule_filter_no_sponsorship():
    filter_engine = RuleFilter()
    
    job_no_spons = Job(
        source=JobSource.GREENHOUSE,
        external_id="125",
        company="TestCo",
        title="Software Engineer",
        location="Remote",
        url="http://test.com",
        description="We do not offer visa sponsorship for this role. Candidates must be US citizens."
    )
    res = filter_engine.filter(job_no_spons)
    assert res.passed is False
    assert "Sponsorship pre-filtered" in res.reason

def test_rule_filter_experience_gap():
    filter_engine = RuleFilter()
    
    # 8+ years (hard block)
    job_8yoe = Job(
        source=JobSource.GREENHOUSE,
        external_id="126",
        company="TestCo",
        title="Software Engineer",
        location="Remote",
        url="http://test.com",
        description="Requires 8+ years of relevant experience in machine learning."
    )
    res = filter_engine.filter(job_8yoe)
    assert res.passed is False
    assert "Experience pre-filtered" in res.reason

    # 9+ years, plain title — isolates the experience gate (legacy cutoff = 3+4 = 7)
    job_9yoe = Job(
        source=JobSource.GREENHOUSE,
        external_id="127",
        company="TestCo",
        title="Software Engineer",
        location="Remote",
        url="http://test.com",
        description="Requires 9+ years of experience."
    )
    res = filter_engine.filter(job_9yoe)
    assert res.passed is False
    assert "Experience pre-filtered" in res.reason

    # A profile reporting 0 years (student / new grad) means "unknown" —
    # the experience gate is skipped so junior/intern roles still surface.
    class _Prof:
        years_experience = 0
        key_skills = ""
        degree = ""
    student_filter = RuleFilter(profile=_Prof())
    res_student = student_filter.filter(job_9yoe)
    assert "Experience pre-filtered" not in res_student.reason

class _IntlProf:
    """Minimal profile stub for a non-US user."""
    years_experience = 0
    key_skills = ""
    degree = ""
    preferred_country = "United Kingdom"
    requires_sponsorship = False


def _mk_job(location, remote=False, external_id="200", title="Software Engineer"):
    return Job(
        source=JobSource.GREENHOUSE,
        external_id=external_id,
        company="TestCo",
        title=title,
        location=location,
        remote=remote,
        url="http://test.com",
        description="We are looking for a Software Engineer.",
    )


def test_rule_filter_country_aware_for_uk_user():
    uk_filter = RuleFilter(profile=_IntlProf())

    # A UK user KEEPS London jobs (previously hard-rejected as "outside the US").
    res = uk_filter.filter(_mk_job("London, UK", external_id="201"))
    assert res.passed is True

    # ...and drops onsite US jobs.
    res = uk_filter.filter(_mk_job("New York, NY", external_id="202"))
    assert res.passed is False
    assert "Location pre-filtered" in res.reason


def test_rule_filter_country_unknown_location_kept():
    uk_filter = RuleFilter(profile=_IntlProf())
    res = uk_filter.filter(_mk_job("Main Office", external_id="203"))
    assert "Location pre-filtered" not in res.reason


def test_rule_filter_country_remote_exempt():
    # Remote roles are never location-filtered, whatever country they advertise.
    uk_filter = RuleFilter(profile=_IntlProf())
    res = uk_filter.filter(_mk_job("San Francisco, CA", remote=True, external_id="204"))
    assert "Location pre-filtered" not in res.reason

    us_filter = RuleFilter()  # legacy default targets the US
    res = us_filter.filter(_mk_job("Berlin, Germany", remote=True, external_id="205"))
    assert "Location pre-filtered" not in res.reason


def test_rule_filter_staff_titles():
    filter_engine = RuleFilter()
    
    job_staff = Job(
        source=JobSource.GREENHOUSE,
        external_id="128",
        company="TestCo",
        title="Staff AI/ML Engineer",
        location="Remote",
        url="http://test.com",
        description="Looking for a leader."
    )
    res = filter_engine.filter(job_staff)
    assert res.passed is False
    assert "Title pre-filtered" in res.reason
