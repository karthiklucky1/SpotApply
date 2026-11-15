"""A site that refuses us, or changes its answer, is not thousands of dead employers.

Audit 2026-11-14 (production, read-only). 44,996 boards were "active"; 29,790
showed no jobs — and 86% of those sat on three job systems whose adapters had
stopped reading the site, each in a different way and none of them logged as a
problem:

  BambooHR  5,557 boards, 0 with jobs, no error recorded, last new job
            2026-10-07. The widget URL had started answering JSON; the HTML
            parser found no <li> in it and reported an empty board,
            successfully, every poll. The detail record had moved under
            ``result.jobOpening`` too, so no row ever had a description.

  join.com  19,766 boards, 4 with jobs. The REST listing was removed; its
            ``404`` was read as "nothing more to read" (an empty board), and
            the rest were in a 429 backoff from a five-a-page walk.

  Rippling  the API began answering 403 to production's address while the same
            request succeeded from another network. 403 was on the adapter's
            "permanent" list, every refusal was a strike, and 960 boards holding
            15,293 open jobs were retired as "unreachable x5" in one day.

Three rules follow, and this file pins each from both sides:

  1. an answer the adapter cannot READ raises — it is never "0 jobs";
  2. a REFUSAL is backed off — it is never a strike against the board;
  3. when nearly every board of one system is failing, the failure is the
     system's (``site_health``) — while one dead board among healthy ones, and
     boards that answer 404, still retire exactly as before.

Every response shape below is the one the live site sent on 2026-11-14.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import CompanyRegistry, JobSource
from app.discovery import bamboohr, rippling, site_health
from app.discovery.base import (
    BLOCKED_MARKER, UNREADABLE_MARKER, BoardBlockedError, BoardUnreadableError,
    fetch_error,
)
from app.discovery.pipeline import (
    BOARD_DEACTIVATE_AFTER_FAILURES, _is_throttled, record_board_failure,
    record_board_failures_bulk,
)
from app.strategy import pulse_lane
from app.strategy.hot_lane import _mark_polled

_PREFIX = "outage-probe-"
_REQ = httpx.Request("GET", "https://example.test/")


def _http(status=200, payload=None, text=None):
    if payload is not None:
        return httpx.Response(status, json=payload, request=_REQ)
    return httpx.Response(status, text=text or "", request=_REQ)


# ── Rippling ─────────────────────────────────────────────────────────────────

_RIPPLING_LISTING = [
    {"uuid": "ded5bd89-cc47-43fd-b5e4-b0ac79bf4a9b", "name": " Associate Veterinarian",
     "department": {"id": "Medical", "label": "Medical"},
     "url": "https://ats.rippling.com/petfolk/jobs/ded5bd89-cc47-43fd-b5e4-b0ac79bf4a9b",
     "workLocation": {"label": "Raleigh, NC", "id": "Raleigh, NC"}},
]


def _rippling(monkeypatch, response=None, exc=None):
    def get(*a, **k):
        if exc is not None:
            raise exc
        return response
    monkeypatch.setattr(rippling.httpx, "get", get)
    return rippling.RipplingScraper("petfolk")


def test_rippling_reads_the_live_listing(monkeypatch):
    s = _rippling(monkeypatch, _http(payload=_RIPPLING_LISTING))
    (j,) = s.fetch()
    assert j.external_id == "ded5bd89-cc47-43fd-b5e4-b0ac79bf4a9b"
    assert j.title == "Associate Veterinarian" and j.location == "Raleigh, NC"
    assert j.url.startswith("https://ats.rippling.com/petfolk/jobs/")
    assert j.context["department"].value == "Medical"
    assert s.fetch_complete is True and fetch_error(s) == ""


@pytest.mark.parametrize("status", [403, 429])
def test_a_rippling_refusal_is_blocked_not_dead(monkeypatch, status):
    """Every board is served by one host: a refusal is about our address."""
    s = _rippling(monkeypatch, _http(status, text="<html>blocked</html>"))
    with pytest.raises(BoardBlockedError) as exc:
        s.fetch()
    assert _is_throttled(str(exc.value)) is True, (
        "a refusal counted as a strike is how 960 boards were retired in a day")
    assert site_health.answered(str(exc.value)) is False


@pytest.mark.parametrize("status", [401, 404, 410])
def test_a_missing_rippling_board_is_still_a_fact_about_the_board(monkeypatch, status):
    s = _rippling(monkeypatch, _http(status))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        s.fetch()
    assert _is_throttled(str(exc.value)) is False
    assert str(status) in str(exc.value)


@pytest.mark.parametrize("response,exc", [
    (_http(503), None),
    (_http(500), None),
    (None, httpx.ConnectTimeout("timed out")),
])
def test_a_rippling_fault_is_not_an_empty_board(monkeypatch, response, exc):
    """`return []` with nothing said was a successful poll of a board with no
    jobs: job_count=0, and the board demoted to the 72-hour cadence."""
    s = _rippling(monkeypatch, response, exc)
    assert s.fetch() == []
    assert fetch_error(s), "an empty list from a failed fetch must say why"
    assert s.fetch_complete is False


@pytest.mark.parametrize("response", [
    _http(200, text="<html>Just a moment...</html>"),
    _http(200, payload={"detail": "something else"}),
    _http(200, payload={"items": "nope"}),
])
def test_a_rippling_answer_we_cannot_read_is_not_an_empty_board(monkeypatch, response):
    s = _rippling(monkeypatch, response)
    with pytest.raises(BoardUnreadableError) as exc:
        s.fetch()
    assert _is_throttled(str(exc.value)) is True


def test_an_empty_rippling_board_is_still_empty(monkeypatch):
    s = _rippling(monkeypatch, _http(payload=[]))
    assert s.fetch() == []
    assert fetch_error(s) == "" and s.fetch_complete is True


# ── BambooHR ─────────────────────────────────────────────────────────────────

_BAMBOO_JSON = {
    "version": None,
    "labels": {"heading": "Open Positions", "blankStateTitle": "No open positions"},
    "departments": [
        {"id": 18609, "label": "Customer Service", "positions": [
            {"id": 259, "name": "Bilingual Customer Service Reps- Belize ",
             "url": "https://itel.bamboohr.com/careers/259", "location": "Belize City"},
            {"id": 260, "name": "Team Lead", "url": "https://itel.bamboohr.com/careers/260",
             "location": "Kingston"}]},
        {"id": 2, "label": "Finance", "positions": [
            {"id": 33, "name": "Accountant", "url": "//itel.bamboohr.com/careers/33",
             "location": ""}]},
    ],
    "success": True,
}


def test_the_json_widget_is_read():
    """The bug: this body has no <li>, so the HTML parser read 0 jobs."""
    rows = bamboohr.parse_listing(json.dumps(_BAMBOO_JSON), "itel")
    assert [(r["id"], r["title"], r["department"]) for r in rows] == [
        ("259", "Bilingual Customer Service Reps- Belize", "Customer Service"),
        ("260", "Team Lead", "Customer Service"),
        ("33", "Accountant", "Finance"),
    ]
    assert rows[0]["url"] == "https://itel.bamboohr.com/careers/259"
    assert rows[2]["url"] == "https://itel.bamboohr.com/careers/33"
    assert rows[0]["location"] == "Belize City"


def test_the_html_widget_is_still_read():
    html = ('<ul><li id="bhrPositionID_101" class="BambooHR-ATS-Jobs-Item">'
            '<a href="//acme.bamboohr.com/careers/101">Backend Engineer</a>'
            '<span class="BambooHR-ATS-Location">Austin, TX</span></li></ul>')
    (row,) = bamboohr.parse_listing(html, "acme")
    assert row == {"id": "101", "title": "Backend Engineer",
                   "url": "https://acme.bamboohr.com/careers/101",
                   "location": "Austin, TX", "department": ""}


def test_an_empty_json_widget_is_a_real_empty_board():
    """`labels.blankStateTitle` is template text; an empty list is the answer."""
    body = json.dumps({"departments": [], "labels": {"blankStateTitle": "None"}, "success": True})
    assert bamboohr.parse_listing(body, "acme") == []


def test_an_empty_html_widget_is_a_real_empty_board():
    assert bamboohr.parse_listing('<div id="BambooHR-ATS" class="BambooHR-ATS-board"></div>', "acme") == []
    assert bamboohr.parse_listing("", "acme") == []


@pytest.mark.parametrize("body", [
    json.dumps({"success": False, "error": "something"}),            # JSON, no departments
    json.dumps({"departments": {"not": "a list"}}),
    "<html><head><title>Sign in</title></head><body>" + "x" * 800 + "</body></html>",
])
def test_a_widget_we_cannot_read_is_not_an_empty_board(body):
    """The next format change must fail loudly on the first poll, not read as
    "no openings" on every board for five weeks."""
    with pytest.raises(BoardUnreadableError) as exc:
        bamboohr.parse_listing(body, "acme")
    assert _is_throttled(str(exc.value)) is True


def _bamboo_scraper(monkeypatch, widget, detail):
    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw): return detail

    monkeypatch.setattr(bamboohr.httpx, "get", lambda *a, **k: widget)
    monkeypatch.setattr(bamboohr.httpx, "Client", _Client)
    return bamboohr.BambooHRScraper("itel")


def test_the_detail_record_is_read_from_its_new_envelope(monkeypatch):
    """`result.jobOpening` — reading the old top-level keys off it is why every
    BambooHR row was stored with no description."""
    detail = _http(payload={"meta": {}, "result": {"jobOpening": {
        "jobOpeningName": "Bilingual Customer Service Reps- Belize ",
        "departmentLabel": "Customer Service", "employmentStatusLabel": "Full-Time",
        "description": "<p><span>Is responsible for providing professional service.</span></p>",
        "compensation": "$9", "datePosted": "2026-10-23", "locationType": "0",
        "location": {"city": "Belize City", "state": None, "postalCode": "00000",
                     "addressCountry": "Belize"},
        "atsLocation": {"country": None, "countryId": None, "state": None, "city": None}}}})
    s = _bamboo_scraper(monkeypatch, _http(text=json.dumps(_BAMBOO_JSON)), detail)
    jobs = s.fetch()
    assert len(jobs) == 3
    j = jobs[0]
    assert j.external_id == "itel:259"
    assert "professional service" in j.description
    assert j.posted_at == datetime(2026, 10, 23)
    assert j.location == "Belize City, Belize"
    assert j.geo.country == "Belize" and j.geo.country_field == "location.addressCountry"
    assert j.geo.work_mode == "onsite" and j.geo.work_mode_field == "locationType"
    assert j.remote is False and j.salary_text == "$9"
    assert j.context["department"].value == "Customer Service"
    assert s.fetch_complete is True


def test_the_old_detail_shape_still_reads(monkeypatch):
    detail = _http(payload={"description": "<p>Python</p>", "datePosted": "2026-07-10",
                            "location": {"city": "Austin", "state": "TX", "country": "USA"},
                            "isRemote": True})
    s = _bamboo_scraper(monkeypatch, _http(text=json.dumps(_BAMBOO_JSON)), detail)
    j = s.fetch()[0]
    assert j.description == "Python" and j.location == "Austin, TX, USA"
    assert j.geo.work_mode == "remote" and j.remote is True


def test_a_failed_detail_keeps_the_listing_row(monkeypatch):
    s = _bamboo_scraper(monkeypatch, _http(text=json.dumps(_BAMBOO_JSON)), _http(500))
    jobs = s.fetch()
    assert [j.title for j in jobs][:2] == ["Bilingual Customer Service Reps- Belize", "Team Lead"]
    assert jobs[0].description == "" and jobs[0].location == "Belize City"


def _bamboo_counting(monkeypatch, statuses):
    """A 30-posting board whose detail endpoint answers `statuses` in turn (the
    last one repeats). Returns (scraper, list of detail calls)."""
    board = {"departments": [{"id": 1, "label": "Ops", "positions": [
        {"id": i, "name": f"Role {i}", "url": f"https://itel.bamboohr.com/careers/{i}",
         "location": "Kingston"} for i in range(30)]}], "success": True}
    calls: list[str] = []

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, **kw):
            calls.append(url)
            status = statuses[min(len(calls) - 1, len(statuses) - 1)]
            if status == 200:
                return _http(payload={"result": {"jobOpening": {"description": "<p>text</p>"}}})
            return _http(status)

    monkeypatch.setattr(bamboohr.httpx, "get", lambda *a, **k: _http(text=json.dumps(board)))
    monkeypatch.setattr(bamboohr.httpx, "Client", _Client)
    return bamboohr.BambooHRScraper("itel"), calls


def test_a_throttled_detail_stops_the_poll_at_once(monkeypatch):
    """The loop used to skip a non-200 and send the other fifty-nine requests
    anyway, record a clean poll, and do it again next time. Asking harder is
    how a rate limit becomes a block."""
    s, calls = _bamboo_counting(monkeypatch, [200, 200, 429])
    with pytest.raises(BoardBlockedError) as exc:
        s.fetch()
    assert len(calls) == 3, f"{len(calls)} detail requests sent to a site that said stop"
    assert _is_throttled(str(exc.value)) is True


def test_repeated_refusals_stop_the_poll_but_one_private_posting_does_not(monkeypatch):
    s, calls = _bamboo_counting(monkeypatch, [403, 200])
    assert len(s.fetch()) == 30 and len(calls) == 30, "one 403 is one posting"
    s, calls = _bamboo_counting(monkeypatch, [200, 403])
    with pytest.raises(BoardBlockedError):
        s.fetch()
    assert len(calls) == 1 + bamboohr._DETAIL_REFUSALS_BEFORE_STOP


def test_an_unanswering_detail_endpoint_is_not_asked_thirty_times(monkeypatch):
    """5xx is not a refusal, so the board is not failed — but the rows listed
    are kept and the remaining detail requests are not sent."""
    s, calls = _bamboo_counting(monkeypatch, [503])
    jobs = s.fetch()
    assert len(jobs) == 30 and all(j.description == "" for j in jobs)
    assert len(calls) == bamboohr._DETAIL_FAILURES_BEFORE_STOP


@pytest.mark.parametrize("status", [500, 503])
def test_a_bamboohr_fault_is_not_an_empty_board(monkeypatch, status):
    s = _bamboo_scraper(monkeypatch, _http(status), _http(200, payload={}))
    assert s.fetch() == []
    assert fetch_error(s), "an empty list from a failed fetch must say why"


# ── the markers ──────────────────────────────────────────────────────────────

def test_the_two_errors_carry_the_marker_the_recorders_read():
    blocked = str(BoardBlockedError("rippling", 403))
    unreadable = str(BoardUnreadableError("bamboohr", "widget is JSON"))
    assert BLOCKED_MARKER in blocked and UNREADABLE_MARKER in unreadable
    for msg in (blocked, unreadable):
        assert _is_throttled(msg) is True
        assert site_health.answered(msg) is False, "the site did not answer the question"


def test_an_unreadable_error_that_mentions_a_status_is_still_unreadable():
    """`_mark_polled` retires on any error containing "404"; the marker is
    checked first, so a message quoting a status cannot retire the board."""
    msg = str(BoardUnreadableError("join", "endpoint answered 404"))
    assert _is_throttled(msg) is True and site_health.answered(msg) is False


# ── the registry: what a failed poll writes ──────────────────────────────────

def _seed(n=1, ats=JobSource.RIPPLING, **over):
    slugs = [f"{_PREFIX}{ats.value}-{i}" for i in range(n)]
    with get_session() as s:
        for r in s.exec(select(CompanyRegistry).where(
                CompanyRegistry.slug.like(f"{_PREFIX}%"))).all():
            s.delete(r)
        for slug in slugs:
            s.add(CompanyRegistry(slug=slug, ats=ats, company_name=slug, **over))
        s.commit()
    return slugs


def _rows():
    with get_session() as s:
        return {r.slug: r for r in s.exec(select(CompanyRegistry).where(
            CompanyRegistry.slug.like(f"{_PREFIX}%"))).all()}


@pytest.fixture(autouse=True)
def _clean():
    yield
    with get_session() as s:
        for r in s.exec(select(CompanyRegistry).where(
                CompanyRegistry.slug.like(f"{_PREFIX}%"))).all():
            s.delete(r)
        s.commit()


def _fail(slug, ats, error):
    _mark_polled(slug, ats, job_count=None, ok=False, error=error,
                 failure_backoff_minutes=15, failure_backoff_cap_hours=24)


@pytest.mark.parametrize("error", [
    str(BoardBlockedError("rippling", 403)),
    str(BoardBlockedError("rippling", 429)),
    str(BoardUnreadableError("rippling", "board API did not send JSON")),
])
def test_a_refused_or_unreadable_board_is_never_retired(error):
    (slug,) = _seed(job_count=12)
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES + 3):
        _fail(slug, JobSource.RIPPLING, error)
    row = _rows()[slug]
    assert row.is_active is True, "a board the site refused to answer for was retired"
    assert (row.failure_count or 0) == 0
    assert row.next_poll_at and row.next_poll_at > datetime.utcnow(), "it must be backed off"
    assert row.job_count == 12, "a refusal must not write 'no jobs' over the board"
    assert row.last_error and ("refused" in row.last_error or "unreadable" in row.last_error)


@pytest.mark.parametrize("error", [
    str(BoardBlockedError("rippling", 403)),
    str(BoardUnreadableError("rippling", "board API did not send JSON")),
])
def test_a_board_refused_alone_while_the_site_answers_still_retires(error):
    """The adapter's "this is the site" is believed — until the same system is
    answering normally for everyone else. Then this board alone is refused or
    unreadable (a private board, a custom page, a stale id), and sparing it
    forever would keep a dead slug in rotation."""
    (slug,) = _seed()
    for _ in range(80):
        site_health.record(JobSource.RIPPLING, True)
    assert site_health.answering(JobSource.RIPPLING) is True
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES):
        _fail(slug, JobSource.RIPPLING, error)
    row = _rows()[slug]
    assert row.is_active is False and row.inactive_reason.startswith("unreachable")


def test_a_rate_limit_is_never_a_strike_whatever_the_neighbours_do():
    """One board can be throttled while the rest of the site answers."""
    (slug,) = _seed()
    for _ in range(80):
        site_health.record(JobSource.RIPPLING, True)
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES + 3):
        _fail(slug, JobSource.RIPPLING, str(BoardBlockedError("rippling", 429)))
    row = _rows()[slug]
    assert row.is_active is True and (row.failure_count or 0) == 0


def test_too_small_a_sample_favours_the_board():
    """A handful of answers is not proof the site is fine."""
    (slug,) = _seed()
    for _ in range(5):
        site_health.record(JobSource.RIPPLING, True)
    assert site_health.answering(JobSource.RIPPLING) is False
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES):
        _fail(slug, JobSource.RIPPLING, str(BoardBlockedError("rippling", 403)))
    assert _rows()[slug].is_active is True


def test_the_full_lane_recorders_spare_them_too():
    (slug,) = _seed()
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES + 1):
        record_board_failure("rippling", slug, str(BoardBlockedError("rippling", 403)))
        record_board_failures_bulk([(slug, "rippling",
                                     str(BoardUnreadableError("rippling", "x")))])
    row = _rows()[slug]
    assert row.is_active is True and (row.failure_count or 0) == 0


# ── site health: the system, not the board ───────────────────────────────────

def test_a_site_wide_failure_is_not_counted_against_any_board():
    """The Rippling day, with an error the text match does NOT spare: every
    board fails, repeatedly. Nobody is retired, and no board gets past the
    strike it took before the sample was large enough to call."""
    slugs = _seed(30)
    for _round in range(BOARD_DEACTIVATE_AFTER_FAILURES + 2):
        for slug in slugs:
            _fail(slug, JobSource.RIPPLING, "HTTP 503")
    rows = _rows()
    assert all(r.is_active for r in rows.values()), (
        f"{sum(not r.is_active for r in rows.values())} boards retired by a site-wide outage")
    assert max((r.failure_count or 0) for r in rows.values()) <= 1
    assert site_health.mass_failing(JobSource.RIPPLING) is True


def test_one_dead_board_among_healthy_ones_still_retires():
    """The guard can only spare. A board that fails while its neighbours answer
    is the board's problem."""
    (slug,) = _seed()
    for _ in range(80):
        site_health.record(JobSource.RIPPLING, True)
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES):
        _fail(slug, JobSource.RIPPLING, "HTTP 503")
    row = _rows()[slug]
    assert row.is_active is False and row.inactive_reason.startswith("unreachable")


