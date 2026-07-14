"""International sponsorship + salary handling: refusal phrasing from any
country is caught, H-1B intelligence only applies to US postings, and salary
parsing understands £/€."""
from app.intelligence.sponsorship import assess, SponsorshipLikelihood
from app.matching.filters.rule_filter import RuleFilter, _extract_salary_range
from app.db.models import Job, JobSource


def test_assess_non_us_posting_skips_h1b_messaging():
    a = assess(company="Monzo", description="Great fintech role.",
               url="https://monzo.com/careers", location="London, UK")
    assert a.likelihood == SponsorshipLikelihood.UNKNOWN
    assert "h-1b" not in a.reason.lower()
    assert a.badge == "Check visa policy"


def test_assess_non_us_university_not_marked_cap_exempt():
    # Cap-exempt is a US H-1B concept — a UK university must not get the badge.
    a = assess(company="University of Oxford", description="Research engineer.",
               url="https://ox.ac.uk", location="Oxford, United Kingdom")
    assert a.cap_exempt is False


def test_assess_refusal_is_universal():
    a = assess(company="AcmeCo",
               description="Applicants must have the right to work in the UK. No sponsorship.",
               location="Manchester, United Kingdom")
    assert a.likelihood == SponsorshipLikelihood.LOW
    assert a.explicitly_refuses is True


def test_assess_us_posting_keeps_h1b_intelligence():
    a = assess(company="Stanford University", description="Lab engineer.",
               url="https://stanford.edu/jobs", location="Stanford, CA")
    assert a.cap_exempt is True
    assert "no lottery" in a.reason.lower()


def test_rule_filter_blocks_international_refusal_phrasings():
    f = RuleFilter()  # legacy => requires_sponsorship=True
    job = Job(source=JobSource.GREENHOUSE, external_id="i18n-1", company="AcmeCo",
              title="Software Engineer", location="Remote", url="http://x",
              description="You must be authorised to work in the UK without sponsorship.")
    res = f.filter(job)
    assert res.passed is False
    assert "Sponsorship pre-filtered" in res.reason


def test_salary_range_parses_pounds_and_euros():
    assert _extract_salary_range("salary £90k–£110k per annum") == (90_000.0, 110_000.0, "GBP")
    assert _extract_salary_range("compensation: €70,000 - €90,000") == (70_000.0, 90_000.0, "EUR")
    assert _extract_salary_range("base pay $120k-$150k") == (120_000.0, 150_000.0, "USD")
