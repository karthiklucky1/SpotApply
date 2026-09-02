"""join.com: an empty board and a throttled one must not look the same.

The scraper answered every question with `[]`. A 429, a 422, a 5xx, a connection
timeout and a company with no openings were one indistinguishable outcome, and
two separate consumers read that silence as fact:

  * the pulse lane recorded a SUCCESSFUL poll of a zero-job board, writing
    "no jobs" over the board's state and resetting its failure count
  * the registry validator counted it as a failed validation, and seven of
    those retire a board for thirty days

join.com is the largest ATS in the dataset (~23.5K companies) and throttles
hard, so seven consecutive throttles is an ordinary afternoon — not a dead
company. These tests pin the distinction at both layers.

NOT covered here, because it cannot be verified from this environment: the page
size, the pagination envelope's field name, and the safe page cap. Those encode
observations about join.com's live API, and join.com is unreachable behind the
egress proxy. They are deliberately left exactly as they were.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.discovery import join as join_mod
from app.discovery.join import JoinScraper, JoinTransientError
from app.discovery.registry import is_indeterminate_error

_SLUG = "acme"
_COMPANY_PAGE = '<html>{"company":{"id":9001,"domain":"acme"}}</html>'
_PLACEHOLDER_PAGE = '<html>{"company":{"id":233,"domain":"join-placeholder"}}</html>'


def _job(i, **over):
    d = {"id": i, "name": f"Engineer {i}", "publishedAt": "2026-09-01T00:00:00Z",
         "location": {"cityName": "Berlin", "countryName": "Germany"},
         "description": "<p>Build things.</p>"}
    d.update(over)
    return d


class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._payload


def _client(handler):
    """Patch httpx.Client so JoinScraper's `with httpx.Client(...)` gets ours."""
    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            return handler(url, params or {})

    return _C


@pytest.fixture
def patch_client(monkeypatch):
    def apply(handler):
        monkeypatch.setattr(join_mod.httpx, "Client", _client(handler))
    return apply


# ── a genuinely empty board is still empty ───────────────────────────────────

def test_a_board_with_no_openings_returns_empty(patch_client):
    def h(url, params):
        if "/companies/acme" in url and "/api/" not in url:
            return _Resp(200, _COMPANY_PAGE)
        return _Resp(200, payload={"items": [], "pagination": {"totalPages": 1}})

    patch_client(h)
    s = JoinScraper(_SLUG)
    assert s.fetch() == []
    assert s.fetch_complete is True, "an empty board is a COMPLETE answer"


def test_a_missing_company_returns_empty(patch_client):
    patch_client(lambda url, params: _Resp(404, ""))
    assert JoinScraper(_SLUG).fetch() == []


# ── a throttle is not an empty board ─────────────────────────────────────────

@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_a_transient_status_on_the_jobs_api_raises(patch_client, status):
    """Raising is what routes this into the caller's failure path — backoff and
    a retry — instead of `_mark_polled(ok=True, job_count=0)`."""
    def h(url, params):
        if "/api/" not in url:
            return _Resp(200, _COMPANY_PAGE)
        return _Resp(status)

    patch_client(h)
    with pytest.raises(JoinTransientError):
        JoinScraper(_SLUG).fetch()


@pytest.mark.parametrize("status", [429, 503])
def test_a_transient_status_on_the_company_page_raises(patch_client, status):
    patch_client(lambda url, params: _Resp(status))
    with pytest.raises(JoinTransientError):
        JoinScraper(_SLUG).fetch()


def test_a_connection_timeout_raises(patch_client):
    def h(url, params):
        raise httpx.ConnectTimeout("timed out")

    patch_client(h)
    with pytest.raises(JoinTransientError):
        JoinScraper(_SLUG).fetch()


def test_a_contract_error_raises_rather_than_reporting_an_empty_board(patch_client):
    """422 — the shape of the request was wrong. That is a bug on our side or a
    changed API, and either way it is not evidence the company stopped hiring."""
    def h(url, params):
        return _Resp(200, _COMPANY_PAGE) if "/api/" not in url else _Resp(422)

    patch_client(h)
    with pytest.raises(JoinTransientError):
        JoinScraper(_SLUG).fetch()


def test_unparseable_json_raises(patch_client):
    def h(url, params):
        return _Resp(200, _COMPANY_PAGE) if "/api/" not in url else _Resp(200, payload=None)

    patch_client(h)
    with pytest.raises(JoinTransientError):
        JoinScraper(_SLUG).fetch()


# ── a throttle PART WAY through keeps what we read, and says so ──────────────

def test_a_throttle_on_a_later_page_keeps_the_jobs_and_flags_partial(patch_client):
    """Page 1 succeeded, so we hold real postings for this board. Throwing them
    away would be worse than keeping them — but reporting them as the whole
    board would ghost-close everything on page 2."""
    def h(url, params):
        if "/api/" not in url:
            return _Resp(200, _COMPANY_PAGE)
        if params.get("page") == 1:
            return _Resp(200, payload={"items": [_job(1), _job(2)],
                                       "pagination": {"totalPages": 3}})
        return _Resp(429)

    patch_client(h)
    s = JoinScraper(_SLUG)
    jobs = s.fetch()
    assert len(jobs) == 2
    assert s.fetch_complete is False, (
        "a truncated walk reported as complete lets the pipeline ghost-close "
        "every posting we never loaded"
    )


def test_hitting_the_page_cap_flags_partial(patch_client):
    """Not an error — but still a subset, and the difference matters downstream."""
    def h(url, params):
        if "/api/" not in url:
            return _Resp(200, _COMPANY_PAGE)
        p = params.get("page", 1)
        return _Resp(200, payload={"items": [_job(p * 100 + i) for i in range(3)],
                                   "pagination": {"totalPages": 99}})

    patch_client(h)
    s = JoinScraper(_SLUG)
    jobs = s.fetch()
    assert len(jobs) == 3 * join_mod._MAX_PAGES
    assert s.fetch_complete is False


