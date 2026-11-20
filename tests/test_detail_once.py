"""A posting's description is fetched ONCE, and a row without one is never written over one with.

Rippling, join.com and Breezy list postings with no description: it is one more
request per posting. Until 2026-11-15 the adapters did not make it, so every row
from them (431 of 431 fresh rows in a day) was stored with an empty description
and scored on its title. Fetching every detail on every poll instead is the load
that gets an address refused — Rippling's own board lists 717 postings.

So (app/discovery/detail_memo.py):

  * a detail is fetched only for a posting the pool does not hold yet, from a
    per-source budget; without budget the posting waits for the next poll and
    NO blind row is written;
  * a posting the pool already holds comes back as a listing-only row: the
    upsert refreshes last_seen and nothing else — an empty description must
    never blank a real one;
  * the memory of what the pool holds is written by the UPSERT, so a result
    the lane dropped is fetched again;
  * a result that carries a new detail is ingested even when the listing
    itself reads unchanged.

The response shapes are the live ones (2026-11-14/15).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, Job, JobSource
from app.discovery import breezy, detail_memo, join as join_mod, rippling
from app.discovery.base import BoardBlockedError, RawJob
from app.discovery.pipeline import SHARED_POOL_USER, _upsert
from app.discovery.title_filter import set_title_demand
from app.strategy import pulse_lane

_REQ = httpx.Request("GET", "https://example.test/")
_P = "detail-once-"          # every row this file writes carries it


def _http(status=200, payload=None, text=None):
    if payload is not None:
        return httpx.Response(status, json=payload, request=_REQ)
    return httpx.Response(status, text=text or "", request=_REQ)


@pytest.fixture
def budget(monkeypatch):
    """Turn the feature on (the suite runs with no budget) with room to spare."""
    def apply(rates="rippling=600,join=600,breezy=600", per_poll=25):
        monkeypatch.setattr(settings, "detail_fetch_per_minute", rates, raising=False)
        monkeypatch.setattr(settings, "detail_max_per_poll", per_poll, raising=False)
        detail_memo.reset()
    apply()
    return apply


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    set_title_demand(["Software Engineer"])
    yield
    with get_session() as s:
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_P}%")))
        s.commit()
    set_title_demand(None)


def _stored(source: JobSource):
    with get_session() as s:
        return {r.external_id: r for r in s.exec(select(Job).where(
            Job.user_id == SHARED_POOL_USER, Job.source == source,
            Job.external_id.like(f"{_P}%"))).all()}


# ── the memory ───────────────────────────────────────────────────────────────

def test_a_posting_needs_its_detail_until_the_pool_holds_it(budget):
    fp = detail_memo.fingerprint("Backend Engineer", "Austin, TX")
    assert detail_memo.needs_detail("rippling", "a1", fp) is True
    detail_memo.remember("rippling", "a1", fp)
    assert detail_memo.needs_detail("rippling", "a1", fp) is False


def test_an_edited_listing_is_a_new_posting(budget):
    """Title or location changed: the detail (and with it the description and
    the location evidence) is read again."""
    fp = detail_memo.fingerprint("Backend Engineer", "Austin, TX")
    detail_memo.remember("rippling", "a1", fp)
    assert detail_memo.needs_detail(
        "rippling", "a1", detail_memo.fingerprint("Backend Engineer", "Remote")) is True
    assert detail_memo.needs_detail(
        "rippling", "a1", detail_memo.fingerprint("Senior Backend Engineer", "Austin, TX")) is True


def test_a_forgotten_posting_is_fetched_again_once_and_only_once(budget):
    """A listing-only row with nothing stored behind it is fetched again. A
    posting the pool then declines a SECOND time is one it does not keep —
    asking on every other poll would spend the budget on rows never written."""
    fp = detail_memo.fingerprint("Engineer", "X")
    detail_memo.remember("join", "j1", fp)
    detail_memo.forget("join", ["j1"])
    assert detail_memo.needs_detail("join", "j1", fp) is True
    detail_memo.remember("join", "j1", fp)
    detail_memo.forget("join", ["j1"])
    assert detail_memo.needs_detail("join", "j1", fp) is False


def test_the_memory_starts_from_what_the_pool_already_holds(budget):
    """After a deploy the process knows nothing; refetching every stored
    posting's detail would be the burst this exists to prevent."""
    with get_session() as s:
        s.add(Job(source=JobSource.RIPPLING, external_id=f"{_P}has-text", company="C",
                  title="Engineer", location="", remote=False, url="", description="real text",
                  content_hash="abc123", user_id=SHARED_POOL_USER))
        s.add(Job(source=JobSource.RIPPLING, external_id=f"{_P}blind", company="C",
                  title="Engineer", location="", remote=False, url="", description="",
                  content_hash=detail_memo.EMPTY_CONTENT_HASH, user_id=SHARED_POOL_USER))
        s.commit()
    detail_memo.reset()
    fp = detail_memo.fingerprint("Engineer", "")
    assert detail_memo.needs_detail("rippling", f"{_P}has-text", fp) is False
    assert detail_memo.needs_detail("rippling", f"{_P}blind", fp) is True, (
        "a row stored WITHOUT a description is exactly the one to fetch")


