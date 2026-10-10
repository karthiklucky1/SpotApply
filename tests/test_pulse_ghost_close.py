"""The pulse lane closes postings that LEFT a board it fetched completely.

Live test 2026-10-09: a closed Hasbro Greenhouse posting was still open in the
shared pool with copies on other users' boards. The only ghost-closer was the
full discovery pass, which rotates boards by `last_seen` ascending — the column
the pulse lane bumps on every poll — so the boards the pulse lane watched were
the ones never ghost-closed. Ashby has no per-posting endpoint at all, so for
Ashby board absence is the ONLY closure signal.

What this pins (app/discovery/board_absence.py, app/strategy/pulse_lane.py):

  * a COMPLETE fetch missing a posting closes the shared row and its waiting
    copies exactly as `delivery_gate._close_copies` does (Removed with a
    "job closed" note; TAILORED keeps its place, only is_closed);
  * only sources whose ids and URLs pin a posting to one board close at all —
    Workday, Teamtailor, BambooHR and Workable never do (CLOSING_SOURCES);
  * an incomplete, failed, throttled/blocked, undeclared or DEFERRED fetch
    closes nothing;
  * another board's rows, aggregator rows, another company's copy and a row
    newer than the fetch are never closed;
  * the doubt guard (mass disappearance — decided before any row is read and
    remembered while no new listing could resolve it; a collapsed listing — on
    ONE poll or several; an empty listing) closes nothing;
  * one closure budget per tick, the overflow carried to the next tick
    WITHOUT re-polling or pulling the board's schedule forward, and a board
    that owes nothing is never held; reopening never waits on the budget;
  * the step runs after routing, in ONE session, in a bounded slice, locks
    job rows ascending before any application, and records liveness as one
    upsert under a SAVEPOINT;
  * a posting a later complete fetch lists again is reopened;
  * PULSE_GHOST_CLOSE_ENABLED=0 changes nothing.

Every row here is prefixed and only those rows are cleaned up.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import engine, get_session, init_db
from app.db.models import (
    Application, ApplicationStatus, CompanyRegistry, Job, JobLiveness,
    JobSource,
)
from app.discovery import board_absence
from app.discovery.base import RawJob
from app.discovery.pipeline import SHARED_POOL_USER
from app.strategy import pulse_lane

_P = "gab-"                      # external ids, registry slugs
_CO = "Gabsent Co"               # company name (Greenhouse boards)
_GH = "https://job-boards.greenhouse.io/gabsentco/jobs/"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _reset_memory() -> None:
    pulse_lane._VOLATILE_SIGS.clear()
    pulse_lane._ABSENCE_CARRY.clear()
    pulse_lane._COLLAPSE_STREAK.clear()
    board_absence.reset_state()


def _cleanup() -> None:
    with get_session() as s:
        ids = s.exec(select(Job.id).where(Job.external_id.like(f"%{_P}%"))).all()
        if ids:
            s.exec(delete(Application).where(Application.job_id.in_(ids)))
            s.exec(delete(Job).where(Job.id.in_(ids)))
        s.exec(delete(JobLiveness).where(JobLiveness.external_id.like(f"%{_P}%")))
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_P}%")))
        s.commit()
    _reset_memory()


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    _cleanup()
    yield
    _cleanup()


def _job(ext: str, *, url: str, uid=SHARED_POOL_USER, company: str = _CO,
         source=JobSource.GREENHOUSE, closed: bool = False, reason=None,
         origin=None, status=None, first_seen=None) -> int:
    with get_session() as s:
        j = Job(source=source, external_id=ext, company=company,
                title=f"Engineer {ext}", url=url, user_id=uid,
                is_closed=closed, closed_reason=reason, origin=origin)
        if first_seen is not None:
            j.first_seen = first_seen
            j.discovered_at = first_seen
        s.add(j)
        s.commit()
        s.refresh(j)
        if status is not None:
            s.add(Application(job_id=j.id, user_id=uid, status=status))
            s.commit()
        return j.id


def _gh(n) -> str:
    return f"{_P}{n}"


def _raw(ext: str, url: str, *, company: str = _CO, source: str = "greenhouse") -> RawJob:
    return RawJob(source=source, external_id=ext, company=company,
                  title=f"Engineer {ext}", location="", remote=False, url=url,
                  description="")


def _board(slug: str, *, ats=JobSource.GREENHOUSE, job_count: int = 4,
           career_url=None) -> int:
    with get_session() as s:
        row = CompanyRegistry(slug=_P + slug, ats=ats, company_name=_P + slug,
                              is_active=True, job_count=job_count,
                              poll_hash="baseline-before-the-change",
                              career_url=career_url,
                              next_poll_at=datetime.utcnow() - timedelta(hours=1))
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _mine():
    with get_session() as s:
        return s.exec(select(CompanyRegistry)
                      .where(CompanyRegistry.slug.like(f"{_P}%"))).all()


def _row(bid: int) -> CompanyRegistry:
    return next(r for r in _mine() if r.id == bid)


def _tick(monkeypatch, scraper, *, spy=None, users=None, upsert=None):
    monkeypatch.setattr(pulse_lane, "_due_boards", lambda now, limit: _mine()[:limit])
    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, url: scraper)
    monkeypatch.setattr("app.strategy.hot_lane._active_users", lambda: list(users or []))
    monkeypatch.setattr(pulse_lane, "_watchlist_terms", lambda: set())
    # The shared upsert is not under test: the absence step is.
    monkeypatch.setattr("app.discovery.pipeline._upsert", upsert or (lambda raw, **kw: 0))
    if spy is not None:
        real = pulse_lane._queue_absence

        def _spy(*a, **kw):
            spy.append(1)
            return real(*a, **kw)
        monkeypatch.setattr(pulse_lane, "_queue_absence", _spy)
    return pulse_lane.run_pulse_tick()


def _get(jid: int) -> Job:
    with get_session() as s:
        return s.get(Job, jid)


def _app(jid: int) -> Application:
    with get_session() as s:
        return s.exec(select(Application).where(Application.job_id == jid)).first()


def _liveness(ext: str, source: str = "greenhouse"):
    with get_session() as s:
        return s.exec(select(JobLiveness).where(
            JobLiveness.source == source, JobLiveness.external_id == ext)).first()


def _complete(raw: list, **extra):
    return SimpleNamespace(fetch=lambda: list(raw), fetch_complete=True, **extra)


def _four_on_the_board():
    """Four open shared rows on one Greenhouse board; the fetch lists three."""
    ids = {n: _job(_gh(n), url=f"{_GH}{n}") for n in (1, 2, 3, 4)}
    listed = [_raw(_gh(n), f"{_GH}{n}") for n in (1, 3, 4)]
    return ids, listed


def _listing(source: str, raw: list, meta: dict, *, listed_count=None, board_id=None):
    return board_absence.listing_from_fetch(
        source, raw, meta, listed_count=len(raw) if listed_count is None else listed_count,
        previous_count=None, board_id=board_id)


class _Counter:
    """Pool checkouts and statements while the block runs."""

    def __init__(self):
        self.checkouts = 0
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(engine, "checkout", self._co)
        event.listen(engine, "before_cursor_execute", self._st)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "checkout", self._co)
        event.remove(engine, "before_cursor_execute", self._st)
        return False

    def _co(self, *a):
        self.checkouts += 1

    def _st(self, conn, cursor, statement, *a):
        self.statements.append(" ".join(statement.split()))


# ── 1. the closure, and what it does to copies ────────────────────────────────

def test_complete_fetch_missing_a_posting_closes_shared_row_and_waiting_copies(monkeypatch):
    _board("hasbro")
    ids, listed = _four_on_the_board()
    gone = _gh(2)
    waiting = _job(gone, url=f"{_GH}2", uid="u1", status=ApplicationStatus.SHORTLISTED)
    engaged = _job(gone, url=f"{_GH}2", uid="u2", status=ApplicationStatus.TAILORED)
    queued = _job(gone, url=f"{_GH}2", uid="u3")                     # no application
    errored = _job(gone, url=f"{_GH}2", uid="u4", status=ApplicationStatus.ERROR)
    other = _job(_gh(1), url=f"{_GH}1", uid="u1", status=ApplicationStatus.SHORTLISTED)

    stats = _tick(monkeypatch, _complete(listed))

    shared = _get(ids[2])
    assert shared.is_closed and shared.closed_reason.startswith(
        board_absence.ABSENCE_REASON_PREFIX)
    assert all(not _get(ids[n]).is_closed for n in (1, 3, 4))

    # Waiting applications leave the board as a SYSTEM skip.
    for jid in (waiting, errored):
        assert _get(jid).is_closed
        a = _app(jid)
        assert a.status == ApplicationStatus.SKIPPED
        assert "job closed" in (a.notes or "").lower()
    # Work the user started keeps its place; only the job is marked closed.
    assert _get(engaged).is_closed
    assert _app(engaged).status == ApplicationStatus.TAILORED
    assert _get(queued).is_closed
    # A present posting's copy is untouched.
    assert not _get(other).is_closed
    assert _app(other).status == ApplicationStatus.SHORTLISTED

    lv = _liveness(gone)
    assert lv is not None and lv.state == "REMOVED"
    g = stats["ghost"]
    assert (g["closed"], g["copies_closed"], g["applications_removed"],
            g["kept_engaged"]) == (1, 4, 2, 1)
    # Measured on its own, NOT as a stage of the consume loop: it runs after
    # routing and cannot make a board unconsumed.
    assert "ms" in g
    assert "ghost_close" not in stats.get("consumer_ms", {})


def test_closed_note_reads_as_a_system_skip_for_preference_learning(monkeypatch):
    from app.matching.preference_learning import _is_user_dismissal

    _board("pref")
    _ids, listed = _four_on_the_board()
    copy = _job(_gh(2), url=f"{_GH}2", uid="u1", status=ApplicationStatus.SHORTLISTED)
    _tick(monkeypatch, _complete(listed))
    a = _app(copy)
    assert a.status == ApplicationStatus.SKIPPED
    assert not _is_user_dismissal(a), "a closed posting is not 'not interested'"


def test_a_small_board_may_lose_all_but_one_at_once():
    """"More than a few" is the mass guard's floor: 2 of 3 gone is a small
    board turning over, not a broken listing."""
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(3)]
    res = board_absence.reconcile(
        _listing("greenhouse", [_raw(_gh(0), f"{_GH}0")], {"complete_declared": True}),
        budget=10)
    assert res.outcome == "closed" and res.closed == 2
    assert [_get(i).is_closed for i in ids] == [False, True, True]


# ── 2. a fetch that cannot vouch for the board closes nothing ─────────────────

@pytest.mark.parametrize("scraper_kind", [
    "incomplete", "undeclared", "soft_error", "raises_403", "raises_429",
])
def test_incomplete_or_failed_fetch_closes_nothing(monkeypatch, scraper_kind):
    _board("partial")
    ids, listed = _four_on_the_board()
    if scraper_kind == "incomplete":
        scraper = SimpleNamespace(fetch=lambda: list(listed), fetch_complete=False)
    elif scraper_kind == "undeclared":
        # An adapter that never SAYS it is complete is not trusted to close jobs
        # (the lane's signature default of "complete" is not evidence).
        scraper = SimpleNamespace(fetch=lambda: list(listed))
    elif scraper_kind == "soft_error":
        scraper = SimpleNamespace(fetch=lambda: [], fetch_complete=False,
                                  last_error="HTTPStatusError: 503")
    else:
        code = "403" if scraper_kind == "raises_403" else "429"

        def _boom():
            raise RuntimeError(f"Client error '{code} Forbidden'")
        scraper = SimpleNamespace(fetch=_boom, fetch_complete=True)

    _tick(monkeypatch, scraper)
    assert all(not _get(i).is_closed for i in ids.values())
    assert _liveness(_gh(2)) is None


def test_deferred_board_is_never_reconciled(monkeypatch):
    """A board the tick never consumed was never polled — and never judged."""
    _board("slow")
    ids, listed = _four_on_the_board()

    class _Slow:
        fetch_complete = True

        def fetch(self):
            import time as _t
            _t.sleep(1.5)
            return list(listed)

    monkeypatch.setattr(settings, "pulse_tick_max_seconds", 1)
    calls: list = []
    stats = _tick(monkeypatch, _Slow(), spy=calls)
    assert stats["deferred"] == 1 and stats["fetch_ok"] == 0
    assert calls == []
    assert pulse_lane._ABSENCE_CARRY == {}
    assert all(not _get(i).is_closed for i in ids.values())


# ── 3. this board exactly ─────────────────────────────────────────────────────

_FEDEX_A = "https://fedex.wd1.myworkdayjobs.com/apac_external/job/"
_FEDEX_B = "https://fedex.wd1.myworkdayjobs.com/lac_external/job/"


def test_closing_sources_pin_a_posting_to_one_board():
    """Every closing source has platform-wide ids (never tenant-scoped, so no
    bare id is shared by two employers), and every pulse-polled source is
    either closing or deliberately excluded."""
    from app.discovery.job_identity import TENANT_SCOPED_SOURCES
    from app.discovery.pipeline import scraper_for

    assert not board_absence.CLOSING_SOURCES & TENANT_SCOPED_SOURCES
    excluded = {"workday", "teamtailor", "bamboohr", "workable"}
    assert not board_absence.CLOSING_SOURCES & excluded
    polled = {src for src in JobSource if scraper_for(src, "x", "https://x.wd1.myworkdayjobs.com/y")}
    assert {s.value for s in polled} == board_absence.CLOSING_SOURCES | excluded


@pytest.mark.parametrize("ats,url,company", [
    (JobSource.WORKDAY, f"https://{_P}vfc.wd5.myworkdayjobs.com/vfc_careers/job/x_", "Gab Vfc"),
    (JobSource.TEAMTAILOR, f"https://{_P}acme.teamtailor.com/jobs/", "Gab Acme"),
    (JobSource.BAMBOOHR, f"https://{_P}acme.bamboohr.com/careers/", "Gab Acme"),
    (JobSource.WORKABLE, "https://apply.workable.com/j/", "Gab Reach"),
])
def test_excluded_sources_never_close_on_absence(monkeypatch, ats, url, company):
    """Workday: one requisition on several sites of a tenant, a title edit
    moves the path. Teamtailor: the id carries the title slug. BambooHR:
    tenant-scoped ids. Workable: the URL names no board and display names
    collide across accounts ("Reach" is two). A complete listing of any of
    them closes nothing, holds no census and is never queued."""
    src = ats.value
    bid = _board(f"excl-{src}", ats=ats, job_count=100)
    t = f"{_P}t:" if src in ("workday", "teamtailor", "bamboohr") else _P
    ids = {n: _job(f"{t}{n}", url=f"{url}{_P}{n}", company=company, source=ats)
           for n in range(1, 6)}
    copy = _job(f"{t}1", url=f"{url}{_P}1", uid="u1", company=company, source=ats,
                status=ApplicationStatus.SHORTLISTED)
    listed = [_raw(f"{t}{n}", f"{url}{_P}{n}", company=company, source=src)
              for n in range(2, 6)]
    stats = _tick(monkeypatch, _complete(listed, listing_complete=True))
    assert stats["ghost"]["excluded"] == 1 and stats["ghost"]["queued"] == 0
    assert all(not _get(i).is_closed for i in ids.values())
    assert _app(copy).status == ApplicationStatus.SHORTLISTED
    assert _liveness(f"{t}1", src) is None
    assert _row(bid).job_count == 4, "the census is recorded as before"
    # Even handed straight to the module, it judges nothing.
    listing = board_absence.listing_from_fetch(
        src, listed, {"complete_declared": True, "listing_complete": True},
        listed_count=4, previous_count=5)
    assert listing is None
    direct = board_absence.BoardListing(
        source=src, companies=frozenset({company}),
        present_ids=frozenset(r.external_id for r in listed),
        present_urls=frozenset(board_absence.norm_url(r.url) for r in listed),
        listed_count=4, previous_count=5)
    assert board_absence.reconcile(direct, budget=10).outcome == "excluded"
    assert not _get(ids[1]).is_closed


def test_aggregator_rows_and_other_hosts_are_not_this_boards_to_judge():
    _job(_gh(1), url=f"{_GH}1")
    _job(_gh(2), url=f"{_GH}2")
    agg = _job(_gh(9), url=f"{_GH}9", origin="serpapi")          # filed under the bucket
    elsewhere = _job(_gh(8), url="https://careers.other.example/jobs/8")
    listing = board_absence.BoardListing(
        source="greenhouse", companies=frozenset({_CO}),
        present_ids=frozenset({_gh(1), _gh(2)}),
        present_urls=frozenset({f"{_GH}1", f"{_GH}2"}),
        listed_count=2, previous_count=2)
    res = board_absence.reconcile(listing, budget=10)
    assert res.outcome == "nothing_absent", res
    assert not _get(agg).is_closed and not _get(elsewhere).is_closed


def test_a_board_prefix_is_matched_literally():
    """`_` is a LIKE wildcard: the `gab_co` board must not judge the rows of a
    `gab-co` board that carries the same company name ("Gab Co")."""
    a = "https://job-boards.greenhouse.io/gab_co/jobs/"
    b = "https://job-boards.greenhouse.io/gab-co/jobs/"
    co = "Gab Co"
    for n in range(3):
        _job(_gh(f"a{n}"), url=f"{a}{n}", company=co)
    theirs = _job(_gh("b1"), url=f"{b}1", company=co)
    listing = _listing("greenhouse", [_raw(_gh(f"a{n}"), f"{a}{n}", company=co) for n in range(3)],
                       {"complete_declared": True})
    res = board_absence.reconcile(listing, budget=10)
    assert res.outcome == "nothing_absent", res
    assert not _get(theirs).is_closed


def test_a_copy_under_the_same_id_but_another_company_is_not_this_postings():
    """Copies keep the company verbatim; a per-user row with the same id under
    another company is some other posting and is never touched."""
    ids, listed = _four_on_the_board()
    mine = _job(_gh(2), url=f"{_GH}2", uid="u1", status=ApplicationStatus.SHORTLISTED)
    other = _job(_gh(2), url="https://jobs.lever.co/elsewhere/2", uid="u2",
                 company="Gabsent Elsewhere", status=ApplicationStatus.SHORTLISTED)
    res = board_absence.reconcile(_listing("greenhouse", listed, {"complete_declared": True}),
                                  budget=10)
    assert res.closed == 1 and _get(ids[2]).is_closed
    assert _app(mine).status == ApplicationStatus.SKIPPED
    assert not _get(other).is_closed
    assert _app(other).status == ApplicationStatus.SHORTLISTED


def test_a_row_newer_than_the_fetch_is_not_judged_by_it():
    """The listing waited in the consumer queue while another lane (or an alias
    registry row of the same board) inserted a brand-new posting."""
    for n in range(1, 6):
        _job(_gh(n), url=f"{_GH}{n}", first_seen=datetime.utcnow() - timedelta(hours=1))
    raw = [_raw(_gh(n), f"{_GH}{n}") for n in range(1, 6)]
    fetched = datetime.utcnow() - timedelta(minutes=2)
    listing = board_absence.listing_from_fetch(
        "greenhouse", raw, {"complete_declared": True, "fetched_at": fetched},
        listed_count=5, previous_count=5)
    new = _job(_gh(6), url=f"{_GH}6")                          # inserted after the fetch
    res = board_absence.reconcile(listing, budget=10)
    assert res.closed == 0 and not _get(new).is_closed


def test_the_lane_stamps_each_fetch_with_its_start(monkeypatch):
    """End to end: a row another lane inserts while this board's fetch is in
    flight is not judged by the listing that fetch returns."""
    _board("race")
    ids, listed = _four_on_the_board()
    born: list = []

    def _fetch():
        born.append(_job(_gh(9), url=f"{_GH}9"))               # another lane, mid-fetch
        return list(listed) + [_raw(_gh(2), f"{_GH}2")]
    stats = _tick(monkeypatch, SimpleNamespace(fetch=_fetch, fetch_complete=True))
    assert stats["ghost"]["boards"] == 1
    assert not _get(born[0]).is_closed
    assert all(not _get(i).is_closed for i in ids.values())


# ── 4. the doubt guard ───────────────────────────────────────────────────────

def test_mass_disappearance_closes_nothing(monkeypatch):
    # The census agrees with the listing (2): the pool's 8 extra rows are what
    # would close — far more than half of what it holds.
    _board("mass", job_count=2)
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    listed = [_raw(_gh(n), f"{_GH}{n}") for n in range(2)]     # 8 of 10 gone
    stats = _tick(monkeypatch, _complete(listed))
    assert all(not _get(i).is_closed for i in ids)
    assert stats["ghost"]["doubt_mass"] == 1 and stats["ghost"]["closed"] == 0


def _doubt_listing(n_listed, board_id=987654):
    return board_absence.listing_from_fetch(
        "greenhouse", [_raw(_gh(n), f"{_GH}{n}") for n in range(n_listed)],
        {"complete_declared": True}, listed_count=n_listed, previous_count=n_listed,
        board_id=board_id)


def _job_reads(c) -> list:
    return [st for st in c.statements if "FROM job" in st]


def test_doubt_is_decided_from_one_count_before_any_row_is_read():
    """A board holding far more unlisted rows than it lists (a Lever board at
    4.5k listed held ~21k open shared rows) used to read every one of them,
    URLs included, to conclude it was in doubt. One aggregate answers it."""
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(260)]
    with _Counter() as c:
        res = board_absence.reconcile(_doubt_listing(1), budget=10)
    assert res.outcome == "doubt_mass" and res.closed == 0
    reads = _job_reads(c)
    assert len(reads) == 1, reads
    assert "sum(" in reads[0].lower(), "an aggregate, not a row read"
    assert all(not _get(i).is_closed for i in ids[:5])


def test_a_doubt_verdict_holds_until_a_listing_could_resolve_it():
    """A board in doubt used to be read in full on every changed poll, and the
    big ones change on every poll (a Lever board listing 4.5k postings over
    ~21.7k pool rows: one census reads ~18k blocks, 10.6-11.5 s). The verdict
    holds while no new listing could have resolved it: with 8 absent of 10, a
    listing of 3 still leaves ≥4 absent against ≤3 listed."""
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    assert board_absence.reconcile(_doubt_listing(2), budget=10).outcome == "doubt_mass"
    for n in (2, 3):
        with _Counter() as c:
            assert board_absence.reconcile(_doubt_listing(n), budget=10).outcome == "doubt_memo"
        assert not _job_reads(c), "the verdict cannot have changed: not read again"
    # A listing that grew enough to resolve it is read again…
    with _Counter() as c:
        assert board_absence.reconcile(_doubt_listing(4), budget=10).outcome == "doubt_mass"
    assert len(_job_reads(c)) == 1
    assert all(not _get(i).is_closed for i in ids)
    # …and one that resolves it closes.
    res = board_absence.reconcile(_doubt_listing(5), budget=10)
    assert res.outcome == "closed" and res.closed == 5
    # The memo has a ceiling whatever the counts.
    _cleanup()
    [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    assert board_absence.reconcile(_doubt_listing(2), budget=10).outcome == "doubt_mass"
    with board_absence._doubt_lock:
        _until, absent = board_absence._DOUBT_MEMO[987654]
        board_absence._DOUBT_MEMO[987654] = (0.0, absent)
    assert board_absence.reconcile(_doubt_listing(2), budget=10).outcome == "doubt_mass"


def test_a_census_that_hits_the_statement_ceiling_is_left_alone_for_hours(monkeypatch):
    _job(_gh(1), url=f"{_GH}1")

    def _timeout(*a, **k):
        raise RuntimeError("canceling statement due to statement timeout")
    monkeypatch.setattr(board_absence, "_board_rows", _timeout)
    assert board_absence.reconcile(_doubt_listing(1), budget=10).outcome == "error"
    until, absent = board_absence._DOUBT_MEMO[987654]
    assert absent is None
    assert until - __import__("time").monotonic() > 3600, "a timeout is remembered like a doubt"
    assert board_absence.reconcile(_doubt_listing(7), budget=10).outcome == "doubt_memo"


def test_a_board_in_doubt_still_reopens_what_it_lists_again():
    """Presence is positive evidence even when the absence side is in doubt."""
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    back = _job(_gh("b"), url=f"{_GH}b", closed=True,
                reason=board_absence.closed_reason_for("greenhouse"))
    raw = [_raw(_gh(n), f"{_GH}{n}") for n in range(2)] + [_raw(_gh("b"), f"{_GH}b")]
    res = board_absence.reconcile(_listing("greenhouse", raw, {"complete_declared": True}),
                                  budget=10)
    assert res.outcome == "doubt_mass" and res.closed == 0 and res.reopened == 1
    assert not _get(back).is_closed
    assert all(not _get(i).is_closed for i in ids)


def test_empty_listing_closes_nothing_however_few_rows(monkeypatch):
    _board("empty", job_count=2)
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(2)]
    stats = _tick(monkeypatch, _complete([]))
    assert all(not _get(i).is_closed for i in ids)
    assert stats["ghost"]["doubt_empty"] == 1


def test_collapsed_listing_closes_nothing_and_keeps_the_census(monkeypatch):
    """The pool holds only some of a big board (the tech titles), so a board
    that suddenly lists a fraction of itself can lose few POOL rows while most
    of its listing vanished — the share guard alone would not see it."""
    bid = _board("collapse", job_count=100)
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    listed = [_raw(_gh(n), f"{_GH}{n}") for n in range(8)]     # 2 of 10 gone…
    stats = _tick(monkeypatch, _complete(listed))               # …but 100 → 8 listed
    assert all(not _get(i).is_closed for i in ids)
    assert stats["ghost"]["doubt_collapse"] == 1
    assert _row(bid).job_count == 100, "a doubted poll must not move the baseline"


def test_collapse_is_doubted_until_it_repeats_on_consecutive_polls(monkeypatch):
    """The guard used to work ONCE: the doubted poll wrote its shrunken count
    as the census, and the next poll — compared against it — closed."""
    bid = _board("collapse2", job_count=100)
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    listed = [_raw(_gh(n), f"{_GH}{n}") for n in range(8)]
    _tick(monkeypatch, _complete(listed))
    # The same collapsed listing plus one new posting (signature changes).
    listed2 = listed + [_raw(_gh(50), f"{_GH}50")]
    s2 = _tick(monkeypatch, _complete(listed2))
    assert s2["ghost"]["doubt_collapse"] == 1 and s2["ghost"]["closed"] == 0
    assert all(not _get(i).is_closed for i in ids)
    assert _row(bid).job_count == 100
    # A third consecutive complete poll that still says so is believed.
    assert settings.pulse_ghost_close_confirm_polls == 3
    s3 = _tick(monkeypatch, _complete(listed2 + [_raw(_gh(51), f"{_GH}51")]))
    assert s3["ghost"]["closed"] == 2
    assert [_get(i).is_closed for i in ids] == [False] * 8 + [True, True]
    assert _row(bid).job_count == 10


def test_an_unchanged_repeat_of_a_collapse_does_not_move_the_census(monkeypatch):
    bid = _board("collapse3", job_count=100)
    [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    scraper = _complete([_raw(_gh(n), f"{_GH}{n}") for n in range(8)])
    _tick(monkeypatch, scraper)
    s2 = _tick(monkeypatch, scraper)
    assert s2["unchanged"] == 1
    assert _row(bid).job_count == 100


def test_empty_then_partial_listing_closes_nothing(monkeypatch):
    """An empty listing used to write job_count=0, and a zero census disabled
    the collapse check on the next poll."""
    bid = _board("emptythen", job_count=100)
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    s1 = _tick(monkeypatch, _complete([]))
    assert s1["ghost"]["doubt_collapse"] == 1
    assert _row(bid).job_count == 100
    s2 = _tick(monkeypatch, _complete([_raw(_gh(n), f"{_GH}{n}") for n in range(8)]))
    assert s2["ghost"]["closed"] == 0
    assert all(not _get(i).is_closed for i in ids)


def test_no_census_means_no_closing_this_poll(monkeypatch):
    bid = _board("fresh", job_count=0)
    ids, listed = _four_on_the_board()
    stats = _tick(monkeypatch, _complete(listed))
    assert stats["ghost"]["doubt_baseline"] == 1
    assert all(not _get(i).is_closed for i in ids.values())
    assert _row(bid).job_count == 3


# ── 5. one budget per tick, the rest carried ─────────────────────────────────

def test_per_tick_cap_carries_the_rest_without_repolling(monkeypatch):
    monkeypatch.setattr(settings, "pulse_ghost_close_max_per_tick", 2)
    bid = _board("cap", job_count=10)
    ids = [_job(_gh(n), url=f"{_GH}{n}") for n in range(10)]
    listed = [_raw(_gh(n), f"{_GH}{n}") for n in range(3, 10)]   # 0,1,2 gone
    scraper = _complete(listed)

    first = _tick(monkeypatch, scraper)
    assert first["ghost"]["closed"] == 2 and first["ghost"]["pending"] == 1
    assert [_get(i).is_closed for i in ids[:3]] == [True, True, False]
    assert bid in pulse_lane._ABSENCE_CARRY
    # The schedule is the board's own: nothing is pulled forward.
    assert _row(bid).next_poll_at > datetime.utcnow() + timedelta(
        minutes=settings.pulse_deferred_retry_minutes + 5)

    # Next tick: the board reads as unchanged; the carried listing finishes.
    second = _tick(monkeypatch, scraper)
    assert second["unchanged"] == 1
    assert second["ghost"]["closed"] == 1
    assert _get(ids[2]).is_closed
    assert bid not in pulse_lane._ABSENCE_CARRY
    assert all(not _get(i).is_closed for i in ids[3:])


def test_a_spent_budget_never_holds_a_board_that_owes_nothing(monkeypatch):
    """With the tick's budget spent, a board whose listing names every row it
    holds used to be marked pending and re-polled in 2 minutes — over and over
    during the first-run backlog. It owes nothing: normal cadence, not held."""
    monkeypatch.setattr(settings, "pulse_ghost_close_max_per_tick", 0)
    bid = _board("owes-nothing")
    ids = {n: _job(_gh(n), url=f"{_GH}{n}") for n in (1, 2, 3, 4)}
    stats = _tick(monkeypatch, _complete([_raw(_gh(n), f"{_GH}{n}") for n in (1, 2, 3, 4)]))
    assert _row(bid).next_poll_at > datetime.utcnow() + timedelta(
        minutes=settings.pulse_deferred_retry_minutes + 5)
    g = stats["ghost"]
    assert g["budget_spent"] == 0 and g["pending"] == 0 and g["carried"] == 0
    assert bid not in pulse_lane._ABSENCE_CARRY
    assert all(not _get(i).is_closed for i in ids.values())


def test_budget_is_shared_and_a_spent_budget_still_reopens():
    """The budget is shared across boards in order. A board judged after it
    ran out still has its reopenings applied, and owes closures only when it
    has absent rows (pending)."""
    for n in range(3):
        _job(_gh(n), url=f"{_GH}{n}")
    other = "https://job-boards.greenhouse.io/otherco/jobs/"
    for n in range(3):
        _job(_gh(f"o{n}"), url=f"{other}{n}", company="Gabsent Other")
    reason = board_absence.closed_reason_for("greenhouse")
    third = "https://job-boards.greenhouse.io/thirdco/jobs/"
    back = _job(_gh("t1"), url=f"{third}1", company="Gabsent Third", closed=True, reason=reason)
    a = _listing("greenhouse", [_raw(_gh(n), f"{_GH}{n}") for n in (1, 2)],
                 {"complete_declared": True}, board_id=1)
    b = _listing("greenhouse", [_raw(_gh(f"o{n}"), f"{other}{n}", company="Gabsent Other")
                                for n in (1, 2)], {"complete_declared": True}, board_id=2)
    c = _listing("greenhouse", [_raw(_gh("t1"), f"{third}1", company="Gabsent Third")],
                 {"complete_declared": True}, board_id=3)
    results, totals = board_absence.reconcile_many([a, b, c], budget=1)
    assert [r.outcome for r in results] == ["closed", "budget_spent", "reopened"]
    assert totals.closed == 1 and results[1].pending == 1 and results[2].pending == 0
    assert not _get(back).is_closed


def test_the_step_runs_after_routing_and_a_spent_slice_carries(monkeypatch):
    """No slice left (here: a zero-second slice) means nothing is read and the
    listing waits for the next tick — never a reason to hold up routing."""
    monkeypatch.setattr(settings, "pulse_ghost_close_max_seconds", 0)
    bid = _board("slice")
    ids, listed = _four_on_the_board()
    order: list = []
    real = board_absence.reconcile_many
    monkeypatch.setattr(board_absence, "reconcile_many",
                        lambda *a, **k: (order.append("absence"), real(*a, **k))[1])

    def _upsert(raw, **kw):
        order.append("upsert:" + str(kw.get("user_id")))
        return 0
    users = [{"user_id": "gab-user", "roles": ["engineer"]}]
    s1 = _tick(monkeypatch, _complete(listed), users=users, upsert=_upsert)
    assert s1["ghost"]["deferred"] == 1 and s1["ghost"]["closed"] == 0
    assert not _get(ids[2]).is_closed and bid in pulse_lane._ABSENCE_CARRY
    assert order == ["upsert:" + SHARED_POOL_USER, "upsert:gab-user"]

    monkeypatch.setattr(settings, "pulse_ghost_close_max_seconds", 10)
    s2 = _tick(monkeypatch, _complete(listed), users=users, upsert=_upsert)
    assert s2["unchanged"] == 1 and s2["ghost"]["closed"] == 1
    assert _get(ids[2]).is_closed
    assert order[-1] == "absence"


def test_one_session_for_every_board_of_a_tick():
    """Each board used to cost up to four pooled sessions (rows, URLs, close,
    liveness). Now: one session for all of them, and a steady board is one
    statement."""
    listings = []
    for b in range(5):
        url = f"https://job-boards.greenhouse.io/gabco{b}/jobs/"
        co = f"Gabsent Co {b}"
        for n in range(4):
            _job(_gh(f"{b}-{n}"), url=f"{url}{n}", company=co,
                 first_seen=datetime.utcnow() - timedelta(hours=1))
        _job(_gh(f"{b}-1"), url=f"{url}1", company=co, uid="u1",
             status=ApplicationStatus.SHORTLISTED)
        listings.append(_listing("greenhouse", [_raw(_gh(f"{b}-{n}"), f"{url}{n}", company=co)
                                                for n in (0, 2, 3)],
                                 {"complete_declared": True}))
    with _Counter() as c:
        _results, totals = board_absence.reconcile_many(listings, budget=100)
    assert totals.closed == 5 and totals.applications_removed == 5
    assert c.checkouts == 1, c.checkouts

    steady = _listing("greenhouse", [_raw(_gh(f"0-{n}"), f"https://job-boards.greenhouse.io/gabco0/jobs/{n}",
                                          company="Gabsent Co 0") for n in (0, 2, 3)],
                      {"complete_declared": True})
    with _Counter() as c:
        board_absence.reconcile_many([steady], budget=100)
    assert c.checkouts == 1
    assert len([st for st in c.statements if st.startswith("SELECT")]) == 1


def test_one_board_losing_one_posting_is_one_session():
    """The reviewer counted 4 pooled sessions (rows, URLs, close, liveness) —
    ~16 round trips — for one departed posting on the pulse consumer."""
    _ids, listed = _four_on_the_board()
    _job(_gh(2), url=f"{_GH}2", uid="u1", status=ApplicationStatus.SHORTLISTED)
    with _Counter() as c:
        res = board_absence.reconcile(_listing("greenhouse", listed, {"complete_declared": True}),
                                      budget=10)
    assert res.closed == 1 and res.copies_closed == 1
    assert c.checkouts == 1, c.checkouts
    assert _liveness(_gh(2)).state == "REMOVED"


def test_liveness_is_a_savepoint_that_cannot_stop_a_close(monkeypatch):
    from app.discovery import liveness

    ids, listed = _four_on_the_board()
    with _Counter() as c:
        board_absence.reconcile(_listing("greenhouse", listed, {"complete_declared": True}),
                                budget=10)
    sp = next(i for i, st in enumerate(c.statements) if st.startswith("SAVEPOINT"))
    ins = next(i for i, st in enumerate(c.statements) if st.startswith("INSERT INTO job_liveness"))
    assert sp < ins
    _cleanup()
    ids, listed = _four_on_the_board()

    def _boom(*a, **k):
        raise RuntimeError("liveness write failed")
    monkeypatch.setattr(liveness, "record_board_states", _boom)
    res = board_absence.reconcile(_listing("greenhouse", listed, {"complete_declared": True}),
                                  budget=10)
    assert res.outcome == "closed" and _get(ids[2]).is_closed
    assert _liveness(_gh(2)) is None


def test_job_rows_are_locked_before_any_application_moves():
    """slate.place locks the job row, then moves applications. Closing several
    postings in one transaction must take the same order, or a placement and
    a closure deadlock."""
    _ids, listed = _four_on_the_board()
    copy = _job(_gh(2), url=f"{_GH}2", uid="u1", status=ApplicationStatus.SHORTLISTED)
    with _Counter() as c:
        res = board_absence.reconcile(_listing("greenhouse", listed, {"complete_declared": True}),
                                      budget=10)
    assert res.closed == 1 and _app(copy).status == ApplicationStatus.SKIPPED
    lock = next(i for i, st in enumerate(c.statements)
                if st.startswith("SELECT job.id FROM job WHERE job.id IN") and "ORDER BY job.id" in st)
    first_write = next(i for i, st in enumerate(c.statements)
                       if st.startswith(("UPDATE application", "UPDATE job")))
    assert lock < first_write
    # SQLite has no row locks; on Postgres the same statement is FOR UPDATE.
    from sqlalchemy.dialects import postgresql
    sql = str(board_absence.lock_statements([9, 3, 5])[0].compile(dialect=postgresql.dialect()))
    assert "ORDER BY job.id" in sql and sql.rstrip().endswith("FOR UPDATE")


# ── 6. a false closure heals ─────────────────────────────────────────────────

def test_reappearance_reopens_the_shared_row_and_unheld_copies(monkeypatch):
    _board("back")
    reason = board_absence.closed_reason_for("greenhouse")
    keep = [_job(_gh(n), url=f"{_GH}{n}") for n in (1, 3)]
    back = _job(_gh(2), url=f"{_GH}2", closed=True, reason=reason)
    legacy = _job(_gh(4), url=f"{_GH}4", closed=True,
                  reason="Removed from company greenhouse ATS board")   # mark_ghost_jobs
    aged = _job(_gh(5), url=f"{_GH}5", closed=True,
                reason="shared-pool retention (45d)")                    # not ours
    queued = _job(_gh(2), url=f"{_GH}2", uid="u1", closed=True, reason=reason)
    removed = _job(_gh(2), url=f"{_GH}2", uid="u2", closed=True, reason=reason,
                   status=ApplicationStatus.SKIPPED)
    with get_session() as s:
        s.add(JobLiveness(source="greenhouse", external_id=_gh(2), state="REMOVED",
                          reason="absent_from_complete_board_fetch",
                          checked_at=datetime.utcnow()))
        s.commit()

    listed = [_raw(_gh(n), f"{_GH}{n}") for n in (1, 2, 3, 4, 5)]
    stats = _tick(monkeypatch, _complete(listed))

    for jid in (back, legacy, queued):
        j = _get(jid)
        assert not j.is_closed and j.closed_reason is None
    assert _get(aged).is_closed, "an age expiry is not board evidence to undo"
    # A copy whose application went to Removed stays there: restoring it would
    # write a SHORTLISTED application outside slate.place().
    assert _get(removed).is_closed
    assert _app(removed).status == ApplicationStatus.SKIPPED
    assert _liveness(_gh(2)).state == "LIVE"
    assert stats["ghost"]["reopened"] == 2 and stats["ghost"]["copies_reopened"] == 1
    assert all(not _get(i).is_closed for i in keep)


# ── 7. the switch ────────────────────────────────────────────────────────────

def test_setting_off_changes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "pulse_ghost_close_enabled", False)
    bid = _board("off", job_count=100)
    ids, listed = _four_on_the_board()
    copy = _job(_gh(2), url=f"{_GH}2", uid="u1", status=ApplicationStatus.SHORTLISTED)
    calls: list = []
    stats = _tick(monkeypatch, _complete(listed), spy=calls)
    assert calls == []
    assert "ghost" not in stats
    assert all(not _get(i).is_closed for i in ids.values())
    assert _app(copy).status == ApplicationStatus.SHORTLISTED
    assert _liveness(_gh(2)) is None
    # The census is recorded exactly as before (no collapse hold).
    assert _row(bid).job_count == 3
    assert pulse_lane._ABSENCE_CARRY == {} and pulse_lane._COLLAPSE_STREAK == {}


# ── 8. the full pass's mark_ghost_jobs ───────────────────────────────────────

def test_mark_ghost_jobs_with_urls_spares_another_workday_site():
    from app.discovery.pipeline import mark_ghost_jobs

    wd = JobSource.WORKDAY
    a_present = _job(f"fedex:{_P}A1", url=f"{_FEDEX_A}{_P}A1", company="Fedex", source=wd)
    a_gone = _job(f"fedex:{_P}A2", url=f"{_FEDEX_A}{_P}A2", company="Fedex", source=wd)
    b_row = _job(f"fedex:{_P}B1", url=f"{_FEDEX_B}{_P}B1", company="Fedex", source=wd)
    mark_ghost_jobs("workday", "Fedex", [f"fedex:{_P}A1"], user_id=SHARED_POOL_USER,
                    present_urls=[f"{_FEDEX_A}{_P}A1"])
    assert not _get(a_present).is_closed
    assert _get(a_gone).is_closed
    assert not _get(b_row).is_closed, "site B is a different board"


def test_mark_ghost_jobs_never_records_a_verdict_under_a_bare_id():
    from app.discovery.pipeline import mark_ghost_jobs

    wd = JobSource.WORKDAY
    _job(f"fedex:{_P}A1", url=f"{_FEDEX_A}x_{_P}A1", company="Fedex", source=wd)
    bare = _job(f"{_P}A9", url=f"{_FEDEX_A}y_{_P}A9", company="Fedex", source=wd)
    scoped = _job(f"fedex:{_P}A8", url=f"{_FEDEX_A}z_{_P}A8", company="Fedex", source=wd)
    mark_ghost_jobs("workday", "Fedex", [f"fedex:{_P}A1"], user_id=SHARED_POOL_USER,
                    present_urls=[f"{_FEDEX_A}x_{_P}A1"])
    assert _get(bare).is_closed and _get(scoped).is_closed
    assert _liveness(f"{_P}A9", "workday") is None
    assert _liveness(f"fedex:{_P}A8", "workday").state == "REMOVED"


def test_mark_ghost_jobs_closes_in_ascending_id_order():
    """The per-company read comes back in title order (its index). Closing in
    that order locked rows in a different order than board absence and
    slate.place, which is how two closers deadlock."""
    from app.discovery.pipeline import mark_ghost_jobs

    _job(_gh("keep"), url=f"{_GH}keep")
    zs = [_job(_gh(f"z{n}"), url=f"{_GH}z{n}") for n in range(3)]   # lower ids, later titles
    a_s = [_job(_gh(f"a{n}"), url=f"{_GH}a{n}") for n in range(3)]
    seen: list = []

    def _st(conn, cursor, statement, params, *a):
        if statement.lstrip().startswith("SELECT") and "WHERE job.id = " in statement:
            seen.append(params[0] if isinstance(params, (list, tuple)) else params)
    event.listen(engine, "before_cursor_execute", _st)
    try:
        mark_ghost_jobs("greenhouse", _CO, [_gh("keep")], user_id=SHARED_POOL_USER,
                        present_urls=[f"{_GH}keep"])
    finally:
        event.remove(engine, "before_cursor_execute", _st)
    assert all(_get(i).is_closed for i in zs + a_s)
    assert seen == sorted(zs + a_s), seen


def test_full_pass_counts_every_listed_posting_as_present():
    """SmartRecruiters skips non-tech titles: the full pass must count the
    LISTING's ids (and URLs) as present, not only the parsed postings."""
    from app.discovery.pipeline import _close_ghosts_after_fetch

    sr = JobSource.SMARTRECRUITERS
    base = "https://jobs.smartrecruiters.com/gabsentco/"
    parsed = _job(f"{_P}sr1", url=f"{base}{_P}sr1", source=sr)
    skipped = _job(f"{_P}sr2", url=f"{base}{_P}sr2", source=sr)       # a non-tech title
    by_url = _job(f"{_P}sr3", url=f"{base}{_P}sr3", source=sr)        # listed by URL only
    gone = _job(f"{_P}sr4", url=f"{base}{_P}sr4", source=sr)
    other_board = _job(f"{_P}sr5", url=f"https://jobs.smartrecruiters.com/gabsentother/{_P}sr5",
                       source=sr)
    scraper = SimpleNamespace(name="smartrecruiters", fetch_complete=True,
                              listed_ids={f"{_P}sr1", f"{_P}sr2"},
                              listed_urls={f"{base}{_P}sr3"})
    _close_ghosts_after_fetch(scraper, [_raw(f"{_P}sr1", f"{base}{_P}sr1",
                                             source="smartrecruiters")], SHARED_POOL_USER)
    assert [_get(i).is_closed for i in (parsed, skipped, by_url, gone, other_board)] == \
        [False, False, False, True, False]
    # A partial fetch closes nothing.
    _cleanup()
    g2 = _job(f"{_P}sr4", url=f"{base}{_P}sr4", source=sr)
    _close_ghosts_after_fetch(SimpleNamespace(name="smartrecruiters", fetch_complete=False),
                              [_raw(f"{_P}sr1", f"{base}{_P}sr1", source="smartrecruiters")],
                              SHARED_POOL_USER)
    assert not _get(g2).is_closed


