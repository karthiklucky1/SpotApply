"""A board's poll signature must reflect its postings, not its fetch luck.

Production measured 93% of ALL changed-board events coming from ONE source's
fetch jitter: Workday's parsed list shrinks with every failed per-posting
detail GET, so its poll-hash flipped on 32.6% of polls (everything else ≤1.4%)
— 983 boards oscillating up/down symmetrically, each false "change" paying the
full upsert_shared consumer cost while real change detection drowned in noise.

The contract pinned here:

  * an N+1 adapter (Workday, SmartRecruiters) exposes LISTING-phase
    ``signature_entries`` — stable identity collected before any detail fetch —
    and the pulse tick hashes those in preference to the parsed list;
  * a fetch whose signature basis varies with WHERE it died
    (``signature_stable = False``) must never overwrite the stored baseline —
    comparisons against a volatile hash read as perpetual change;
  * ``job_count`` comes from the listing when available — a live board must
    not be stamped toward the zero-yield tier because its detail endpoint had
    a bad afternoon;
  * a partial fetch is flagged ``fetch_complete = False`` even when the only
    loss was a detail GET (ghost-close must never run on a subset).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlmodel import delete, select

from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource
from app.discovery.base import RawJob
from app.strategy import pulse_lane

_PREFIX = "sig-"


def _board(slug: str, *, poll_hash=None, job_count: int = 5) -> int:
    with get_session() as session:
        row = CompanyRegistry(
            slug=_PREFIX + slug, ats=JobSource.GREENHOUSE,
            company_name=_PREFIX + slug, is_active=True, job_count=job_count,
            poll_hash=poll_hash,
            next_poll_at=datetime.utcnow() - timedelta(hours=2),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _get(bid: int) -> CompanyRegistry:
    with get_session() as session:
        return session.exec(
            select(CompanyRegistry).where(CompanyRegistry.id == bid)).one()


def _cleanup() -> None:
    with get_session() as session:
        session.exec(
            delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_PREFIX}%")))
        session.commit()


def _raw(eid: str, title: str) -> RawJob:
    return RawJob(source="greenhouse", external_id=eid, company="Sig Co",
                  title=title, location="", remote=False, url="", description="")


def _mine():
    with get_session() as session:
        return session.exec(
            select(CompanyRegistry)
            .where(CompanyRegistry.slug.like(f"{_PREFIX}%"))).all()


def _tick_with(monkeypatch, scraper, upsert_calls=None):
    monkeypatch.setattr(pulse_lane, "_due_boards", lambda now, limit: _mine()[:limit])
    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, url: scraper)
    monkeypatch.setattr("app.strategy.hot_lane._active_users", lambda: [])
    monkeypatch.setattr(pulse_lane, "_watchlist_terms", lambda: set())
    if upsert_calls is not None:
        def _spy_upsert(raw, **kw):
            upsert_calls.append(list(raw))
            return 0
        monkeypatch.setattr("app.discovery.pipeline._upsert", _spy_upsert)
    return pulse_lane.run_pulse_tick()


# ── listing entries beat the parsed list ─────────────────────────────────────

def test_detail_fetch_jitter_does_not_read_as_change(monkeypatch):
    """The 93%-of-changed-events bug in one test: same listing, one detail GET
    fails, parsed list shrinks — the board must be UNCHANGED and pay zero
    downstream work."""
    init_db()
    _cleanup()
    entries = [("p1", "Engineer"), ("p2", "Scientist"), ("p3", "Analyst")]
    stored = pulse_lane._signature_from_entries(entries)
    bid = _board("jitter-co", poll_hash=stored)

    scraper = SimpleNamespace(
        fetch=lambda: [_raw("p1", "Engineer"), _raw("p3", "Analyst")],  # p2's detail failed
        fetch_complete=False,
        signature_entries=entries,
        signature_stable=True,
    )
    calls: list = []
    stats = _tick_with(monkeypatch, scraper, upsert_calls=calls)

    assert stats["unchanged"] == 1 and stats["changed"] == 0
    assert calls == [], "a jittered-but-identical board must do no upsert work"
    row = _get(bid)
    assert row.poll_hash == stored
    assert row.job_count == 3, (
        "job_count must come from the LISTING (3), not the jittered parsed "
        "list (2) — undercounting demotes live boards toward the 72h tier")
    _cleanup()


def test_without_entries_the_same_jitter_reads_as_change(monkeypatch):
    """The counterfactual that motivates the whole fix: an adapter that only
    hands over the parsed list turns detail jitter into a hash flip."""
    init_db()
    _cleanup()
    full = [_raw("p1", "Engineer"), _raw("p2", "Scientist")]
    stored = pulse_lane._board_signature(full)
    _board("noentries-co", poll_hash=stored)

    scraper = SimpleNamespace(fetch=lambda: [full[0]])   # p2 dropped
    calls: list = []
    stats = _tick_with(monkeypatch, scraper, upsert_calls=calls)
    assert stats["changed"] == 1
    _cleanup()


def test_a_real_listing_change_is_still_detected(monkeypatch):
    init_db()
    _cleanup()
    stored = pulse_lane._signature_from_entries([("p1", "Engineer")])
    bid = _board("growing-co", poll_hash=stored)
    entries = [("p1", "Engineer"), ("p2", "New Role")]
    scraper = SimpleNamespace(
        fetch=lambda: [_raw("p1", "Engineer"), _raw("p2", "New Role")],
        fetch_complete=True,
        signature_entries=entries,
        signature_stable=True,
    )
    stats = _tick_with(monkeypatch, scraper, upsert_calls=[])
    assert stats["changed"] == 1
    assert _get(bid).poll_hash == pulse_lane._signature_from_entries(entries)
    _cleanup()


# ── volatile signatures never clobber the baseline ───────────────────────────

def test_unstable_partial_fetch_keeps_the_stored_baseline(monkeypatch):
    """An adapter that died mid-listing hands over a hash that varies with
    where it died. The poll is still recorded (the fetch DID complete enough
    to return postings), but the stored baseline must survive."""
    init_db()
    _cleanup()
    stored = "baseline-hash"
    bid = _board("dying-fetch-co", poll_hash=stored)
    scraper = SimpleNamespace(
        fetch=lambda: [_raw("p1", "Engineer")],
        fetch_complete=False,
        signature_stable=False,
    )
    stats = _tick_with(monkeypatch, scraper, upsert_calls=[])
    assert stats["fetch_ok"] == 1
    row = _get(bid)
    assert row.poll_hash == stored, (
        "a volatile signature overwrote the baseline — every subsequent poll "
        "now reads as change")
    assert row.last_seen is not None, "the poll itself must still be recorded"
    _cleanup()


def test_unstable_fetch_still_bootstraps_an_empty_baseline(monkeypatch):
    """No stored hash at all → writing even a volatile one beats writing none:
    the next stable poll corrects it, whereas NULL compares as changed forever."""
    init_db()
    _cleanup()
    bid = _board("fresh-co", poll_hash=None)
    scraper = SimpleNamespace(
        fetch=lambda: [_raw("p1", "Engineer")],
        fetch_complete=False,
        signature_stable=False,
    )
    _tick_with(monkeypatch, scraper, upsert_calls=[])
    assert _get(bid).poll_hash is not None
    _cleanup()


# ── the adapters actually produce what the tick consumes ─────────────────────

def _wd_scraper():
    from app.discovery.workday import WorkdayScraper
    return WorkdayScraper("acme", "https://acme.myworkdayjobs.com/External")


def test_workday_entries_are_stable_under_detail_failures(monkeypatch):
    from app.discovery import workday

    listing = {"jobPostings": [
        {"title": "Software Engineer", "externalPath": "/job/eng-1"},
        {"title": "Data Scientist", "externalPath": "/job/sci-2"},
        {"title": "Sales Rep", "externalPath": "/job/sales-3"},   # filtered
        {"title": "Platform Engineer", "externalPath": "/job/eng-4"},
    ], "total": 4}

    def fake_post(url, **kw):
        return SimpleNamespace(status_code=200, json=lambda: listing)

    def fake_get(url, **kw):
        if "sci-2" in url:
            raise workday.httpx.ConnectError("detail died")
        return SimpleNamespace(status_code=200, json=lambda: {
            "jobPostingInfo": {"jobReqId": url.rsplit("/", 1)[-1],
                               "jobDescription": "d", "location": "NYC"}})

    monkeypatch.setattr(workday.httpx, "post", fake_post)
    monkeypatch.setattr(workday.httpx, "get", fake_get)

    s = _wd_scraper()
    jobs = s.fetch()

    assert [e[0] for e in s.signature_entries] == ["/job/eng-1", "/job/sci-2", "/job/eng-4"]
    assert len(jobs) == 2, "the failed detail drops the JOB, never the ENTRY"
    assert s.signature_stable is True
    assert s.fetch_complete is False, (
        "a posting is live but missing from the parsed list — ghost-close on "
        "this result would close it")


def test_workday_mid_pagination_death_marks_signature_volatile(monkeypatch):
    from app.discovery import workday

    pages = iter([
        SimpleNamespace(status_code=200, json=lambda: {"jobPostings": [
            {"title": "Engineer %d" % i, "externalPath": "/job/e%d" % i}
            for i in range(20)], "total": 40}),
        SimpleNamespace(status_code=500, json=lambda: {}),
    ])
    monkeypatch.setattr(workday.httpx, "post", lambda url, **kw: next(pages))
    monkeypatch.setattr(workday.httpx, "get", lambda url, **kw: SimpleNamespace(
        status_code=200, json=lambda: {"jobPostingInfo": {"jobReqId": "r",
                                                          "jobDescription": "d"}}))
    s = _wd_scraper()
    jobs = s.fetch()
    assert jobs is not None and len(jobs) == 20
    assert s.fetch_complete is False
    assert s.signature_stable is False, (
        "entries end where the pagination died — that hash must never be stored")


def test_smartrecruiters_entries_come_from_the_listing(monkeypatch):
    from app.discovery import smartrecruiters as sr

    listing = {"content": [
        {"id": "111", "name": "Backend Engineer", "location": {}},
        {"id": "222", "name": "ML Engineer", "location": {}},
        {"id": None, "name": "Broken"},                      # no id → skipped
        {"id": "333", "name": "Recruiter", "location": {}},  # non-tech → skipped
    ], "totalFound": 4}

    def fake_get(url, **kw):
        if url.endswith("/postings"):
            return SimpleNamespace(status_code=200, json=lambda: listing)
        if url.endswith("/222"):
            raise sr.httpx.ConnectError("detail died")
        return SimpleNamespace(status_code=200, json=lambda: {"jobAd": {"sections": {}}})

    monkeypatch.setattr(sr.httpx, "get", fake_get)
    s = sr.SmartRecruitersScraper("acme")
    jobs = s.fetch()

    assert [e[0] for e in s.signature_entries] == ["111", "222"]
    assert len(jobs) == 1
    assert s.signature_stable is True
    assert s.fetch_complete is False