def test_the_budget_is_a_rate_not_a_burst(monkeypatch, budget):
    budget("rippling=30")
    clock = {"t": 1000.0}
    monkeypatch.setattr(detail_memo.time, "monotonic", lambda: clock["t"])
    assert sum(detail_memo.take("rippling") for _ in range(100)) == 30, "one minute's worth, no more"
    clock["t"] += 10                                     # ten seconds → five more
    assert sum(detail_memo.take("rippling") for _ in range(100)) == 5
    assert detail_memo.take("greenhouse") is False, "a source with no budget never fetches"


def test_no_budget_means_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(settings, "detail_fetch_per_minute", "", raising=False)
    poll = detail_memo.Poll("rippling")
    assert poll.on is False
    assert poll.decide("x", "Engineer", "NYC") == ("plain", None)


def test_one_poll_fetches_at_most_its_cap(budget):
    budget(per_poll=3)
    poll = detail_memo.Poll("rippling")
    actions = []
    for i in range(6):
        action, _ = poll.decide(f"p{i}", "Engineer", "NYC")
        actions.append(action)
        if action == "fetch":
            poll.fetched()
    assert actions == ["fetch"] * 3 + ["skip"] * 3
    assert poll.n_skipped == 3 and poll.ingest_required is True


# ── Rippling ─────────────────────────────────────────────────────────────────

def _rippling_listing(n=3):
    return [{"uuid": f"{_P}r{i}", "name": f" Software Engineer {i} ",
             "department": {"id": "Eng", "label": "Engineering"},
             "url": f"https://ats.rippling.com/acme/jobs/{_P}r{i}",
             "workLocation": {"label": "Raleigh, NC", "id": "Raleigh, NC"}} for i in range(n)]


_RIPPLING_DETAIL = {
    "uuid": "x", "name": "Software Engineer",
    "description": {"company": "<p>About Acme.</p>", "role": "<p>Build the API in Python.</p>"},
    "workLocations": ["Raleigh, NC", "Durham, NC"],
    "createdOn": "2026-09-28T10:21:30.392000-07:00", "companyName": "Acme",
}


def _rippling(monkeypatch, listing, detail=_RIPPLING_DETAIL, detail_status=200):
    calls = {"listing": 0, "detail": 0}

    def get(url, **kw):
        if url.endswith("/jobs"):
            calls["listing"] += 1
            return _http(payload=listing)
        calls["detail"] += 1
        return _http(detail_status, payload=detail if detail_status == 200 else {})
    monkeypatch.setattr(rippling.httpx, "get", get)
    return rippling.RipplingScraper("acme"), calls


def test_rippling_fetches_each_description_once(monkeypatch, budget):
    s, calls = _rippling(monkeypatch, _rippling_listing(3))
    first = s.fetch()
    assert calls["detail"] == 3 and len(first) == 3
    j = first[0]
    assert "Build the API in Python" in j.description and "About Acme" in j.description
    assert j.posted_at is not None and j.posted_at.year == 2026
    assert j.geo.sites == ["Raleigh, NC", "Durham, NC"], "the detail names every site"
    assert j.listing_only is False and j.listing_fp
    assert s.ingest_required is True and s.fetch_complete is True
    assert _upsert(first, user_id=SHARED_POOL_USER) == 3

    s, calls = _rippling(monkeypatch, _rippling_listing(3))
    second = s.fetch()
    assert calls["detail"] == 0, "a posting the pool holds is not fetched again"
    assert [r.listing_only for r in second] == [True, True, True]
    assert all(r.description == "" for r in second)
    assert s.ingest_required is False and s.fetch_complete is True
    assert _upsert(second, user_id=SHARED_POOL_USER) == 0
    rows = _stored(JobSource.RIPPLING)
    assert len(rows) == 3 and all("Build the API" in r.description for r in rows.values()), (
        "a listing-only re-sighting blanked a stored description")