# ── 9. adapters only say "complete" when they can prove it ───────────────────

def test_listing_truncated_reads_envelopes():
    from app.discovery.base import listing_truncated
    assert listing_truncated({"jobs": [1], "meta": {"total": 2}}, 1)
    assert listing_truncated({"items": [1], "links": {"next": "/p2"}}, 1)
    assert listing_truncated({"data": [], "pagination": {"hasMore": True}}, 0)
    assert not listing_truncated({"jobs": [1, 2], "meta": {"total": 2}}, 2)
    assert not listing_truncated([1, 2, 3], 3)


def _resp(payload=None, status=200, text=None, content=None):
    import httpx
    req = httpx.Request("GET", "https://example.test/")
    if payload is not None:
        return httpx.Response(status, json=payload, request=req)
    return httpx.Response(status, text=text or "", request=req) if content is None \
        else httpx.Response(status, content=content, request=req)


def test_greenhouse_completeness(monkeypatch):
    from app.discovery import greenhouse
    job = {"id": 1, "title": "Engineer", "location": {"name": "NYC"},
           "absolute_url": "https://job-boards.greenhouse.io/x/jobs/1", "content": ""}
    s = greenhouse.GreenhouseScraper("x")
    monkeypatch.setattr(greenhouse.httpx, "get",
                        lambda *a, **k: _resp({"jobs": [job], "meta": {"total": 1}}))
    s.fetch()
    assert s.fetch_complete is True
    monkeypatch.setattr(greenhouse.httpx, "get",
                        lambda *a, **k: _resp({"jobs": [job], "meta": {"total": 7}}))
    s.fetch()
    assert s.fetch_complete is False
    monkeypatch.setattr(greenhouse.httpx, "get", lambda *a, **k: _resp({}, status=429))
    assert s.fetch() == [] and s.fetch_complete is False


