"""The daily slate: reaching the day's count ends delivery, not the search.

The behaviour this replaces (measured in production 2026-09-06/09/10): the
board filled between 18:00 and 22:00 UTC, ``allowance()`` returned zero, the
pulse fast path returned before Tier-1, and a posting that appeared at 15:12
and would have scored 92 waited behind thirty-five jobs scoring 71-73 until
00:00 UTC.

Rows are prefixed ``sl_`` and cleaned by that prefix — a wholesale delete takes
out fixtures other test files built in the same session.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.strategy import slate

_P = "sl_"
UID = "sl_user"
CAP = 5          # a small slate keeps the tests readable


@pytest.fixture(autouse=True)
def _small_slate(monkeypatch):
    """Pin the plan cap and clean up only this file's rows."""
    import app.common.plan_limits as pl
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: CAP)
    _clean()
    yield
    _clean()


def _clean():
    with get_session() as s:
        jids = [r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.commit()


def _job(session, ext: str, score: float) -> Job:
    """One job at its own company, so the per-company cap never interferes."""
    j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + ext,
            company=f"Co-{ext}", title=f"ML Engineer {ext}", location="Austin, TX",
            remote=False, url=f"https://x/{ext}", description="d",
            rerank_score=score, first_seen=datetime.utcnow())
    session.add(j)
    session.commit()
    session.refresh(j)
    return j


def _fill(scores, viewed=False, status=ApplicationStatus.SHORTLISTED):
    """Put `scores` on today's slate. Returns [(app_id, job_id, score)]."""
    out = []
    with get_session() as s:
        for i, sc in enumerate(scores):
            j = _job(s, f"e{i}_{int(sc)}", sc)
            a = Application(job_id=j.id, user_id=UID, status=status,
                            apply_url=j.url, apply_track="autofill",
                            viewed_at=datetime.utcnow() if viewed else None)
            s.add(a)
            s.commit()
            s.refresh(a)
            out.append((a.id, j.id, sc))
    return out


def _slate_scores():
    with get_session() as s:
        return sorted(slate._score_of(s, a) for a in slate.todays_entries(s, UID))


# ── While there is room ──────────────────────────────────────────────────────

def test_a_job_takes_a_free_slot_and_there_is_no_cutoff_yet():
    _fill([80, 78])
    assert slate.cutoff(UID) is None, "a slate with room has nothing to beat"
    with get_session() as s:
        j = _job(s, "new", 72)
        res = slate.place(s, j, 72, user_id=UID)
        s.commit()
    assert res.created and res.outcome == "placed"
    assert _slate_scores() == [72, 78, 80]


# ── Once it is full ──────────────────────────────────────────────────────────

def test_the_cutoff_is_the_weakest_replaceable_entry():
    _fill([71, 72, 72, 73, 90])
    assert slate.cutoff(UID) == 71


def test_a_stronger_late_job_replaces_the_weakest_unviewed_entry():
    """The case the redesign exists for: 35 delivered by 14:00, a 92 at 15:12."""
    entries = _fill([71, 72, 72, 73, 90])
    weakest_app = entries[0][0]
    with get_session() as s:
        j = _job(s, "late", 92)
        res = slate.place(s, j, 92, user_id=UID)
        s.commit()

    assert res.created and res.outcome == "replaced"
    assert res.displaced_id == weakest_app
    assert res.cutoff == 71
    # The board is still exactly CAP jobs, and the 71 is gone.
    assert _slate_scores() == [72, 72, 73, 90, 92]
    with get_session() as s:
        old = s.get(Application, weakest_app)
    assert old.status == ApplicationStatus.SKIPPED
    assert slate.SLATE_REPLACED_MARKER in (old.notes or "")


def test_a_marginal_job_does_not_churn_the_board():
    """Hysteresis: without the margin a 71.4 would evict a 71 every afternoon
    for no user-visible gain."""
    _fill([71, 72, 72, 73, 90])
    with get_session() as s:
        j = _job(s, "marginal", 74)
        res = slate.place(s, j, 74, user_id=UID)   # 74 < 71 + 5
        s.commit()
    assert not res.created and res.outcome == "below_cutoff"
    assert _slate_scores() == [71, 72, 72, 73, 90]


# ── What may never be replaced ───────────────────────────────────────────────

def test_a_job_the_user_opened_is_never_taken_away():
    """The weakest entry is a 71 the user has READ. It must survive, and the
    next-weakest UNVIEWED entry is replaced instead."""
    _fill([71], viewed=True)
    unviewed = _fill([72, 73, 74, 90])
    with get_session() as s:
        j = _job(s, "strong", 95)
        res = slate.place(s, j, 95, user_id=UID)
        s.commit()
    assert res.created and res.outcome == "replaced"
    assert res.displaced_id == unviewed[0][0], "the viewed 71 was evicted"
    assert 71 in _slate_scores()