def test_a_new_posting_on_a_known_board_costs_one_request(monkeypatch, budget):
    s, _ = _rippling(monkeypatch, _rippling_listing(3))
    _upsert(s.fetch(), user_id=SHARED_POOL_USER)
    s, calls = _rippling(monkeypatch, _rippling_listing(4))
    raw = s.fetch()
    assert calls["detail"] == 1
    assert sorted(r.listing_only for r in raw) == [False, True, True, True]
    assert s.ingest_required is True


def test_without_budget_a_posting_waits_and_no_blind_row_is_written(monkeypatch, budget):
    budget(per_poll=2)
    s, calls = _rippling(monkeypatch, _rippling_listing(5))
    raw = s.fetch()
    assert calls["detail"] == 2 and len(raw) == 2, "three postings wait for the next poll"
    assert all(r.description for r in raw), "a row is never emitted without its description"
    assert s.fetch_complete is False, "the list we returned is a subset"
    assert s.listing_complete is True and len(s.listed_ids) == 5, (
        "…but the LISTING was whole: absence is judged against it, so a posting "
        "waiting for its detail is never read as gone")
    assert len(s.signature_entries) == 5


def test_a_title_nobody_wants_costs_no_request(monkeypatch, budget):
    listing = _rippling_listing(1) + [{
        "uuid": f"{_P}cashier", "name": "Cashier", "url": "https://ats.rippling.com/acme/jobs/c",
        "workLocation": {"label": "Raleigh, NC"}}]
    s, calls = _rippling(monkeypatch, listing)
    raw = s.fetch()
    assert calls["detail"] == 1 and [r.title for r in raw] == ["Software Engineer 0"]
    assert f"{_P}cashier" in s.listed_ids, "still LISTED, so never closed as absent"


@pytest.mark.parametrize("status", [403, 429])
def test_a_refused_detail_stops_the_poll(monkeypatch, budget, status):
    """The detail is on the same host as the listing: a refusal there is the
    same refusal, and asking for the next posting after it is how a rate limit
    becomes a block."""
    s, calls = _rippling(monkeypatch, _rippling_listing(5), detail_status=status)
    with pytest.raises(BoardBlockedError):
        s.fetch()
    assert calls["detail"] == 1


def test_a_detail_that_does_not_arrive_is_retried_next_poll(monkeypatch, budget):
    s, calls = _rippling(monkeypatch, _rippling_listing(2), detail_status=500)
    assert s.fetch() == [] and s.fetch_complete is False
    s, calls = _rippling(monkeypatch, _rippling_listing(2))
    assert len(s.fetch()) == 2 and calls["detail"] == 2


def test_rippling_without_the_feature_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "detail_fetch_per_minute", "", raising=False)
    s, calls = _rippling(monkeypatch, _rippling_listing(2) + [{
        "uuid": f"{_P}cashier", "name": "Cashier", "workLocation": {"label": "NC"}}])
    raw = s.fetch()
    assert calls["detail"] == 0 and len(raw) == 3, "every listed posting, no detail requests"
    assert not any(r.listing_only or r.listing_fp for r in raw)
    assert getattr(s, "signature_entries", None) is None or s.signature_entries == []


# ── Breezy ───────────────────────────────────────────────────────────────────

def _breezy(monkeypatch, n=2, page_status=200):
    calls = {"listing": 0, "page": 0}
    listing = [{"id": f"{_P}b{i}", "name": f"Software Engineer {i}",
                "url": f"https://acme.breezy.hr/p/{_P}b{i}-software-engineer",
                "location": {"city": "Toronto", "country": {"name": "Canada"}, "is_remote": False},
                "published_date": "2026-11-01T10:00:00Z"} for i in range(n)]

    def get(url, **kw):
        if url.endswith("/json"):
            calls["listing"] += 1
            return _http(payload=listing)
        calls["page"] += 1
        return _http(page_status, text=(
            '<html><body><div class="description"><h2>About Us</h2>'
            '<p>We build software in Go.</p></div></body></html>'))
    monkeypatch.setattr(breezy.httpx, "get", get)
    return breezy.BreezyScraper("acme"), calls