def test_missing_boards_do_not_shield_each_other():
    """A 404 is the site answering. A batch of junk slugs must each retire —
    thirty of them failing together is not an outage."""
    slugs = _seed(30)
    for slug in slugs:
        _fail(slug, JobSource.RIPPLING, "Client error '404 Not Found' for url 'https://x'")
    rows = _rows()
    assert not any(r.is_active for r in rows.values())
    assert site_health.mass_failing(JobSource.RIPPLING) is False


def test_another_systems_outage_spares_nothing_here():
    (slug,) = _seed(ats=JobSource.BAMBOOHR)
    for _ in range(40):
        site_health.record(JobSource.RIPPLING, False)
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES):
        _fail(slug, JobSource.BAMBOOHR, "HTTP 503")
    assert _rows()[slug].is_active is False


def test_the_full_lane_asks_the_same_question():
    (slug,) = _seed(ats=JobSource.WORKDAY)
    for _ in range(40):
        site_health.record("workday", False)
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES + 1):
        record_board_failure("workday", slug, "HTTP 503")
        record_board_failures_bulk([(slug, "workday", "connection timeout")])
    row = _rows()[slug]
    assert row.is_active is True and (row.failure_count or 0) == 0
    assert row.next_poll_at is not None


def test_the_switch_restores_per_board_counting(monkeypatch):
    monkeypatch.setattr(settings, "site_health_enabled", False, raising=False)
    slugs = _seed(30)
    for _round in range(BOARD_DEACTIVATE_AFTER_FAILURES):
        for slug in slugs:
            _fail(slug, JobSource.RIPPLING, "HTTP 503")
    assert not any(r.is_active for r in _rows().values())


