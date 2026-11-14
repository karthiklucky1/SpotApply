"""join.com: an empty board, a throttled one and an unreadable one must not look the same.

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

2026-11-14: the REST listing this adapter read (``/api/public/companies/{id}/
jobs``, five a page) was REMOVED by join.com, and its ``404 Cannot GET`` was
read as "conclusive: nothing more to read" — a successful poll of an empty
board, on every board that got past the throttle (19,766 active boards, 4
showing jobs). The listing is now the GraphQL query join.com's own company
page runs, verified against the live site: one POST for up to 100 postings.
The response shapes below are the real ones.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.discovery import join as join_mod
from app.discovery.base import BoardBlockedError, BoardUnreadableError
from app.discovery.join import JoinScraper, JoinTransientError
from app.discovery.registry import is_indeterminate_error

_SLUG = "acme"
_COMPANY_PAGE = '<html>{"company":{"id":9001,"domain":"acme"}}</html>'
_PLACEHOLDER_PAGE = '<html>{"company":{"id":233,"domain":"join-placeholder"}}</html>'


def _job(i, **over):
    """One `publicJobs` item, in the shape the live query returns."""
    d = {"id": i, "idParam": f"{i}-engineer-{i}", "title": f"Engineer {i}",
         "createdAt": "2026-09-01T00:00:00.000Z", "workplaceType": "ONSITE",
         "city": {"cityName": "Berlin", "countryName": "Germany"}}
    d.update(over)
    return d


def _listing(items, page_count=1, row_count=None):
    """A `publicJobs` answer."""
    return {"data": {"publicJobs": {
        "items": items,
        "pageInfo": {"pageCount": page_count,
                     "rowCount": len(items) if row_count is None else row_count}}}}


class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("no json", "", 0)
        return self._payload


def _client(page_handler, query_handler):
    """Patch httpx.Client: GET → the company page, POST → the jobs query.

    ``query_handler(page)`` gets the page number the scraper asked for.
    """
    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, **kw):
            return page_handler(url)

        def post(self, url, json=None, headers=None, **kw):
            assert url.endswith("/candidate-api/graphql"), url
            inp = json["variables"]["input"]
            return query_handler(inp["paginationPage"]["page"], inp)

    return _C


def _company_page(url):
    """The company page for whichever slug was asked — the scraper refuses a
    company whose embedded domain does not match, so the fixture must agree."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return _Resp(200, '<html>{"company":{"id":9001,"domain":"%s"}}</html>' % slug)


@pytest.fixture
def patch_client(monkeypatch):
    def apply(query_handler, page_handler=_company_page):
        monkeypatch.setattr(join_mod.httpx, "Client",
                            _client(page_handler, lambda page, inp: query_handler(page)))
    return apply


# ── a genuinely empty board is still empty ───────────────────────────────────

def test_a_board_with_no_openings_returns_empty(patch_client):
    patch_client(lambda page: _Resp(200, payload=_listing([])))
    s = JoinScraper(_SLUG)
    assert s.fetch() == []
    assert s.fetch_complete is True, "an empty board is a COMPLETE answer"


def test_a_missing_company_returns_empty(patch_client):
    patch_client(lambda page: pytest.fail("no company — the jobs query must not run"),
                 page_handler=lambda url: _Resp(404, ""))
    assert JoinScraper(_SLUG).fetch() == []


def test_the_live_shape_parses(patch_client):
    """The fields the live query returns, and what each becomes."""
    patch_client(lambda page: _Resp(200, payload=_listing([
        _job(16945871, idParam="16945871-sales-manager-in", title="Sales Manager*in",
             createdAt="2026-11-13T09:53:09.111Z", workplaceType="HYBRID",
             city={"cityName": "Dülmen", "countryName": "Germany"})])))
    s = JoinScraper("farmers")
    (j,) = s.fetch()
    assert j.source == "join" and j.external_id == "16945871", (
        "the numeric id stays the key — every stored join row is keyed by it")
    assert j.url == "https://join.com/companies/farmers/jobs/16945871-sales-manager-in", (
        "the page a person opens is addressed by idParam")
    assert j.title == "Sales Manager*in" and j.location == "Dülmen, Germany"
    assert j.posted_at is not None and j.posted_at.year == 2026
    assert j.geo.country == "Germany" and j.geo.country_field == "city.countryName"
    assert j.geo.work_mode == "hybrid" and j.geo.work_mode_field == "workplaceType"
    assert j.remote is False
    assert s.fetch_complete is True