def test_breezy_reads_the_description_from_the_position_page_once(monkeypatch, budget):
    s, calls = _breezy(monkeypatch)
    first = s.fetch()
    assert calls["page"] == 2 and all("We build software in Go" in r.description for r in first)
    assert _upsert(first, user_id=SHARED_POOL_USER) == 2
    s, calls = _breezy(monkeypatch)
    second = s.fetch()
    assert calls["page"] == 0 and all(r.listing_only for r in second)
    _upsert(second, user_id=SHARED_POOL_USER)
    assert all("We build software" in r.description for r in _stored(JobSource.BREEZY).values())


def test_a_throttled_breezy_page_stops_the_poll(monkeypatch, budget):
    s, calls = _breezy(monkeypatch, n=4, page_status=429)
    with pytest.raises(BoardBlockedError):
        s.fetch()
    assert calls["page"] == 1


# ── join.com ─────────────────────────────────────────────────────────────────

_JOIN_PAGE = ('<html><script type="application/ld+json">' + json.dumps({
    "@context": "https://schema.org", "@type": "JobPosting", "title": "Software Engineer",
    "description": "<p>Wir bauen <b>Software</b> in Kotlin.</p>",
    "datePosted": "2026-11-13T09:53:09.111Z"}) + '</script></html>')


def _join(monkeypatch, n=2, page=_JOIN_PAGE, page_status=200):
    calls = {"company": 0, "query": 0, "page": 0}
    items = [{"id": 9000 + i, "idParam": f"{9000 + i}-software-engineer",
              "title": f"Software Engineer {i}", "createdAt": "2026-11-13T09:53:09.111Z",
              "workplaceType": "HYBRID", "city": {"cityName": "Berlin", "countryName": "Germany"}}
             for i in range(n)]

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, **kw):
            if "/jobs/" in url:
                calls["page"] += 1
                return _http(page_status, text=page)
            calls["company"] += 1
            return _http(text='{"company":{"id":77,"domain":"acme"}}')

        def post(self, url, json=None, **kw):
            calls["query"] += 1
            return _http(payload={"data": {"publicJobs": {
                "items": items, "pageInfo": {"pageCount": 1, "rowCount": len(items)}}}})

    monkeypatch.setattr(join_mod.httpx, "Client", _C)
    return join_mod.JoinScraper("acme"), calls


def test_join_reads_the_description_from_the_job_page_once(monkeypatch, budget):
    s, calls = _join(monkeypatch)
    first = s.fetch()
    assert calls["page"] == 2
    assert all(r.description == "Wir bauen \nSoftware\n in Kotlin." or "Kotlin" in r.description
               for r in first)
    assert all(r.listing_fp and not r.listing_only for r in first)
    assert s.ingest_required is True and s.fetch_complete is True
    s, calls = _join(monkeypatch)
    for r in first:                                   # what the upsert would have recorded
        detail_memo.remember("join", r.external_id, r.listing_fp)
    second = s.fetch()
    assert calls["page"] == 0 and all(r.listing_only for r in second)
    assert s.ingest_required is False and s.fetch_complete is True


def test_a_join_page_without_a_description_waits(monkeypatch, budget):
    s, calls = _join(monkeypatch, page="<html>no structured data</html>")
    assert s.fetch() == [], "no description, no row — it is asked again next poll"
    assert s.fetch_complete is False and s.listing_complete is True
    assert len(s.listed_ids) == 2


def test_a_throttled_join_page_turns_details_off_for_the_poll(monkeypatch, budget):
    s, calls = _join(monkeypatch, n=5, page_status=429)
    assert s.fetch() == []
    assert calls["page"] == 1, "one throttled page is the last page asked for this poll"


def test_every_posting_waiting_is_not_an_unreadable_board(monkeypatch, budget):
    """The count says 3 postings and we return none because all three are
    waiting for budget: that is not "an answer we cannot read"."""
    budget(per_poll=1)
    s, calls = _join(monkeypatch, n=3, page="<html>nothing</html>")
    assert s.fetch() == []                               # must not raise