def test_mass_failing_needs_a_sample_and_a_share():
    for _ in range(19):
        site_health.record("x", False, now=1000.0)
    assert site_health.mass_failing("x", now=1000.0) is False, "19 failures is not a sample"
    site_health.record("x", False, now=1000.0)
    assert site_health.mass_failing("x", now=1000.0) is True
    for _ in range(10):                       # 20 failed of 30 = 67%
        site_health.record("x", True, now=1000.0)
    assert site_health.mass_failing("x", now=1000.0) is False, "a third of boards answered"


def test_the_window_forgets():
    for _ in range(40):
        site_health.record("y", False, now=1000.0)
    assert site_health.mass_failing("y", now=1000.0) is True
    later = 1000.0 + settings.site_health_window_minutes * 60 + 1
    assert site_health.mass_failing("y", now=later) is False, (
        "an outage that ended must stop sparing boards")


def test_the_snapshot_names_systems_only():
    for _ in range(25):
        site_health.record(JobSource.RIPPLING, False)
    snap = site_health.snapshot()
    assert snap == {"rippling": {"fetches": 25, "failed": 25, "mass_failing": True}}


# ── the per-site floor ───────────────────────────────────────────────────────

def _board(ats, **over):
    return CompanyRegistry(slug=f"{_PREFIX}cadence", ats=ats, **over)