@pytest.mark.parametrize("module,cls,payload", [
    ("workable", "WorkableScraper", {"name": "X", "jobs": []}),
    ("recruitee", "RecruiteeScraper", {"offers": []}),
    ("rippling", "RipplingScraper", []),
    ("breezy", "BreezyScraper", []),
    ("pinpoint", "PinpointScraper", {"data": []}),
    ("ashby", "AshbyScraper", {"jobs": []}),
])
def test_long_tail_adapters_declare_complete_only_on_a_whole_answer(
        monkeypatch, module, cls, payload):
    import importlib
    mod = importlib.import_module(f"app.discovery.{module}")
    s = getattr(mod, cls)("x")
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: _resp(payload))
    s.fetch()
    assert s.fetch_complete is True
    # A throttled/failed answer that the adapter swallows into [] is NOT whole.
    monkeypatch.setattr(mod.httpx, "get", lambda *a, **k: _resp({}, status=503))
    assert s.fetch() == []
    assert s.fetch_complete is False


def test_teamtailor_feed_at_its_cap_is_partial(monkeypatch):
    from app.discovery import teamtailor

    def feed(n):
        items = "".join(
            f"<item><title>Engineer {i}</title><link>https://x.teamtailor.com/jobs/{i}-e</link></item>"
            for i in range(n))
        return f"<rss><channel>{items}</channel></rss>".encode()
    s = teamtailor.TeamtailorScraper("x")
    monkeypatch.setattr(teamtailor.httpx, "get", lambda *a, **k: _resp(content=feed(3)))
    s.fetch()
    assert s.fetch_complete is True
    monkeypatch.setattr(teamtailor.httpx, "get",
                        lambda *a, **k: _resp(content=feed(teamtailor._FEED_CAP)))
    s.fetch()
    assert s.fetch_complete is False
    assert s.signature_stable is True, "the cut is deterministic"