# ── the upsert ───────────────────────────────────────────────────────────────

def _job(ext, description, listing_only=False, fp=None, location="Austin, TX"):
    return RawJob(source="rippling", external_id=f"{_P}{ext}", company=f"{_P}co",
                  title="Software Engineer", location=location, remote=False,
                  url="https://example.test/j", description=description,
                  listing_only=listing_only, listing_fp=fp)


def test_a_listing_only_row_never_blanks_a_stored_description(budget):
    fp = detail_memo.fingerprint("Software Engineer", "Austin, TX")
    assert _upsert([_job("u1", "The real description, in full.", fp=fp)],
                   user_id=SHARED_POOL_USER) == 1
    before = _stored(JobSource.RIPPLING)[f"{_P}u1"]
    assert _upsert([_job("u1", "", listing_only=True)], user_id=SHARED_POOL_USER) == 0
    after = _stored(JobSource.RIPPLING)[f"{_P}u1"]
    assert after.description == "The real description, in full."
    assert after.content_hash == before.content_hash and after.embedding_id == before.embedding_id


def test_a_listing_only_row_refreshes_a_stale_last_seen(budget):
    fp = detail_memo.fingerprint("Software Engineer", "Austin, TX")
    _upsert([_job("u2", "text", fp=fp)], user_id=SHARED_POOL_USER)
    old = datetime.utcnow() - timedelta(days=2)
    with get_session() as s:
        row = s.exec(select(Job).where(Job.external_id == f"{_P}u2")).one()
        row.last_seen = old
        s.add(row)
        s.commit()
    _upsert([_job("u2", "", listing_only=True)], user_id=SHARED_POOL_USER)
    assert _stored(JobSource.RIPPLING)[f"{_P}u2"].last_seen > old + timedelta(days=1)


def test_a_listing_only_row_with_nothing_behind_it_is_never_inserted_and_is_forgotten(budget):
    fp = detail_memo.fingerprint("Software Engineer", "Austin, TX")
    detail_memo.remember("rippling", f"{_P}ghost", fp)
    assert _upsert([_job("ghost", "", listing_only=True)], user_id=SHARED_POOL_USER) == 0
    assert f"{_P}ghost" not in _stored(JobSource.RIPPLING), "a blind row was written"
    assert detail_memo.needs_detail("rippling", f"{_P}ghost", fp) is True, (
        "the adapter must fetch that posting's detail again")


def test_only_the_shared_door_teaches_the_memory(budget):
    """The memory describes the SHARED pool. A user's door writing a copy says
    nothing about whether the pool holds the posting."""
    fp = detail_memo.fingerprint("Software Engineer", "Austin, TX")
    _upsert([_job("u3", "text", fp=fp)], user_id=f"{_P}user",
            user_keywords=["Software Engineer"])
    assert detail_memo.needs_detail("rippling", f"{_P}u3", fp) is True
    with get_session() as s:
        s.exec(delete(Job).where(Job.user_id == f"{_P}user"))
        s.commit()


# ── the pulse lane ───────────────────────────────────────────────────────────

def test_a_new_description_is_ingested_even_when_the_listing_is_unchanged(monkeypatch):
    """The listing signature is ids and titles. A description arriving for a
    posting already listed changes neither — and without this the lane would
    skip the result and the description would never be stored."""
    with get_session() as s:
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_P}%")))
        s.commit()
    pulse_lane._VOLATILE_SIGS.clear()
    entries = [(f"{_P}p1", "Engineer")]
    with get_session() as s:
        s.add(CompanyRegistry(slug=f"{_P}board", ats=JobSource.RIPPLING, company_name="x",
                              is_active=True, job_count=1,
                              poll_hash=pulse_lane._signature_from_entries(entries),
                              next_poll_at=datetime.utcnow() - timedelta(hours=2)))
        s.commit()

    def mine():
        with get_session() as s:
            return s.exec(select(CompanyRegistry).where(
                CompanyRegistry.slug.like(f"{_P}%"))).all()

    def run(ingest_required):
        scraper = SimpleNamespace(
            fetch=lambda: [_job("p1", "now with its description")],
            fetch_complete=True, signature_entries=entries, signature_stable=True,
            ingest_required=ingest_required)
        calls: list = []
        monkeypatch.setattr(pulse_lane, "_due_boards", lambda now, limit: mine()[:limit])
        monkeypatch.setattr("app.discovery.pipeline.scraper_for", lambda ats, slug, url: scraper)
        monkeypatch.setattr("app.strategy.hot_lane._active_users", lambda: [])
        monkeypatch.setattr(pulse_lane, "_watchlist_terms", lambda: set())
        monkeypatch.setattr("app.discovery.pipeline._upsert",
                            lambda raw, **kw: calls.append(list(raw)) or 0)
        return pulse_lane.run_pulse_tick(), calls

    stats, calls = run(ingest_required=False)
    assert stats["unchanged"] == 1 and calls == [], "an unchanged listing does no work"
    with get_session() as s:
        row = s.exec(select(CompanyRegistry).where(CompanyRegistry.slug == f"{_P}board")).one()
        row.next_poll_at = datetime.utcnow() - timedelta(hours=2)
        s.add(row)
        s.commit()
    stats, calls = run(ingest_required=True)
    assert stats["changed"] == 1 and len(calls) == 1, (
        "a result carrying a new description was skipped as 'unchanged'")