def test_one_request_lists_a_whole_board(monkeypatch):
    """The REST listing took five a page; this is one POST for the board."""
    asked: list[dict] = []

    def q(page, inp):
        asked.append(inp)
        return _Resp(200, payload=_listing([_job(i) for i in range(1, 41)]))

    monkeypatch.setattr(join_mod.httpx, "Client", _client(_company_page, q))
    jobs = JoinScraper(_SLUG).fetch()
    assert len(jobs) == 40 and len(asked) == 1
    assert asked[0]["companyId"] == 9001
    assert asked[0]["paginationPage"] == {"page": 1, "pageSize": join_mod._PAGE_SIZE}


# ── a throttle is not an empty board ─────────────────────────────────────────

@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_a_transient_status_on_the_jobs_query_raises(patch_client, status):
    """Raising is what routes this into the caller's failure path — backoff and
    a retry — instead of `_mark_polled(ok=True, job_count=0)`."""
    patch_client(lambda page: _Resp(status))
    with pytest.raises(JoinTransientError) as exc:
        JoinScraper(_SLUG).fetch()
    assert str(status) in str(exc.value)


@pytest.mark.parametrize("status", [429, 503])
def test_a_transient_status_on_the_company_page_raises(patch_client, status):
    patch_client(lambda page: pytest.fail("unreachable"),
                 page_handler=lambda url: _Resp(status))
    with pytest.raises(JoinTransientError):
        JoinScraper(_SLUG).fetch()


def test_a_connection_timeout_raises(patch_client):
    def h(url):
        raise httpx.ConnectTimeout("timed out")

    patch_client(lambda page: pytest.fail("unreachable"), page_handler=h)
    with pytest.raises(JoinTransientError):
        JoinScraper(_SLUG).fetch()


def test_a_timeout_on_the_jobs_query_raises(patch_client):
    def q(page):
        raise httpx.ReadTimeout("timed out")

    patch_client(q)
    with pytest.raises(JoinTransientError):
        JoinScraper(_SLUG).fetch()


def test_a_contract_error_raises_with_the_body(patch_client):
    """422 — the shape of the request was wrong. That is a bug on our side or a
    changed API, and either way it is not evidence the company stopped hiring.
    "returned 422" says something broke; the body says what to change."""
    patch_client(lambda page: _Resp(422, "pageSize must be between 1 and 50"))
    with pytest.raises(JoinTransientError) as exc:
        JoinScraper(_SLUG).fetch()
    assert "422" in str(exc.value) and "between 1 and 50" in str(exc.value)


# ── an answer we cannot read is not an empty board ───────────────────────────

def test_the_endpoint_going_away_is_not_an_empty_board(patch_client):
    """The bug this file was rewritten for. The query URL names no company, so
    a 404 on it is the ENDPOINT — the REST listing's removal was read as
    "this board ends here" on every board for as long as nobody counted."""
    patch_client(lambda page: _Resp(404, "<pre>Cannot POST /candidate-api/graphql</pre>"))
    with pytest.raises(BoardUnreadableError):
        JoinScraper(_SLUG).fetch()


def test_unparseable_json_raises(patch_client):
    patch_client(lambda page: _Resp(200, text="<html>maintenance</html>", payload=None))
    with pytest.raises(BoardUnreadableError):
        JoinScraper(_SLUG).fetch()


@pytest.mark.parametrize("payload", [
    {"errors": [{"message": 'Cannot query field "idParam" on type "PublicJob".'}]},
    {"data": {"publicJobs": None}},
    {"data": {"publicJobs": {"items": [], "pageInfo": {}}}},          # no pageCount
    {"data": {"publicJobs": {"items": "nope", "pageInfo": {"pageCount": 1}}}},
    {"data": None},
    {"something": "else"},
])
def test_a_changed_envelope_raises_rather_than_reading_empty(patch_client, payload):
    """GraphQL fails a query WHOLE when one selected field leaves the schema,
    and answers 200 while doing it."""
    patch_client(lambda page: _Resp(200, payload=payload))
    with pytest.raises(BoardUnreadableError):
        JoinScraper(_SLUG).fetch()


def test_a_count_with_no_readable_postings_is_unreadable(patch_client):
    """The envelope says 7 postings exist and every entry is null (join.com's
    search index out of sync): that is not an empty board."""
    patch_client(lambda page: _Resp(200, payload=_listing([None, None], row_count=7)))
    with pytest.raises(BoardUnreadableError):
        JoinScraper(_SLUG).fetch()