def test_bamboohr_cut_board_is_partial(monkeypatch):
    from app.discovery import bamboohr

    def page(n):
        lis = "".join(
            f'<li class="BambooHR-ATS-Jobs-Item" id="bhrPositionID_{i}">'
            f'<a href="//x.bamboohr.com/careers/{i}">Engineer {i}</a></li>' for i in range(n))
        return f"<ul>{lis}</ul>"

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _resp({}, status=404)

    monkeypatch.setattr(bamboohr.httpx, "Client", _Client)
    s = bamboohr.BambooHRScraper("x")
    monkeypatch.setattr(bamboohr.httpx, "get", lambda *a, **k: _resp(text=page(2)))
    s.fetch()
    assert s.fetch_complete is True
    monkeypatch.setattr(bamboohr.httpx, "get",
                        lambda *a, **k: _resp(text=page(bamboohr._MAX_JOBS + 1)))
    assert len(s.fetch()) == bamboohr._MAX_JOBS
    assert s.fetch_complete is False


def test_smartrecruiters_lists_every_id_and_needs_a_stated_total(monkeypatch):
    from app.discovery import smartrecruiters as sr
    listing = {"content": [{"id": "1", "name": "Backend Engineer", "location": {}},
                           {"id": "2", "name": "Recruiter", "location": {}}]}

    def fake_get(url, **kw):
        if url.endswith("/postings"):
            return SimpleNamespace(status_code=200, json=lambda: listing)
        return SimpleNamespace(status_code=200, json=lambda: {"jobAd": {"sections": {}}})

    monkeypatch.setattr(sr.httpx, "get", fake_get)
    s = sr.SmartRecruitersScraper("x")
    s.fetch()
    assert s.listed_ids == {"1", "2"}, "the skipped non-tech posting is still LISTED"
    assert s.fetch_complete is False and s.listing_complete is False, (
        "no totalFound: the size of the board was never stated")
    listing["totalFound"] = 2
    s.fetch()
    assert s.fetch_complete is True and s.listing_complete is True


