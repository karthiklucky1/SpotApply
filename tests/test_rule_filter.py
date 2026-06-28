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
