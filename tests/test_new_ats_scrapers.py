"""Workable / Recruitee / Personio scrapers — fixture-based parser tests
(no network; the HTTP layer is patched)."""
from unittest.mock import patch

import httpx

from app.discovery.workable import WorkableScraper
from app.discovery.recruitee import RecruiteeScraper
from app.discovery.personio import PersonioScraper


def _resp(json_body=None, text_body=None, status=200):
    r = httpx.Response(
        status,
        json=json_body,
        text=text_body,
        request=httpx.Request("GET", "https://example.test/"),
    )
    return r


WORKABLE_PAYLOAD = {
    "name": "Netdata",
    "jobs": [{
        "title": "Backend Engineer",
        "shortcode": "AB12CD",
        "city": "Athens",
        "country": "Greece",
        "telecommuting": True,
        "url": "https://apply.workable.com/netdata/j/AB12CD/",
        "application_url": "https://apply.workable.com/netdata/j/AB12CD/apply/",
        "published_on": "2026-07-01",
        "description": "<p>Build APIs with <b>Python</b>.</p>",
    }],
}


def test_workable_parses_jobs():
    with patch("httpx.get", return_value=_resp(json_body=WORKABLE_PAYLOAD)):
        jobs = WorkableScraper("netdata").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "workable"
    assert j.external_id == "AB12CD"
    assert j.company == "Netdata"
    assert j.remote is True
    assert "Athens" in j.location and "Greece" in j.location
    assert "Build APIs" in j.description and "<p>" not in j.description
    assert j.posted_at is not None
    assert j.url.endswith("/apply/")


RECRUITEE_PAYLOAD = {
    "offers": [{
        "id": 987,
        "title": "Data Engineer",
        "slug": "data-engineer",
        "status": "published",
        "location": "Amsterdam, Netherlands",
        "remote": False,
        "careers_url": "https://sendcloud.recruitee.com/o/data-engineer",
        "description": "<ul><li>Pipelines</li></ul>",
        "requirements": "<p>Python, SQL</p>",
        "published_at": "2026-07-02T09:00:00Z",
    }, {
        "id": 988,
        "title": "Closed Role",
        "status": "closed",
    }],
}


def test_recruitee_parses_and_skips_closed():
    with patch("httpx.get", return_value=_resp(json_body=RECRUITEE_PAYLOAD)):
        jobs = RecruiteeScraper("sendcloud").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "recruitee"
    assert j.external_id == "987"
    assert "Amsterdam" in j.location
    assert "Pipelines" in j.description and "Python, SQL" in j.description
    assert j.url == "https://sendcloud.recruitee.com/o/data-engineer"
    assert j.posted_at is not None


PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>555</id>
    <name>ML Engineer</name>
    <office>Berlin</office>
    <schedule>full-time</schedule>
    <createdAt>2026-06-30T08:00:00Z</createdAt>
    <jobDescriptions>
      <jobDescription>
        <name>Your role</name>
        <value>&lt;p&gt;Train models.&lt;/p&gt;</value>
      </jobDescription>
    </jobDescriptions>
  </position>
  <position>
    <id></id>
    <name>Broken entry</name>
  </position>
</workzag-jobs>
"""


def test_personio_parses_xml():
    with patch("httpx.get", return_value=_resp(text_body=PERSONIO_XML)):
        jobs = PersonioScraper("enpal").fetch()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "personio"
    assert j.external_id == "555"
    assert j.title == "ML Engineer"
    assert j.location == "Berlin"
    assert "Train models." in j.description
    assert j.url == "https://enpal.jobs.personio.de/job/555"
    assert j.posted_at is not None


def test_scrapers_survive_http_errors():
    err = httpx.ConnectError("down")
    with patch("httpx.get", side_effect=err):
        assert WorkableScraper("x").fetch() == []
        assert RecruiteeScraper("x").fetch() == []
        assert PersonioScraper("x").fetch() == []


def test_all_scrapers_dispatches_new_providers():
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import CompanyRegistry, JobSource
    from app.discovery.pipeline import _all_scrapers

    with get_session() as s:
        for slug, ats in (("wtest", JobSource.WORKABLE),
                          ("rtest", JobSource.RECRUITEE),
                          ("ptest", JobSource.PERSONIO)):
            if not s.exec(select(CompanyRegistry).where(
                    CompanyRegistry.slug == slug, CompanyRegistry.ats == ats)).first():
                s.add(CompanyRegistry(slug=slug, ats=ats, source="test"))
        s.commit()
    try:
        names = {(sc.name, getattr(sc, "board_slug", None)) for sc in _all_scrapers()}
        assert ("workable", "wtest") in names
        assert ("recruitee", "rtest") in names
        assert ("personio", "ptest") in names
    finally:
        with get_session() as s:
            for slug in ("wtest", "rtest", "ptest"):
                for row in s.exec(select(CompanyRegistry).where(CompanyRegistry.slug == slug)).all():
                    s.delete(row)
            s.commit()