class _WorkdayFake:
    """A Workday CXS tenant: `pages` is the list of page sizes, `totals` the
    `total` each page reports. Counts listing POSTs and detail GETs."""

    def __init__(self, pages, totals, *, fail_detail=()):
        self.pages, self.totals, self.fail = pages, totals, set(fail_detail)
        self.posts = self.gets = 0

    def post(self, url, json=None, **kw):
        self.posts += 1
        i = json["offset"] // 20
        n = self.pages[i] if i < len(self.pages) else 0
        rows = [{"title": ("Sales Rep" if (i == 0 and k == 0) else f"Engineer {i}-{k}"),
                 "externalPath": f"/job/Loc/Engineer-{i}-{k}_R{i}{k:02d}"} for k in range(n)]
        total = self.totals[i] if i < len(self.totals) else 0
        return SimpleNamespace(status_code=200, json=lambda: {"jobPostings": rows, "total": total})

    def get(self, url, **kw):
        self.gets += 1
        req = url.rsplit("_", 1)[-1]
        if req in self.fail:
            return SimpleNamespace(status_code=500, json=lambda: {})
        return SimpleNamespace(status_code=200, json=lambda: {"jobPostingInfo": {
            "jobReqId": req, "jobDescription": "d"}})


def _workday(monkeypatch, fake):
    from app.discovery import workday
    monkeypatch.setattr(workday.httpx, "post", fake.post)
    monkeypatch.setattr(workday.httpx, "get", fake.get)
    s = workday.WorkdayScraper("acme", "https://acme.wd1.myworkdayjobs.com/External")
    return s, s.fetch()