def test_a_site_is_never_asked_faster_than_its_floor():
    now = datetime.utcnow()
    fast = timedelta(minutes=settings.pulse_fast_interval_minutes)
    posting = dict(last_new_job_at=now - timedelta(hours=1), job_count=9)
    assert pulse_lane._cadence(_board(JobSource.GREENHOUSE, **posting), set(), now) == fast
    assert pulse_lane._cadence(_board(JobSource.RIPPLING, **posting), set(), now) == \
        timedelta(minutes=30)
    assert pulse_lane._cadence(_board(JobSource.JOIN, **posting), set(), now) == \
        timedelta(minutes=120)
    assert pulse_lane._cadence(_board(JobSource.BAMBOOHR, **posting), set(), now) == \
        timedelta(minutes=60)


def test_a_followed_board_obeys_the_floor_too():
    """The site does not know why we are asking."""
    now = datetime.utcnow()
    b = _board(JobSource.RIPPLING, company_name="Petfolk", job_count=3)
    b.slug = "petfolk"
    assert pulse_lane._cadence(b, {"petfolk"}, now) == timedelta(minutes=30)


def test_the_floor_never_shortens_a_cadence():
    now = datetime.utcnow()
    quiet = pulse_lane._cadence(_board(JobSource.RIPPLING, job_count=0), set(), now)
    assert quiet == timedelta(hours=settings.pulse_dead_interval_hours)
    live = pulse_lane._cadence(_board(JobSource.RIPPLING, job_count=4), set(), now)
    assert live == timedelta(minutes=max(30, settings.pulse_floor_interval_minutes))


