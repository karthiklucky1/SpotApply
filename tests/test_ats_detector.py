import pytest
from app.discovery.resolver import ATSDetector, CareerResolver

def test_ats_detector_from_url():
    test_cases = [
        ("https://boards.greenhouse.io/openai", ("greenhouse", "openai")),
        ("https://jobs.lever.co/anthropic", ("lever", "anthropic")),
        ("https://jobs.ashbyhq.com/openai", ("ashby", "openai")),
        ("https://stripe.myworkdayjobs.com/stripecareers", ("workday", "stripe")),
        ("https://careers.smartrecruiters.com/deliveryhero", ("smartrecruiters", "deliveryhero")),
        ("https://apply.workable.com/vercel", ("workable", "vercel")),
        ("https://spacex.bamboohr.com/jobs/", ("bamboohr", "spacex")),
        ("https://careers-stripe.icims.com", ("icims", "careers-stripe")),
        ("https://www.jobvite.com/company-name", ("jobvite", "company-name")),
        ("https://comeet.co/company-name", ("comeet", "company-name")),
        ("https://company.teamtailor.com/", ("teamtailor", "company")),
        # Without protocols
        ("spacex.bamboohr.com/jobs/", ("bamboohr", "spacex")),
        ("company.teamtailor.com/", ("teamtailor", "company")),
    ]
    
    for url, expected in test_cases:
        res = ATSDetector.detect_from_url(url)
        assert res == expected, f"Failed on URL: {url}"

def test_ats_detector_from_html():
    html = """
    <html>
        <body>
            <a href="https://boards.greenhouse.io/openai">Apply here</a>
        </body>
    </html>
    """
    res = ATSDetector.detect_from_html(html, "https://homepage.com")
    assert res == ("greenhouse", "openai")

def test_ats_detector_from_html_iframe():
    html = """
    <html>
        <body>
            <iframe src="https://company.teamtailor.com/"></iframe>
        </body>
    </html>
    """
    res = ATSDetector.detect_from_html(html, "https://homepage.com")
    assert res == ("teamtailor", "company")

@pytest.mark.anyio
async def test_career_resolver_candidates():
    resolver = CareerResolver()
    try:
        candidates = await resolver.resolve_careers_url("https://example.com")
        assert "https://example.com/careers" in candidates
        assert "https://example.com/jobs" in candidates
    finally:
        await resolver.close()