def test_workday_board_with_total_on_page_one_only_keeps_its_walk_and_is_partial(monkeypatch):
    """Most tenants report `total` on the first page only. The walk keeps its
    old depth (two pages — walking a 250-posting board to the end would cost
    ~2.5x the requests on every poll), but the result no longer CLAIMS to be
    the whole board."""
    fake = _WorkdayFake(pages=[20] * 13, totals=[250])
    s, _jobs = _workday(monkeypatch, fake)
    assert fake.posts == 2 and fake.gets <= 40
    assert s.listing_complete is False and s.fetch_complete is False


def test_workday_board_inside_two_pages_is_whole_and_lists_skipped_titles(monkeypatch):
    fake = _WorkdayFake(pages=[20, 15], totals=[35])
    s, jobs = _workday(monkeypatch, fake)
    assert len(s.listed_urls) == 35, "skipped titles included"
    assert any(u.endswith("_R000") for u in s.listed_urls)        # the Sales Rep
    assert len(jobs) == 34
    assert s.listing_complete is True and s.fetch_complete is True


def test_workday_short_middle_page_is_partial(monkeypatch):
    """Offsets can reach the stated total while a page came back short (the
    board changed mid-walk): the listing named fewer postings than stated."""
    fake = _WorkdayFake(pages=[20, 15, 5], totals=[45, 45, 45])
    s, _jobs = _workday(monkeypatch, fake)
    assert fake.posts == 3
    assert len(s.listed_urls) == 40
    assert s.listing_complete is False and s.fetch_complete is False