@pytest.mark.parametrize("raw", ["", "rippling=abc,join", "=5,,rippling=0", "greenhouse=45"])
def test_a_missing_or_malformed_floor_is_no_floor(monkeypatch, raw):
    monkeypatch.setattr(settings, "pulse_ats_min_interval_minutes", raw, raising=False)
    now = datetime.utcnow()
    b = _board(JobSource.RIPPLING, last_new_job_at=now, job_count=1)
    assert pulse_lane._cadence(b, set(), now) == \
        timedelta(minutes=settings.pulse_fast_interval_minutes)


def test_a_failed_board_is_not_retried_sooner_than_the_floor():
    """The failure backoff starts at 15 minutes; a site with a two-hour floor
    was being retried in 15, 30 and 60."""
    assert pulse_lane._failure_backoff_minutes(JobSource.JOIN, 15) == 120
    assert pulse_lane._failure_backoff_minutes(JobSource.RIPPLING, 15) == 30
    assert pulse_lane._failure_backoff_minutes(JobSource.GREENHOUSE, 15) == 15
    assert pulse_lane._failure_backoff_minutes(JobSource.JOIN, 240) == 240
    assert pulse_lane._failure_backoff_minutes(JobSource.JOIN, 0) == 0, "0 = backoff off"


def test_the_floors_cover_the_three_sites_that_refused_us():
    """The defaults are the lesson; an empty default would un-learn it."""
    for ats in ("rippling", "join", "bamboohr"):
        assert pulse_lane._ats_min_interval(ats) is not None, ats
    assert pulse_lane._ats_min_interval("greenhouse") is None