# ── deduplication ────────────────────────────────────────────────────────────

def test_a_posting_repeated_across_pages_is_ingested_once(patch_client):
    """A board that changes mid-walk shifts postings between pages, so the same
    id arrives twice — and every poll then re-upserts the same row."""
    def h(url, params):
        if "/api/" not in url:
            return _Resp(200, _COMPANY_PAGE)
        page = params.get("page", 1)
        items = [_job(1), _job(2)] if page == 1 else [_job(2), _job(3)]
        return _Resp(200, payload={"items": items, "pagination": {"totalPages": 2}})

    patch_client(h)
    jobs = JoinScraper(_SLUG).fetch()
    assert [j.external_id for j in jobs] == ["1", "2", "3"]


def test_signature_entries_track_the_deduped_postings(patch_client):
    """The pulse lane hashes these to decide whether a board changed; a
    duplicate would make an unchanged board look different every poll."""
    def h(url, params):
        if "/api/" not in url:
            return _Resp(200, _COMPANY_PAGE)
        return _Resp(200, payload={"items": [_job(1), _job(1), _job(2)],
                                   "pagination": {"totalPages": 1}})

    patch_client(h)
    s = JoinScraper(_SLUG)
    s.fetch()
    assert s.signature_entries == [("1", "Engineer 1"), ("2", "Engineer 2")]


# ── the placeholder guard has no way around it ───────────────────────────────

def test_the_placeholder_company_is_rejected_with_no_fallback(patch_client):
    """join.com serves company 233 for empty tenants. The domain check existed
    to catch that — and the line after it returned the first id on the page when
    no domain matched, which is precisely the placeholder. Every empty tenant
    scraped the placeholder's jobs under the wrong company name."""
    calls: list[str] = []

    def h(url, params):
        calls.append(url)
        return _Resp(200, _PLACEHOLDER_PAGE)

    patch_client(h)
    assert JoinScraper("newco").fetch() == []
    assert not any("/api/" in u for u in calls), (
        "resolved a company id that did not match the slug and went on to "
        "scrape someone else's jobs"
    )


def test_a_matching_domain_still_resolves(patch_client):
    """The guard must not be so tight that real boards stop resolving."""
    def h(url, params):
        if "/api/" not in url:
            return _Resp(200, _COMPANY_PAGE)
        return _Resp(200, payload={"items": [_job(7)], "pagination": {"totalPages": 1}})

    patch_client(h)
    jobs = JoinScraper(_SLUG).fetch()
    assert [j.external_id for j in jobs] == ["7"]
    assert jobs[0].company == "Acme"


# ── the retirement path ──────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "API returned status 429",
    "API returned status 503",
    "API returned status 502",
    "API returned status 408",
    "Hiring page returned status 500",
    "ConnectTimeout: timed out",
    "Connection reset by peer",
    "Too Many Requests",
])
def test_throttles_and_transport_failures_are_not_evidence(msg):
    """Seven strikes retire a board for thirty days. A board that rate-limited
    us seven times is a board that answered seven times."""
    assert is_indeterminate_error(msg) is True


@pytest.mark.parametrize("msg", [
    "API returned status 404",
    "API returned status 410",
    "API returned status 403",
    "No career URL available for manual ATS",
    "XML parse error",
])
def test_conclusive_errors_still_count_as_failures(msg):
    """The guard can only ever SPARE a board. A dead one must still retire, or
    the registry fills with slugs nobody can fetch."""
    assert is_indeterminate_error(msg) is False


def test_no_error_is_not_indeterminate():
    """A board that answered 200 with zero jobs really is inactive."""
    assert is_indeterminate_error(None) is False
    assert is_indeterminate_error("") is False


# ── the OTHER retirement path ────────────────────────────────────────────────
# The discovery pipeline retires a board after BOARD_DEACTIVATE_AFTER_FAILURES
# failed fetches. Making the scraper RAISE routed join's throttles into that
# counter for the first time — a regression the first production deploy of this
# change surfaced within a minute: 429s were already spared, but 422s were not,
# and five discovery runs would have retired the board.

@pytest.mark.parametrize("error", [
    "join jobs API returned 429",
    "join jobs API returned 422",
    "HTTP 422",
    "API returned status 422",
    "Too Many Requests",
])
def test_a_throttle_or_contract_error_never_retires_the_board(error):
    """Retirement is a thirty-day sentence passed on five data points, so what
    counts as a data point matters. A board that asked us to slow down, or that
    rejected the SHAPE of our request, has told us nothing about whether it is
    alive — and 23.5K join companies would have been retired for our page size."""
    from app.discovery.pipeline import _is_throttled
    assert _is_throttled(error) is True, error


@pytest.mark.parametrize("error", [
    "board_not_found (404)",
    "API returned status 404",
    "API returned status 403",
    "No career URL available for manual ATS",
    "XML parse error",
    # 5xx and transport failures are deliberately NOT spared here: a board that
    # answers 500 forever is unusable whatever the cause. test_board_throttling
    # pins that policy; this test pins that widening the contract-error
    # exemption did not quietly change it.
    "HTTP 503",
    "HTTP 500",
    "connection timeout",
])
def test_a_real_fault_still_counts_toward_retirement(error):
    """The exemption can only ever SPARE a board that answered. A dead one must
    still retire, or the registry fills with slugs nobody can fetch."""
    from app.discovery.pipeline import _is_throttled
    assert _is_throttled(error) is False, error
