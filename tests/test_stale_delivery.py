"""A posting the board's window would remove is never scored or delivered.

Production, 2026-10-29 to 2026-11-15 (counts and dates only): 17 postings whose
SOURCE dated them 30 to 711 days back were placed on two users' boards as new
matches — one published 2024-12-03, delivered 2026-11-14. Shortlist hygiene
removed every one on its next pass ("outside the freshness window"), so each
had cost a Claude final, taken a board slot and counted against the day's
allowance for a job the user never saw.

Freshness has two bounds (app/common/freshness.py): how long WE have held a
posting (tight) and what its SOURCE says about when it went up (loose, there
only to suppress evergreen and long-filled listings). Both were enforced — by
the expiry sweep, by adoption, by hygiene and by the render filter. They were
not enforced where it decided anything:

  * the scoring lane's queue applied the KNOWN bound only. The sweep that
    stamps "ancient" rows runs once at the start of a cycle; the queue is read
    later, after location verification. A copy the pulse lane routed in between
    was scored 13-21 seconds after it was created (the five November rows:
    known 0-4 days, source dates 92-711 days old);
  * the matching lane's retrieval applied NEITHER bound and has no sweep of its
    own (the twelve October rows: held 15-46 days, source dates 30-330 days
    old, every one carrying the ``similarity_score`` only that lane writes);
  * ``slate.place()``, the one writer of a board entry, looked at no date.

What these tests pin, as a class rather than as three patches:

  1. every list a scorer reads candidates from is the exact complement of the
     expiry sweep — whether or not the sweep has run;
  2. ``place()`` refuses precisely what hygiene would prune on its next pass,
     over a grid of ages, so delivery and hygiene cannot disagree again;
  3. the rules this must NOT cost: a job found today with an old or missing
     source date is still delivered, and a first-hour catch-up delivery still
     is too.

Rows are prefixed ``sd-`` and cleaned by that prefix only.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

import app.matching.matcher as mm
from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import Application, ApplicationStatus, FunnelEvent, Job, JobSource
from app.strategy import slate, welcome
from app.strategy.scoring_lane import _expire_stale_unscored, _user_queue

_P = "sd-"
_USER = "sd-user"      # file-unique, so the queue and retrieval see only our rows


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """The production windows, a board with room, and only our own rows."""
    import app.common.plan_limits as pl
    init_db()
    monkeypatch.setattr(settings, "scoring_max_job_age_days", 5)
    monkeypatch.setattr(settings, "scoring_max_posted_age_days", 30)
    monkeypatch.setattr(settings, "shortlist_max_age_days", 5)
    monkeypatch.setattr(settings, "shortlist_max_posted_age_days", 30)
    monkeypatch.setattr(settings, "welcome_catchup_days", 14)
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 10_000)
    _forget_first_hour()
    _clean()
    yield
    _forget_first_hour()
    _clean()


def _forget_first_hour():
    """Drop OUR user's welcome-window state — never another file's."""
    for memo in (welcome._BOOSTS, welcome._GRADUATED, welcome._LAST_CHECK,
                 welcome._FIRST_DONE):
        memo.pop(_USER, None)


def _clean():
    with get_session() as s:
        jids = [r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            # SQLite hands a deleted job's id to the next insert, and a
            # placement event left on that id would read as "this refusal was
            # already recorded" for a job it never described.
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.commit()


def _job(ext, *, found=0.0, posted=None, score=None, user=_USER) -> int:
    """A posting WE first saw ``found`` days ago that its SOURCE dates ``posted``
    days ago (None = the source gave no date). The two are independent — the
    whole defect was a path that looked at one and not the other. Each job is
    at its own company so neither the company cap nor the one-role-once rule
    takes part."""
    now = datetime.utcnow()
    with get_session() as s:
        j = Job(source=JobSource.ASHBY, external_id=_P + ext, company=f"Co {ext}",
                title=f"Backend Engineer {ext}", location="Remote", remote=True,
                url=f"https://jobs.example.test/{_P}{ext}", description="d",
                user_id=user, rerank_score=score,
                first_seen=now - timedelta(days=found),
                discovered_at=now - timedelta(days=found),
                posted_at=None if posted is None else now - timedelta(days=posted))
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _unscored(ids) -> set:
    with get_session() as s:
        return {r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.id.in_(list(ids)), Job.rerank_score.is_(None))).all()}


def _open_first_hour(monkeypatch):
    monkeypatch.setattr(settings, "welcome_boost_minutes", 60)
    welcome.begin(_USER, "test")
    assert welcome.scoring_window_days(_USER) == 14


# Ages chosen away from the bounds (5 / 14 / 30 days), so no case can flip on
# the seconds a test takes to run.
_KNOWN = (0, 3, 6, 9, 13, 16, 40)
_POSTED = (None, -3, 0, 12, 29, 31, 400)


# ── 1. The scoring lane's queue ──────────────────────────────────────────────

def test_the_queue_never_offers_an_ancient_posting_before_the_sweep_has_run():
    """THE November leak. Found today, published over a year ago, and the sweep
    has not reached it yet — which is the normal state of any copy that lands
    mid-cycle. It must not be a candidate for a paid score."""
    evergreen = _job("q-evergreen", found=0, posted=400)
    fresh = _job("q-fresh", found=0, posted=1)

    queue = _user_queue(_USER, cap=50)          # no sweep has run

    assert evergreen not in queue, (
        "a posting its source dates 400 days back was offered to the scorer "
        "because only the sweep, not the queue, knew the posted-age bound")
    assert queue == [fresh]


def test_the_queue_keeps_a_fresh_find_whatever_its_source_date_says():
    """What the posted bound must NOT become: a second known-age gate. Found
    today with a source date of 12 or 29 days, no date at all, or a future
    date — all still worth a score (the regression of 2026-08: 82.9% of expiry
    stamps had been jobs like these)."""
    keep = {_job(f"q-keep-{i}", found=0, posted=p)
            for i, p in enumerate((None, -3, 5, 12, 29))}
    assert set(_user_queue(_USER, cap=50)) == keep


def test_the_queue_is_exactly_what_the_sweep_would_leave():
    """The invariant behind both tests above: the queue and the sweep are two
    spellings of ONE rule. Whatever the sweep is about to stamp, the queue has
    already left out; whatever the queue offers, the sweep leaves alone."""
    ids = {(k, p): _job(f"q-grid-{k}-{p}", found=k, posted=p)
           for k in _KNOWN for p in _POSTED}

    before_sweep = set(_user_queue(_USER, cap=500))
    _expire_stale_unscored()
    survivors = _unscored(ids.values())

    disagree = {kp for kp, jid in ids.items() if (jid in before_sweep) != (jid in survivors)}
    assert not disagree, (
        f"(known_days, posted_days) the queue and the sweep judge differently: "
        f"{sorted(disagree, key=str)}")
    # And the rule itself, spelled out once.
    expected = {jid for (k, p), jid in ids.items() if k < 5 and (p is None or p < 30)}
    assert before_sweep == expected


def test_a_first_hour_queue_reaches_further_back_but_not_past_the_source_date(monkeypatch):
    """The catch-up window widens how long WE may have held a posting (14 days).
    It does not make a posting published months ago a catch-up candidate."""
    _open_first_hour(monkeypatch)
    held = _job("q-w-held", found=9, posted=9)
    undated = _job("q-w-undated", found=9, posted=None)
    ancient = _job("q-w-ancient", found=9, posted=100)
    too_long = _job("q-w-too-long", found=16, posted=16)

    queue = set(_user_queue(_USER, cap=50))

    assert queue == {held, undated}
    assert ancient not in queue and too_long not in queue


# ── 2. The matching lane's retrieval ─────────────────────────────────────────

def _retrieved() -> set:
    """Run the SELECT retrieval builds for ``only_unscored=True`` (the ranking
    itself needs the ML stack; the candidate set does not)."""
    q = select(*mm._candidate_columns()).where(
        Job.is_closed == False, Job.user_id == _USER)  # noqa: E712
    with get_session() as s:
        return {row[0] for row in s.exec(mm._unscored_only(q, 2000)).all()}


def test_retrieval_only_ranks_what_the_scoring_gate_would_score():
    """THE October leak. Retrieval took the newest 2,000 unscored rows whatever
    their age, and the matching lane has no sweep of its own."""
    fresh = _job("r-fresh", found=0, posted=None)
    old_date = _job("r-old-date", found=1, posted=12)
    evergreen = _job("r-evergreen", found=0, posted=400)
    held = _job("r-held", found=9, posted=9)
    both = _job("r-both", found=40, posted=330)
    scored = _job("r-scored", found=0, posted=1, score=71.0)

    got = _retrieved()

    assert got == {fresh, old_date}
    assert not got & {evergreen, held, both}, "a stale posting competed for a paid score"
    assert scored not in got


def test_retrieval_is_the_same_set_the_sweep_would_leave():
    ids = {(k, p): _job(f"r-grid-{k}-{p}", found=k, posted=p)
           for k in _KNOWN for p in _POSTED}
    got = _retrieved()
    _expire_stale_unscored()
    survivors = _unscored(ids.values())
    assert got == survivors


def test_retrieval_leaves_catch_up_postings_to_the_scoring_lane(monkeypatch):
    """A first-hour user's queue reaches back 14 days; retrieval does not. A
    catch-up delivery needs evidence the posting is open and its
    ``delivered_catchup`` mark, and only the scoring lane does either — a final
    bought here would be refused at the slate."""
    _open_first_hour(monkeypatch)
    recent = _job("r-w-recent", found=2, posted=2)
    catchup = _job("r-w-catchup", found=9, posted=9)

    assert _retrieved() == {recent}
    assert catchup in _user_queue(_USER, cap=50), "the scoring lane still serves it"


def test_the_matching_lane_reads_its_candidates_through_that_select():
    """``search_for_resume`` cannot run without the ML stack, so the wiring is
    pinned here: its unscored corpus is ``_unscored_only`` and nothing else."""
    src = inspect.getsource(mm.Matcher.search_for_resume)
    assert "_unscored_only(q, corpus_cap)" in src
    assert "rerank_score == None" not in src, (
        "search_for_resume builds its own unscored filter again — the copy "
        "that had no freshness gate")


# ── 3. The one writer of a board entry ───────────────────────────────────────

def _place(jid, score=88.0, **kw):
    with get_session() as s:
        res = slate.place(s, s.get(Job, jid), score, user_id=_USER, **kw)
        s.commit()
    return res


def _application(jid):
    with get_session() as s:
        return s.exec(select(Application).where(Application.job_id == jid)).first()


def _last_placement_reason(jid):
    with get_session() as s:
        row = s.exec(select(FunnelEvent.reason, FunnelEvent.passed)
                     .where(FunnelEvent.job_id == jid,
                            FunnelEvent.stage == slate.PLACEMENT_STAGE)
                     .order_by(FunnelEvent.id.desc()).limit(1)).first()
    return row


def test_the_slate_refuses_a_posting_its_source_dates_past_the_bound():
    """The backstop no lane can skip: scored 88, found today, published 400
    days ago. It keeps its score and never becomes a board entry."""
    jid = _job("p-evergreen", found=0, posted=400, score=88.0)

    res = _place(jid)

    assert not res.created
    assert res.outcome == slate.OUTCOME_ANCIENT_POSTING == "ancient_posting"
    assert _application(jid) is None
    # A qualified job that missed the board says why.
    assert tuple(_last_placement_reason(jid)) == ("ancient_posting", False)
    with get_session() as s:
        assert s.get(Job, jid).rerank_score == 88.0


def test_the_slate_refuses_a_posting_we_have_held_past_the_window():
    jid = _job("p-held", found=9, posted=None, score=88.0)
    res = _place(jid)
    assert not res.created and res.outcome == slate.OUTCOME_HELD_TOO_LONG
    assert _application(jid) is None
    assert tuple(_last_placement_reason(jid)) == ("held_too_long", False)


@pytest.mark.parametrize("posted", [None, -3, 0, 12, 29])
def test_the_slate_still_delivers_a_fresh_find_with_an_old_source_date(posted):
    """The behaviour the refusal must not cost. ATS dates are not reliable
    enough to evict a match found this morning (Greenhouse's moves on edits,
    aggregators stamp their crawl date) — that is why the posted bound is 30
    days and not 5."""
    jid = _job(f"p-fresh-{posted}", found=0, posted=posted, score=82.0)
    res = _place(jid, 82.0)
    assert res.created and res.outcome == "placed"
    assert _application(jid).status == ApplicationStatus.SHORTLISTED


def test_a_catch_up_delivery_is_placed_and_only_its_known_age_is_excused():
    """``catchup`` widens how long we may have held the posting. It excuses
    neither a posting held past the catch-up window nor an ancient source
    date."""
    ok = _job("p-c-ok", found=9, posted=9, score=85.0)
    too_long = _job("p-c-too-long", found=16, posted=16, score=85.0)
    ancient = _job("p-c-ancient", found=9, posted=100, score=85.0)

    assert _place(ok, 85.0, catchup=True).outcome == "placed"
    assert _place(too_long, 85.0, catchup=True).outcome == slate.OUTCOME_HELD_TOO_LONG
    assert _place(ancient, 85.0, catchup=True).outcome == slate.OUTCOME_ANCIENT_POSTING
    # And the same posting WITHOUT the lane's word for it is not a catch-up.
    plain = _job("p-c-plain", found=9, posted=9, score=85.0)
    assert _place(plain, 85.0).outcome == slate.OUTCOME_HELD_TOO_LONG


def test_turning_the_board_window_off_turns_the_refusal_off(monkeypatch):
    """``shortlist_max_age_days = 0`` disables the WHOLE window for hygiene,
    both bounds. A refusal that stayed on would leave the board unable to
    receive what it is now configured to keep."""
    monkeypatch.setattr(settings, "shortlist_max_age_days", 0)
    jid = _job("p-off", found=40, posted=400, score=88.0)
    assert _place(jid).created


def test_delivery_and_hygiene_agree_on_every_case():
    """THE class this file exists for. For every combination of how long we
    have held a posting, what its source says, and whether the lane calls it a
    catch-up delivery:

        the slate places it   <=>   hygiene, run a moment later, keeps it

    Each case uses a pair of identical postings: one is offered to ``place()``,
    the other is put on the board directly (as the old code did) so hygiene's
    own verdict on it can be read. A disagreement in either direction is the
    defect — a delivery hygiene then removes, or a refusal of something the
    board would have shown.
    """
    from app.strategy.shortlist_hygiene import prune_stale_shortlist

    cases = [(k, p, c) for k in _KNOWN for p in _POSTED for c in (False, True)]
    placed, control = {}, {}
    for k, p, c in cases:
        tag = f"{k}-{p}-{int(c)}"
        a = _job(f"h-a-{tag}", found=k, posted=p, score=80.0)
        b = _job(f"h-b-{tag}", found=k, posted=p, score=80.0)
        res = _place(a, 80.0, catchup=c)
        placed[(k, p, c)] = res
        with get_session() as s:
            if res.created:      # the lane marks its catch-up deliveries
                row = s.exec(select(Application).where(Application.job_id == a)).first()
                row.delivered_catchup = c
                s.add(row)
            s.add(Application(job_id=b, user_id=_USER, delivered_catchup=c,
                              status=ApplicationStatus.SHORTLISTED))
            s.commit()
        control[(k, p, c)] = (a, b)

    prune_stale_shortlist()

    wrongly_delivered, wrongly_refused, not_kept = [], [], []
    for case, (a, b) in control.items():
        hygiene_keeps = _application(b).status == ApplicationStatus.SHORTLISTED
        res = placed[case]
        if res.created and not hygiene_keeps:
            wrongly_delivered.append(case)
        if not res.created and hygiene_keeps:
            wrongly_refused.append((case, res.outcome))
        if res.created and _application(a).status != ApplicationStatus.SHORTLISTED:
            not_kept.append(case)
    assert not wrongly_delivered, (
        f"placed, then removed by the next hygiene pass (known, posted, catchup): "
        f"{wrongly_delivered}")
    assert not wrongly_refused, f"refused although the board keeps it: {wrongly_refused}"
    assert not not_kept
    # Not vacuous: the grid really contains both answers.
    outcomes = {r.outcome for r in placed.values()}
    assert {"placed", "held_too_long", "ancient_posting"} <= outcomes


# ── 4. The scoring lane says which deliveries are catch-up ───────────────────

def test_the_scoring_lane_delivers_a_catch_up_posting_through_the_real_slate(monkeypatch):
    """End to end, with nothing about the slate faked: a first-hour user's
    9-day-old posting, confirmed open, reaches the board marked
    ``delivered_catchup`` — and the same lane cannot deliver an ancient one
    even if its queue were ever to hand it one."""
    from app.strategy import delivery_gate as dg
    from app.strategy import scoring_lane as sl
    _open_first_hour(monkeypatch)
    monkeypatch.setattr(dg, "confirmed_open", lambda *a, **k: True)
    monkeypatch.setattr(dg, "verified_dead", lambda *a, **k: False)
    import app.strategy.fresh_alerts as fa
    monkeypatch.setattr(fa, "dispatch_fresh_alerts", lambda *a, **k: 0)

    fresh = _job("l-fresh", found=1, posted=1, score=90.0)
    catchup = _job("l-catchup", found=9, posted=9, score=88.0)
    ancient = _job("l-ancient", found=9, posted=100, score=87.0)
    stats: dict = {"shortlisted": 0, "alerts": 0}

    sl._shortlist_user(_USER, [(fresh, 90.0), (catchup, 88.0), (ancient, 87.0)], stats)

    assert _application(fresh) is not None and not _application(fresh).delivered_catchup
    assert _application(catchup) is not None, (
        "the slate's freshness check refused a catch-up delivery the lane had "
        "confirmed open")
    assert _application(catchup).delivered_catchup
    assert _application(ancient) is None
    assert stats["shortlisted"] == 2