def test_workday_failed_detail_is_listed_but_not_complete(monkeypatch):
    fake = _WorkdayFake(pages=[5], totals=[5], fail_detail={"R003"})
    s, jobs = _workday(monkeypatch, fake)
    assert s.listing_complete is True and s.fetch_complete is False
    assert len(jobs) == 3                                         # Sales Rep skipped, R003 failed
    assert any(u.endswith("_R003") for u in s.listed_urls)


def test_workday_unknown_total_with_a_full_page_is_partial(monkeypatch):
    from app.discovery import workday
    monkeypatch.setattr(workday.httpx, "post", lambda url, json=None, **kw: SimpleNamespace(
        status_code=200, json=lambda: {"jobPostings": [
            {"title": f"Engineer {i}", "externalPath": f"/job/q{i}"} for i in range(20)]}))
    monkeypatch.setattr(workday.httpx, "get", lambda url, **kw: SimpleNamespace(
        status_code=200, json=lambda: {"jobPostingInfo": {"jobReqId": "r", "jobDescription": "d"}}))
    s = workday.WorkdayScraper("acme", "https://acme.wd1.myworkdayjobs.com/External")
    s.fetch()
    assert s.listing_complete is False and s.fetch_complete is False


def test_join_without_a_page_count_trusts_only_a_short_page(monkeypatch):
    from app.discovery import join
    join._reset_learned_shape()
    monkeypatch.setattr(join, "_configured_page_size", lambda: 5)
    full = {"items": [{"id": str(i), "name": f"Engineer {i}"} for i in range(5)],
            "pagination": {"weird": 1}}

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def get(self, url, params=None):
            if "/companies/" in url and "/jobs" not in url:
                return _resp(text='"id":"77","domain":"x"')
            return _resp(full)

    monkeypatch.setattr(join, "_COMPANY_RE", __import__("re").compile(r'"id":"(\d+)","domain":"([^"]+)"'))
    monkeypatch.setattr(join.httpx, "Client", _Client)
    s = join.JoinScraper("x")
    s.fetch()
    assert s.fetch_complete is False
    full["items"] = full["items"][:3]
    s = join.JoinScraper("x")
    s.fetch()
    assert s.fetch_complete is True
    join._reset_learned_shape()


def test_record_board_states_is_one_upsert():
    """One INSERT … ON CONFLICT per source, overwriting what a row said — no
    read first, so no race to fall back from (the old fallback was a session
    per posting)."""
    from app.discovery.liveness import record_board_states
    with get_session() as s:
        s.add(JobLiveness(source="greenhouse", external_id=_gh("lv1"), state="LIVE",
                          inconclusive_streak=4, http_status=429))
        s.commit()
    with _Counter() as c:
        n = record_board_states("greenhouse", removed=[_gh("lv1"), _gh("lv2")],
                                live=[_gh("lv3")])
    assert n == 3
    writes = [st for st in c.statements if not st.startswith(("BEGIN", "COMMIT", "SAVEPOINT"))]
    assert len(writes) == 1 and "ON CONFLICT" in writes[0], writes
    lv1 = _liveness(_gh("lv1"))
    assert lv1.state == "REMOVED" and lv1.inconclusive_streak == 0 and lv1.http_status is None
    assert _liveness(_gh("lv2")).state == "REMOVED"
    assert _liveness(_gh("lv3")).state == "LIVE"