@pytest.mark.parametrize("status", [401, 403])
def test_a_refusal_is_blocked_not_dead(patch_client, status):
    """One shared endpoint: a refusal is about our address, never this company."""
    patch_client(lambda page: _Resp(status))
    with pytest.raises(BoardBlockedError):
        JoinScraper(_SLUG).fetch()
    patch_client(lambda page: pytest.fail("unreachable"),
                 page_handler=lambda url: _Resp(status))
    join_mod._reset_company_ids()
    with pytest.raises(BoardBlockedError):
        JoinScraper(_SLUG).fetch()


# ── a failure PART WAY through keeps what we read, and says so ───────────────

def test_a_throttle_on_a_later_page_keeps_the_jobs_and_flags_partial(patch_client):
    """Page 1 succeeded, so we hold real postings for this board. Throwing them
    away would be worse than keeping them — but reporting them as the whole
    board would ghost-close everything on page 2."""
    def q(page):
        if page == 1:
            return _Resp(200, payload=_listing([_job(1), _job(2)], page_count=3, row_count=250))
        return _Resp(429)

    patch_client(q)
    s = JoinScraper(_SLUG)
    jobs = s.fetch()
    assert len(jobs) == 2
    assert s.fetch_complete is False, (
        "a truncated walk reported as complete lets the pipeline ghost-close "
        "every posting we never loaded"
    )


def test_hitting_the_page_cap_flags_partial(patch_client):
    """Not an error — but still a subset, and the difference matters downstream."""
    def q(page):
        return _Resp(200, payload=_listing(
            [_job(page * 1000 + i) for i in range(3)], page_count=99, row_count=9999))

    patch_client(q)
    s = JoinScraper(_SLUG)
    jobs = s.fetch()
    assert len(jobs) == 3 * join_mod._MAX_PAGES
    assert s.fetch_complete is False


def test_the_cap_is_expressed_in_jobs():
    """A page COUNT silently changes meaning when the page size changes."""
    assert join_mod._MAX_PAGES * join_mod._PAGE_SIZE >= join_mod._MAX_JOBS_PER_BOARD
    assert join_mod._MAX_PAGES <= 10, "one board must not become dozens of requests"


def test_an_empty_page_before_the_last_is_partial(patch_client):
    def q(page):
        if page == 1:
            return _Resp(200, payload=_listing([], page_count=2, row_count=0))
        return pytest.fail("the walk must stop at the empty page")

    patch_client(q)
    s = JoinScraper(_SLUG)
    assert s.fetch() == []
    assert s.fetch_complete is False


def test_fewer_postings_than_the_count_is_not_the_whole_board(patch_client):
    """Null index entries: the walk finished, but we hold fewer postings than
    the envelope counted — absence from this list proves nothing."""
    patch_client(lambda page: _Resp(200, payload=_listing([_job(1), None], row_count=2)))
    s = JoinScraper(_SLUG)
    assert [j.external_id for j in s.fetch()] == ["1"]
    assert s.fetch_complete is False


def test_a_full_walk_that_matches_the_count_is_complete(patch_client):
    def q(page):
        ids = (1, 2) if page == 1 else (3,)
        return _Resp(200, payload=_listing([_job(i) for i in ids], page_count=2, row_count=3))

    patch_client(q)
    s = JoinScraper(_SLUG)
    assert [j.external_id for j in s.fetch()] == ["1", "2", "3"]
    assert s.fetch_complete is True


# ── deduplication ────────────────────────────────────────────────────────────

def test_a_posting_repeated_across_pages_is_ingested_once(patch_client):
    """A board that changes mid-walk shifts postings between pages, so the same
    id arrives twice — and every poll then re-upserts the same row."""
    def q(page):
        items = [_job(1), _job(2)] if page == 1 else [_job(2), _job(3)]
        return _Resp(200, payload=_listing(items, page_count=2, row_count=3))

    patch_client(q)
    jobs = JoinScraper(_SLUG).fetch()
    assert [j.external_id for j in jobs] == ["1", "2", "3"]


def test_signature_entries_track_the_deduped_postings(patch_client):
    """The pulse lane hashes these to decide whether a board changed; a
    duplicate would make an unchanged board look different every poll."""
    patch_client(lambda page: _Resp(200, payload=_listing(
        [_job(1), _job(1), _job(2)], row_count=2)))
    s = JoinScraper(_SLUG)
    s.fetch()
    assert s.signature_entries == [("1", "Engineer 1"), ("2", "Engineer 2")]