# ── an unchanged listing costs no detail requests (Workday, SmartRecruiters) ──
# These two pay a request PER POSTING on top of the listing — up to a hundred —
# and paid it on every poll, changed or not. Within a day of Workday's refusals
# being recorded at all (2026-11-17), 2,105 of 2,407 boards had been answered
# 429. The pulse lane already knows what a board's listing looked like when it
# was last ingested in full (`poll_hash`); it now tells the adapter, which
# compares its listing first and skips the details when nothing changed.

from app.discovery import smartrecruiters, workday  # noqa: E402
from app.discovery.base import listing_signature  # noqa: E402
from app.discovery.pipeline import (  # noqa: E402
    THROTTLED_BOARD_BACKOFF_HOURS, UNSENT_BOARD_BACKOFF_HOURS, spared_backoff_hours,
)


class _Workday:
    """A one-page Workday tenant that counts listing POSTs and detail GETs."""

    def __init__(self, n=4, detail_status=200):
        self.n, self.detail_status = n, detail_status
        self.posts = self.gets = 0

    def post(self, url, json=None, **kw):
        self.posts += 1
        rows = [] if json["offset"] else [
            {"title": f"Software Engineer {i}",
             "externalPath": f"/job/Austin/Software-Engineer-{i}_R{i:03d}"} for i in range(self.n)]
        return _http(payload={"jobPostings": rows, "total": self.n})

    def get(self, url, **kw):
        self.gets += 1
        if self.detail_status != 200:
            return _http(self.detail_status, payload={})
        return _http(payload={"jobPostingInfo": {
            "jobReqId": url.rsplit("_", 1)[-1], "jobDescription": "<p>Build things.</p>",
            "location": "Austin, TX", "startDate": "2026-11-10"}})


def _workday(monkeypatch, fake, known=None):
    monkeypatch.setattr(workday.httpx, "post", fake.post)
    monkeypatch.setattr(workday.httpx, "get", fake.get)
    s = workday.WorkdayScraper("acme", "https://acme.wd1.myworkdayjobs.com/External")
    if known is not None:
        s.known_signature = known
    return s


def test_workday_fetches_details_when_the_lane_knows_nothing(monkeypatch):
    """The full pass and the hot lane never say what was ingested: full fetch."""
    fake = _Workday(4)
    s = _workday(monkeypatch, fake)
    jobs = s.fetch()
    assert len(jobs) == 4 and fake.gets == 4
    assert all("Build things" in j.description for j in jobs)
    assert s.fetch_complete is True and s.listing_complete is True


def test_an_unchanged_workday_listing_makes_no_detail_request(monkeypatch):
    fake = _Workday(4)
    first = _workday(monkeypatch, fake)
    first.fetch()
    ingested = listing_signature(first.signature_entries)

    fake = _Workday(4)
    s = _workday(monkeypatch, fake, known=ingested)
    assert s.fetch() == []
    assert fake.gets == 0, f"{fake.gets} detail requests for a board that did not change"
    assert fake.posts >= 1
    assert listing_signature(s.signature_entries) == ingested, (
        "the lane still needs the listing to see that nothing changed")
    assert s.fetch_complete is False, "an empty list must never claim to be the whole board"
    assert s.listing_complete is True and len(s.listed_urls) == 4