# ── 2026-11-15: the other sources ────────────────────────────────────────────
# Checking the remaining adapters found the same two mistakes still live:
#
#   * Workday and SmartRecruiters answered `None` for ANY non-200 and said
#     nothing else. The pulse lane recorded that as a failure with an EMPTY
#     error, which no rule could recognise as a throttle, a refusal or an
#     outage: every one was a strike. On 2026-11-14, 05:18–15:29 UTC, that
#     retired 1,329 Workday boards holding 30,812 open jobs as "unreachable x5"
#     with nothing recorded about why.
#   * Six adapters answered `[]` for a timeout, a 429 or a 5xx — a successful
#     poll of an employer with no openings (`job_count = 0`, 72-hour cadence) —
#     and four of them did the same for a body they could not parse.

from app.discovery import (  # noqa: E402
    breezy, personio, pinpoint, recruitee, smartrecruiters, teamtailor, workable, workday,
)
from app.discovery.pipeline import (  # noqa: E402
    REFUSED_BOARD_BACKOFF_HOURS, THROTTLED_BOARD_BACKOFF_HOURS, _not_about_the_board,
    spared_backoff_hours,
)


def _workday(monkeypatch, status=200, payload=None, exc=None):
    def post(url, **kw):
        if exc is not None:
            raise exc
        return _http(status, payload=payload if payload is not None else {})
    monkeypatch.setattr(workday.httpx, "post", post)
    return workday.WorkdayScraper("acme", "https://acme.wd1.myworkdayjobs.com/External")


