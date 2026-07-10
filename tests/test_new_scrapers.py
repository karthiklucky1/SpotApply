"""New ATS scrapers: Rippling, Breezy, Pinpoint, Teamtailor (mocked HTTP)."""
from __future__ import annotations

import pytest


class FakeResp:
    def __init__(self, status=200, json_data=None, content=b"", text=""):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _patch(monkeypatch, module, resp):
    monkeypatch.setattr(module.httpx, "get", lambda *a, **k: resp)


def test_rippling(monkeypatch):
    import app.discovery.rippling as m
    _patch(monkeypatch, m, FakeResp(json_data={"items": [
        {"id": "r1", "name": "Backend Engineer", "workLocation": {"label": "Remote"},
         "publishedAt": "2026-07-10T00:00:00Z", "description": "<p>Kafka</p>", "url": "https://x/r1"},
    ]}))
    jobs = m.RipplingScraper("acme").fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Backend Engineer" and jobs[0].source == "rippling"
    assert jobs[0].posted_at is not None and jobs[0].remote is True


def test_breezy(monkeypatch):
    import app.discovery.breezy as m
    _patch(monkeypatch, m, FakeResp(json_data=[
        {"_id": "b1", "name": "Data Scientist", "location": {"city": "NYC", "country": {"name": "USA"}},
         "published_date": "2026-07-09T00:00:00Z", "description": "ML", "url": "https://x/b1"},
    ]))
    jobs = m.BreezyScraper("acme").fetch()
    assert len(jobs) == 1 and jobs[0].title == "Data Scientist"
    assert "NYC" in jobs[0].location and jobs[0].posted_at is not None


def test_pinpoint(monkeypatch):
    import app.discovery.pinpoint as m
    _patch(monkeypatch, m, FakeResp(json_data={"data": [
        {"id": "p1", "attributes": {"title": "Recruiter", "location_name": "London",
         "published_at": "2026-07-08T00:00:00Z", "description": "hiring", "url": "https://x/p1"}},
    ]}))
    jobs = m.PinpointScraper("acme").fetch()
    assert len(jobs) == 1 and jobs[0].title == "Recruiter"
    assert jobs[0].location == "London" and jobs[0].posted_at is not None


def test_teamtailor(monkeypatch):
    import app.discovery.teamtailor as m
    rss = b"""<?xml version="1.0"?><rss><channel>
      <item><title>Frontend Developer - Stockholm</title>
      <link>https://acme.teamtailor.com/jobs/12345</link>
      <pubDate>Wed, 08 Jul 2026 10:00:00 +0000</pubDate>
      <description>React role</description></item>
    </channel></rss>"""
    _patch(monkeypatch, m, FakeResp(content=rss))
    jobs = m.TeamtailorScraper("acme").fetch()
    assert len(jobs) == 1
    assert jobs[0].title == "Frontend Developer" and jobs[0].location == "Stockholm"
    assert jobs[0].source == "teamtailor" and jobs[0].posted_at is not None


def test_scraper_for_wires_new_ats():
    from app.discovery.pipeline import scraper_for
    from app.db.models import JobSource
    for ats in (JobSource.RIPPLING, JobSource.BREEZY, JobSource.PINPOINT, JobSource.TEAMTAILOR):
        assert scraper_for(ats, "acme") is not None


def test_fetch_failure_returns_empty(monkeypatch):
    import app.discovery.rippling as m
    _patch(monkeypatch, m, FakeResp(status=500))
    assert m.RipplingScraper("acme").fetch() == []