def test_tailored_and_beyond_are_never_replaced():
    _fill([60], status=ApplicationStatus.TAILORED)
    _fill([72, 73, 74, 90])
    with get_session() as s:
        j = _job(s, "strong2", 95)
        res = slate.place(s, j, 95, user_id=UID)
        s.commit()
    assert res.created and res.cutoff == 72, "the tailored 60 was treated as replaceable"
    assert 60 in _slate_scores()


# ── When nothing is replaceable ──────────────────────────────────────────────

def test_an_exceptional_job_overflows_a_fully_read_slate(monkeypatch):
    """Every entry has been opened. A user who has read all of today's jobs and
    is still looking should not be denied the best one of the day."""
    _fill([71, 72, 72, 73, 90], viewed=True)
    with get_session() as s:
        j = _job(s, "overflow", 90)
        res = slate.place(s, j, 90, user_id=UID)       # 90 >= 71 + 15
        s.commit()
    assert res.created and res.outcome == "overflow"
    assert len(_slate_scores()) == CAP + 1


def test_overflow_needs_a_much_bigger_margin_than_replacement():
    _fill([71, 72, 72, 73, 90], viewed=True)
    with get_session() as s:
        j = _job(s, "notenough", 80)      # beats 71 by 9: enough to replace, not to overflow
        res = slate.place(s, j, 80, user_id=UID)
        s.commit()
    assert not res.created
    assert len(_slate_scores()) == CAP


def test_overflow_is_capped_per_day(monkeypatch):
    monkeypatch.setattr(settings, "slate_overflow_daily", 1)
    _fill([71, 72, 72, 73, 90], viewed=True)
    with get_session() as s:
        first = slate.place(s, _job(s, "of1", 95), 95, user_id=UID)
        s.commit()
        second = slate.place(s, _job(s, "of2", 96), 96, user_id=UID)
        s.commit()
    assert first.created and first.outcome == "overflow"
    assert not second.created, "overflow ran past its daily cap"


# ── Accounting ───────────────────────────────────────────────────────────────

def test_a_replaced_entry_does_not_keep_consuming_a_slot():
    """If a replaced row still counted, the board would shrink by one every
    time a challenger won."""
    _fill([71, 72, 72, 73, 90])
    with get_session() as s:
        slate.place(s, _job(s, "r1", 92), 92, user_id=UID)
        s.commit()
        slate.place(s, _job(s, "r2", 93), 93, user_id=UID)
        s.commit()
    assert len(_slate_scores()) == CAP


def test_a_user_dismissal_still_counts_against_the_day():
    """A dismissal is an opinion about a job we DID deliver. Re-filling behind
    every dismissal would make the board a treadmill the budget never escapes."""
    entries = _fill([71, 72, 72, 73, 90])
    with get_session() as s:
        a = s.get(Application, entries[0][0])
        a.status = ApplicationStatus.SKIPPED
        a.notes = "user_dismissed"
        s.add(a)
        s.commit()
    with get_session() as s:
        assert len(slate.todays_entries(s, UID)) == CAP


def test_yesterdays_board_does_not_count_against_today():
    # Measured against the SAME clock the finals budget uses (conftest pins it),
    # because a slate day that disagreed with a budget day would be a boundary
    # that cannot happen in production.
    entries = _fill([71, 72, 72, 73, 90])
    before_today = slate._day_start() - timedelta(hours=1)
    with get_session() as s:
        for app_id, _jid, _sc in entries:
            a = s.get(Application, app_id)
            a.created_at = before_today
            s.add(a)
        s.commit()
    assert slate.cutoff(UID) is None
    with get_session() as s:
        res = slate.place(s, _job(s, "today", 71), 71, user_id=UID)
        s.commit()
    assert res.created and res.outcome == "placed"


# ── The invariant that keeps it that way ─────────────────────────────────────

def test_only_the_slate_creates_a_shortlisted_application():
    """The class of mistake this guards.

    Three lanes each carried their own `today_count < shortlist_daily_limit()`
    check plus their own call to the company cap. They had already drifted —
    only the scoring lane applied the degraded/provisional bar — and a fourth
    copy would quietly reintroduce the stop this whole design removes. Placement
    lives in ONE function.

    The paste-a-link importer is the deliberate exception: the user asked for
    that specific job by URL, so it is not competing for a slot on a slate we
    chose.
    """
    import pathlib
    import re

    allowed = {"app/strategy/slate.py",              # the one placement path
               "app/discovery/extractor.py"}         # user pasted a URL
    root = pathlib.Path("app")
    creation = re.compile(r"Application\((?:[^()]|\([^()]*\))*?status\s*=\s*"
                          r"ApplicationStatus\.SHORTLISTED", re.S)
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.as_posix()
        if rel in allowed:
            continue
        if creation.search(path.read_text()):
            offenders.append(rel)
    assert not offenders, (
        "these files create a SHORTLISTED application directly instead of going "
        f"through app/strategy/slate.place(): {offenders}. Capacity, the company "
        "cap and the challenger rule all live there; a second path means a job "
        "can reach the board without competing for its place.")