@pytest.mark.parametrize("status", [403, 429])
def test_a_workday_refusal_is_blocked_not_a_blank_failure(monkeypatch, status):
    s = _workday(monkeypatch, status)
    with pytest.raises(BoardBlockedError) as exc:
        s.fetch()
    assert str(status) in str(exc.value)


@pytest.mark.parametrize("status,needle", [(500, "500"), (503, "503"), (404, "not found")])
def test_a_workday_failure_says_why(monkeypatch, status, needle):
    """`None` with no reason was a strike nobody could classify."""
    s = _workday(monkeypatch, status)
    assert s.fetch() is None
    assert needle in fetch_error(s), fetch_error(s)


def test_a_missing_workday_site_is_not_retired_on_the_spot(monkeypatch):
    """The recorders retire a board immediately on an error naming 404. A
    tenant whose site path moved keeps the five tries it always had — and if
    every tenant says it at once, that is Workday, not the tenants."""
    s = _workday(monkeypatch, 404)
    s.fetch()
    assert "404" not in fetch_error(s)
    assert site_health.answered(fetch_error(s)) is False


def test_a_workday_timeout_says_why(monkeypatch):
    s = _workday(monkeypatch, exc=httpx.ReadTimeout("timed out"))
    assert s.fetch() is None
    assert "unreachable" in fetch_error(s) and "ReadTimeout" in fetch_error(s)


def test_a_working_workday_fetch_has_no_error(monkeypatch):
    s = _workday(monkeypatch, 200, {"jobPostings": [], "total": 0})
    assert s.fetch() == []
    assert fetch_error(s) == ""


def _smartrecruiters(monkeypatch, status=200, exc=None):
    def get(url, **kw):
        if exc is not None:
            raise exc
        return _http(status, payload={"content": [], "totalFound": 0})
    monkeypatch.setattr(smartrecruiters.httpx, "get", get)
    return smartrecruiters.SmartRecruitersScraper("acme")


@pytest.mark.parametrize("status", [403, 429])
def test_a_smartrecruiters_refusal_is_blocked(monkeypatch, status):
    with pytest.raises(BoardBlockedError):
        _smartrecruiters(monkeypatch, status).fetch()


@pytest.mark.parametrize("status,needle", [(500, "500"), (404, "not found")])
def test_a_smartrecruiters_failure_says_why(monkeypatch, status, needle):
    s = _smartrecruiters(monkeypatch, status)
    assert s.fetch() is None
    assert needle in fetch_error(s)
    s = _smartrecruiters(monkeypatch, exc=httpx.ConnectTimeout("x"))
    assert s.fetch() is None and "unreachable" in fetch_error(s)


_TEMPLATE_ADAPTERS = [
    (workable, "WorkableScraper"), (recruitee, "RecruiteeScraper"),
    (personio, "PersonioScraper"), (pinpoint, "PinpointScraper"),
    (breezy, "BreezyScraper"), (teamtailor, "TeamtailorScraper"),
]