def test_a_changed_workday_listing_is_fetched_in_full(monkeypatch):
    first = _workday(monkeypatch, _Workday(4))
    first.fetch()
    fake = _Workday(5)                                   # one more posting
    s = _workday(monkeypatch, fake, known=listing_signature(first.signature_entries))
    assert len(s.fetch()) == 5 and fake.gets == 5


@pytest.mark.parametrize("status", [403, 429])
def test_a_refused_workday_detail_stops_the_requests(monkeypatch, status):
    """It used to `continue` to the next posting: ninety-nine more requests to
    a site that had just said stop."""
    fake = _Workday(8, detail_status=status)
    s = _workday(monkeypatch, fake)
    assert s.fetch() == []
    assert fake.gets == 1
    assert s.fetch_complete is False and len(s.signature_entries) == 8


def test_an_unchanged_smartrecruiters_listing_makes_no_detail_request(monkeypatch):
    calls = {"listing": 0, "detail": 0}
    listing = {"content": [{"id": str(i), "name": f"Software Engineer {i}", "location": {}}
                           for i in range(3)], "totalFound": 3}

    def get(url, **kw):
        if url.endswith("/postings"):
            calls["listing"] += 1
            return _http(payload=listing)
        calls["detail"] += 1
        return _http(payload={"jobAd": {"sections": {"jobDescription": {"text": "<p>Go.</p>"}}}})

    monkeypatch.setattr(smartrecruiters.httpx, "get", get)
    s = smartrecruiters.SmartRecruitersScraper("acme")
    assert len(s.fetch()) == 3 and calls["detail"] == 3
    ingested = listing_signature(s.signature_entries)

    calls["detail"] = 0
    s = smartrecruiters.SmartRecruitersScraper("acme")
    s.known_signature = ingested
    assert s.fetch() == [] and calls["detail"] == 0
    assert s.fetch_complete is False and s.listing_complete is True
    assert s.listed_ids == {"0", "1", "2"}, "absence is still judged against the listing"

    listing["content"].append({"id": "9", "name": "Software Engineer 9", "location": {}})
    listing["totalFound"] = 4
    s = smartrecruiters.SmartRecruitersScraper("acme")
    s.known_signature = ingested
    assert len(s.fetch()) == 4 and calls["detail"] == 4


def test_the_lane_tells_the_adapter_what_it_ingested_and_does_no_work(monkeypatch):
    with get_session() as s:
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_P}%")))
        s.commit()
    pulse_lane._VOLATILE_SIGS.clear()
    entries = [(f"{_P}w1", "Engineer"), (f"{_P}w2", "Scientist")]
    stored = pulse_lane._signature_from_entries(entries)
    assert stored == listing_signature(entries), "lane and adapters must hash identically"
    with get_session() as s:
        s.add(CompanyRegistry(slug=f"{_P}wd", ats=JobSource.WORKDAY, company_name="x",
                              is_active=True, job_count=2, poll_hash=stored,
                              next_poll_at=datetime.utcnow() - timedelta(hours=2)))
        s.commit()

    def mine():
        with get_session() as s:
            return s.exec(select(CompanyRegistry).where(
                CompanyRegistry.slug.like(f"{_P}%"))).all()

    # An adapter that did what Workday now does: saw its listing was the one
    # already ingested, skipped the details and returned nothing.
    scraper = SimpleNamespace(fetch=lambda: [], fetch_complete=False,
                              signature_entries=entries, signature_stable=True)
    calls: list = []
    monkeypatch.setattr(pulse_lane, "_due_boards", lambda now, limit: mine()[:limit])
    monkeypatch.setattr("app.discovery.pipeline.scraper_for", lambda ats, slug, url: scraper)
    monkeypatch.setattr("app.strategy.hot_lane._active_users", lambda: [])
    monkeypatch.setattr(pulse_lane, "_watchlist_terms", lambda: set())
    monkeypatch.setattr("app.discovery.pipeline._upsert",
                        lambda raw, **kw: calls.append(list(raw)) or 0)
    stats = pulse_lane.run_pulse_tick()
    assert scraper.known_signature == stored, "the adapter was not told what was ingested"
    assert stats["unchanged"] == 1 and stats["fetch_ok"] == 1 and calls == []
    with get_session() as s:
        row = s.exec(select(CompanyRegistry).where(CompanyRegistry.slug == f"{_P}wd")).one()
    assert row.job_count == 2 and row.poll_hash == stored and row.is_active, (
        "an early exit must not read as an empty board")


