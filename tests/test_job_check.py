"""Ghost-check / fit-check for arbitrary job URLs (mocked HTTP)."""
from __future__ import annotations

import json

import pytest

import app.intelligence.job_check as jc


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", url="", history=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.url = url
        self.history = history or []

    def json(self):
        return self._json


class FakeClient:
    """Maps URL substrings → FakeResponse."""
    def __init__(self, routes):
        self.routes = routes

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        for frag, resp in self.routes.items():
            if frag in url:
                if not resp.url:
                    resp.url = url
                return resp
        return FakeResponse(status_code=404, url=url)


def _patch(monkeypatch, routes):
    monkeypatch.setattr(jc.httpx, "Client", lambda **kw: FakeClient(routes))


def test_greenhouse_live_posting(monkeypatch):
    _patch(monkeypatch, {
        "api.greenhouse.io/v1/boards/acme/jobs/123": FakeResponse(200, {
            "title": "Backend Engineer",
            "updated_at": "2026-07-08T00:00:00Z",
            "content": "<p>Python and Kafka. Salary: $150,000. " + "word " * 200 + "</p>",
        }),
    })
    out = jc.check_job_url("https://boards.greenhouse.io/acme/jobs/123")
    assert out["ok"] and out["live"] is True
    assert out["ats"] == "greenhouse" and out["title"] == "Backend Engineer"
    assert out["ghost_score"] < 0.6
    assert any(s.startswith("fresh_posting") for s in out["signals"])


def test_greenhouse_removed_posting_is_ghost(monkeypatch):
    _patch(monkeypatch, {
        "api.greenhouse.io/v1/boards/acme/jobs/999": FakeResponse(404),
    })
    out = jc.check_job_url("https://job-boards.greenhouse.io/acme/jobs/999")
    assert out["live"] is False
    assert out["ghost_score"] == 1.0
    assert "posting_closed_or_removed" in out["signals"]


def test_generic_page_closed_phrase(monkeypatch):
    _patch(monkeypatch, {
        "example.com": FakeResponse(200, text="<html>This job is no longer available</html>"),
    })
    out = jc.check_job_url("https://example.com/careers/swe-1")
    assert out["live"] is False and out["ghost_score"] == 1.0


def test_generic_page_jsonld_stale(monkeypatch):
    ld = json.dumps({"@type": "JobPosting", "title": "Data Engineer",
                     "hiringOrganization": {"name": "Acme"},
                     "datePosted": "2026-01-01"})
    html = f'<html><script type="application/ld+json">{ld}</script>' + "word " * 200 + "</html>"
    _patch(monkeypatch, {"example.com": FakeResponse(200, text=html)})
    out = jc.check_job_url("https://example.com/careers/data-eng")
    assert out["live"] is True
    assert out["title"] == "Data Engineer" and out["company"] == "Acme"
    assert any(s.startswith("stale_posting") for s in out["signals"])
    assert out["ghost_score"] >= 0.5


def test_fit_check_with_resume(monkeypatch):
    _patch(monkeypatch, {
        "api.lever.co/v0/postings/acme/11111111-1111-1111-1111-111111111111": FakeResponse(200, {
            "text": "ML Engineer",
            "createdAt": 1780000000000,
            "descriptionPlain": "We need python, kafka, and terraform. " + "filler " * 200,
        }),
    })
    out = jc.check_job_url(
        "https://jobs.lever.co/acme/11111111-1111-1111-1111-111111111111",
        resume_text="Python developer with FastAPI experience.",
    )
    assert out["fit"] is not None
    assert 0 <= out["fit"]["score_pct"] <= 100
    assert "kafka" in out["fit"]["missing"]
    assert "python" in out["fit"]["matched"]


def test_linkedin_never_fetched(monkeypatch):
    def _boom(**kw):
        class C:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def get(self, url, **k):
                raise AssertionError(f"page fetch attempted for hands-off domain: {url}")
        return C()
    monkeypatch.setattr(jc.httpx, "Client", _boom)
    out = jc.check_job_url("https://www.linkedin.com/jobs/view/1234")
    assert out["live"] is None
    assert "hands_off_domain_no_page_check" in out["signals"]


def test_invalid_url():
    assert jc.check_job_url("notaurl")["ok"] is False


def test_aggregator_domain_flagged(monkeypatch):
    _patch(monkeypatch, {"lensa.com": FakeResponse(200, text="<html>" + "word " * 200 + "</html>")})
    out = jc.check_job_url("https://lensa.com/some-job")
    assert "aggregator_redirect_domain" in out["signals"]
    assert out["ghost_score"] >= 0.5