@pytest.mark.parametrize("mod,cls", _TEMPLATE_ADAPTERS, ids=lambda v: getattr(v, "__name__", v))
def test_a_failed_fetch_is_never_an_empty_board(monkeypatch, mod, cls):
    """A timeout, a throttle and a 5xx each used to come back as `[]` with
    nothing said: a clean poll of an employer with no openings."""
    s = getattr(mod, cls)("acme")
    for response, exc, needle in ((_http(503), None, "503"), (_http(429), None, "429"),
                                  (None, httpx.ReadTimeout("timed out"), "unreachable")):
        def get(url, _r=response, _e=exc, **kw):
            if _e is not None:
                raise _e
            return _r
        monkeypatch.setattr(mod.httpx, "get", get)
        assert s.fetch() == []
        assert needle in fetch_error(s), f"{cls}: {fetch_error(s)!r}"
    assert _is_throttled(f"{s.name} returned 429") is True


@pytest.mark.parametrize("mod,cls", _TEMPLATE_ADAPTERS, ids=lambda v: getattr(v, "__name__", v))
def test_an_answer_that_does_not_parse_is_not_an_empty_board(monkeypatch, mod, cls):
    s = getattr(mod, cls)("acme")
    monkeypatch.setattr(mod.httpx, "get",
                        lambda url, **kw: _http(200, text="<html>Just a moment...</html"))
    with pytest.raises(BoardUnreadableError):
        s.fetch()


@pytest.mark.parametrize("mod,cls", _TEMPLATE_ADAPTERS, ids=lambda v: getattr(v, "__name__", v))
def test_a_good_fetch_clears_the_reason(monkeypatch, mod, cls):
    """`last_error` is per fetch: a reason left over from a failed poll would
    turn the next genuinely empty board into a failure."""
    s = getattr(mod, cls)("acme")
    monkeypatch.setattr(mod.httpx, "get", lambda url, **kw: _http(503))
    s.fetch()
    assert fetch_error(s)
    empty = {workable: _http(payload={"name": "Acme", "jobs": []}),
             recruitee: _http(payload={"offers": []}),
             personio: _http(text="<workzag-jobs></workzag-jobs>"),
             pinpoint: _http(payload={"data": []}),
             breezy: _http(payload=[]),
             teamtailor: _http(text="<rss><channel></channel></rss>")}[mod]
    monkeypatch.setattr(mod.httpx, "get", lambda url, **kw: empty)
    assert s.fetch() == []
    assert fetch_error(s) == ""


# ── a failure with no reason, and how long a spared board waits ──────────────

def test_a_blank_failure_is_not_the_site_answering():
    """`answered("")` means "no error". A failed fetch that says nothing was
    being recorded as the site answering, which hid a site-wide failure from
    the one check built to see it — the Workday day exactly."""
    slugs = _seed(30, ats=JobSource.WORKDAY)
    for _round in range(BOARD_DEACTIVATE_AFTER_FAILURES + 2):
        for slug in slugs:
            _fail(slug, JobSource.WORKDAY, "")
    rows = _rows()
    assert all(r.is_active for r in rows.values()), (
        f"{sum(not r.is_active for r in rows.values())} boards retired by blank failures")
    assert site_health.mass_failing(JobSource.WORKDAY) is True


def test_a_blank_failure_on_one_board_still_counts():
    (slug,) = _seed(ats=JobSource.WORKDAY)
    for _ in range(80):
        site_health.record(JobSource.WORKDAY, True)
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES):
        _fail(slug, JobSource.WORKDAY, "")
    assert _rows()[slug].is_active is False
    assert _not_about_the_board(JobSource.WORKDAY, "") is False


def test_a_refused_board_waits_a_day_and_a_throttled_one_six_hours():
    """A site that said no outright is not asked again by 1,650 boards every
    six hours; a throttle is a request to slow down, not to go away."""
    assert REFUSED_BOARD_BACKOFF_HOURS > THROTTLED_BOARD_BACKOFF_HOURS
    assert spared_backoff_hours(str(BoardBlockedError("rippling", 403))) == REFUSED_BOARD_BACKOFF_HOURS
    assert spared_backoff_hours(str(BoardBlockedError("rippling", 429))) == THROTTLED_BOARD_BACKOFF_HOURS
    assert spared_backoff_hours("join jobs API returned 429") == THROTTLED_BOARD_BACKOFF_HOURS
    assert spared_backoff_hours(str(BoardUnreadableError("bamboohr", "x"))) == THROTTLED_BOARD_BACKOFF_HOURS
    (slug,) = _seed()
    _fail(slug, JobSource.RIPPLING, str(BoardBlockedError("rippling", 403)))
    wait = _rows()[slug].next_poll_at - datetime.utcnow()
    assert timedelta(hours=REFUSED_BOARD_BACKOFF_HOURS - 1) < wait <= timedelta(
        hours=REFUSED_BOARD_BACKOFF_HOURS)