def test_a_request_we_never_sent_is_retried_in_an_hour_not_six():
    """join.com's own pace being full is our choice, not the site's answer."""
    msg = "join: not sent — our own pace for join.com is full (rate limit)"
    assert spared_backoff_hours(msg) == UNSENT_BOARD_BACKOFF_HOURS == 1
    assert spared_backoff_hours("join jobs API returned 429") == THROTTLED_BOARD_BACKOFF_HOURS


# ── Workday: a known posting is recognised by its URL ────────────────────────
# Workday's listing names a posting by its path; the requisition id its row is
# stored under is only on the detail record. So "does the pool hold it?" is
# asked by URL (detail_memo.alias), and a re-sighting costs no request.

def _stored_workday():
    with get_session() as s:
        return s.exec(select(Job).where(Job.user_id == SHARED_POOL_USER,
                                        Job.source == JobSource.WORKDAY,
                                        Job.url.like("https://acme.wd1.myworkdayjobs.com/%"))).all()


@pytest.fixture
def _clean_workday():
    yield
    with get_session() as s:
        s.exec(delete(Job).where(Job.source == JobSource.WORKDAY,
                                 Job.url.like("https://acme.wd1.myworkdayjobs.com/%")))
        s.commit()


def test_workday_fetches_each_detail_once(monkeypatch, budget, _clean_workday):
    budget("workday=600")
    fake = _Workday(4)
    s = _workday(monkeypatch, fake)
    first = s.fetch()
    assert fake.gets == 4 and len(first) == 4 and s.ingest_required is True
    assert all(r.listing_fp and not r.listing_only for r in first)
    assert _upsert(first, user_id=SHARED_POOL_USER) == 4

    fake = _Workday(5)                                   # one new posting
    s = _workday(monkeypatch, fake)                      # (no known_signature: a changed board)
    second = s.fetch()
    assert fake.gets == 1, f"{fake.gets} detail requests; only the new posting needs one"
    assert sorted(r.listing_only for r in second) == [False, True, True, True, True]
    held = {r.external_id for r in second if r.listing_only}
    assert held == {r.external_id for r in first}, (
        "a re-sighting must carry the id its row is stored under")
    assert _upsert(second, user_id=SHARED_POOL_USER) == 1
    rows = _stored_workday()
    assert len(rows) == 5 and all("Build things" in r.description for r in rows), (
        "a listing-only re-sighting blanked a stored description")


def test_workday_knows_what_the_pool_holds_after_a_restart(monkeypatch, budget, _clean_workday):
    """A deploy empties the process's memory. The pool's rows (id + URL) are
    read back, so a restart does not cost a detail request per stored posting."""
    budget("workday=600")
    s = _workday(monkeypatch, _Workday(3))
    _upsert(s.fetch(), user_id=SHARED_POOL_USER)
    detail_memo.reset()                                  # the restart
    fake = _Workday(3)
    s = _workday(monkeypatch, fake)
    raw = s.fetch()
    assert fake.gets == 0 and len(raw) == 3 and all(r.listing_only for r in raw)


def test_a_workday_posting_without_budget_waits(monkeypatch, budget):
    budget("workday=600", per_poll=2)
    fake = _Workday(5)
    s = _workday(monkeypatch, fake)
    raw = s.fetch()
    assert fake.gets == 2 and len(raw) == 2 and all(r.description for r in raw)
    assert s.fetch_complete is False and len(s.signature_entries) == 5, (
        "three postings wait; the lane must not record this listing as ingested")


def test_workday_requests_are_spaced(monkeypatch):
    slept: list = []
    monkeypatch.setattr(settings, "workday_request_spacing_seconds", 1.0, raising=False)
    monkeypatch.setattr(workday.time, "sleep", lambda s: slept.append(s))
    fake = _Workday(3)
    _workday(monkeypatch, fake).fetch()
    assert fake.posts + fake.gets >= 4
    assert len(slept) == fake.posts + fake.gets - 1, (
        "every request after the first waits its gap")
    assert set(slept) == {1.0}