# ── the company id ───────────────────────────────────────────────────────────

def test_the_placeholder_company_is_rejected_with_no_fallback(patch_client):
    """join.com serves company 233 for empty tenants. The domain check existed
    to catch that — and the line after it returned the first id on the page when
    no domain matched, which is precisely the placeholder. Every empty tenant
    scraped the placeholder's jobs under the wrong company name."""
    patch_client(lambda page: pytest.fail(
        "resolved a company id that did not match the slug and went on to "
        "scrape someone else's jobs"),
        page_handler=lambda url: _Resp(200, _PLACEHOLDER_PAGE))
    assert JoinScraper("newco").fetch() == []


def test_a_matching_domain_still_resolves(patch_client):
    """The guard must not be so tight that real boards stop resolving."""
    patch_client(lambda page: _Resp(200, payload=_listing([_job(7)])),
                 page_handler=lambda url: _Resp(200, _COMPANY_PAGE))
    jobs = JoinScraper(_SLUG).fetch()
    assert [j.external_id for j in jobs] == ["7"]
    assert jobs[0].company == "Acme"


def test_the_company_page_is_fetched_once_per_process(monkeypatch):
    """The id never changes and the page is ~115 KB. Resolving it on every poll
    doubled the requests sent to a site that rate-limits by address."""
    pages: list[str] = []

    def page(url):
        pages.append(url)
        return _company_page(url)

    monkeypatch.setattr(join_mod.httpx, "Client", _client(
        page, lambda p, inp: _Resp(200, payload=_listing([_job(1)]))))
    for _ in range(3):
        assert len(JoinScraper(_SLUG).fetch()) == 1
    assert len(pages) == 1, f"company page fetched {len(pages)} times for one board"
    JoinScraper("other").fetch()
    assert len(pages) == 2, "a different board still resolves its own id"


def test_an_unreadable_answer_forgets_the_remembered_id(monkeypatch):
    """The answer may be about the ID (a company removed or re-keyed). With the
    id remembered, the company page — whose 404 is the conclusive "no such
    company" — would never be asked again."""
    pages: list[str] = []
    answers = [_Resp(200, payload=_listing([_job(1)])),
               _Resp(200, payload={"errors": [{"message": "Company not found"}]})]

    def page(url):
        pages.append(url)
        return _company_page(url) if len(pages) == 1 else _Resp(404, "")

    monkeypatch.setattr(join_mod.httpx, "Client", _client(
        page, lambda p, inp: answers.pop(0)))
    assert len(JoinScraper(_SLUG).fetch()) == 1
    with pytest.raises(BoardUnreadableError):
        JoinScraper(_SLUG).fetch()
    assert len(pages) == 1, "the id was still remembered for the failing poll"
    assert JoinScraper(_SLUG).fetch() == [], "the company page now says: no such company"
    assert len(pages) == 2


def test_a_missing_company_is_asked_again(monkeypatch):
    """Only POSITIVE answers are remembered: a company can appear."""
    pages: list[str] = []

    def page(url):
        pages.append(url)
        return _Resp(404, "")

    monkeypatch.setattr(join_mod.httpx, "Client", _client(
        page, lambda p, inp: pytest.fail("no company")))
    JoinScraper(_SLUG).fetch()
    JoinScraper(_SLUG).fetch()
    assert len(pages) == 2


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
# counter for the first time — a regression the first production deploy of that
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


def test_every_error_this_adapter_raises_for_the_site_is_spared(patch_client):
    """The strings that actually cross the thread pool, not hand-written ones."""
    from app.discovery.pipeline import _is_throttled
    cases = {
        "throttle": lambda page: _Resp(429),
        "refusal": lambda page: _Resp(403),
        "endpoint gone": lambda page: _Resp(404),
        "schema changed": lambda page: _Resp(200, payload={"errors": [{"message": "x"}]}),
        "not json": lambda page: _Resp(200, text="<html>", payload=None),
        "contract": lambda page: _Resp(422, "bad input"),
    }
    for name, handler in cases.items():
        patch_client(handler)
        with pytest.raises(Exception) as exc:
            JoinScraper(_SLUG).fetch()
        assert _is_throttled(str(exc.value)) is True, (
            f"{name}: {exc.value!r} would count toward retiring the board")


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
