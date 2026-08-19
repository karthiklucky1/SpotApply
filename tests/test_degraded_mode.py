"""Degraded mode: a higher bar while the LLM is down, one recheck after.

Running out of credits used to fill the board with weak matches, because the
local cross-encoder's score cleared the same 60 bar a Claude final does. The
rules under test:

* a LOCAL score must clear the degraded bar (75), a real one still clears 60;
* the recovery recheck looks back over the OUTAGE's own duration — 2 hours out
  means 2 hours of jobs — capped at 48h and 20 jobs per user;
* the outage window closes exactly once, so the recheck can never run twice.
"""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.strategy import degraded


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Never touch the real ./data/degraded_state.json."""
    monkeypatch.setattr(degraded, "_STATE_PATH", tmp_path / "degraded_state.json")


# ── The bar ──────────────────────────────────────────────────────────────────

def test_local_scores_are_held_to_the_degraded_bar():
    assert degraded.shortlist_threshold(is_local_score=True) == \
        settings.degraded_shortlist_threshold
    assert settings.degraded_shortlist_threshold > settings.shortlist_score_threshold, (
        "the degraded bar must be STRICTER than the normal one — that is the "
        "entire point of it"
    )


def test_real_scores_keep_the_normal_bar():
    assert degraded.shortlist_threshold(is_local_score=False) == \
        settings.shortlist_score_threshold


def test_degraded_bar_never_drops_below_the_normal_one(monkeypatch):
    """A misconfigured DEGRADED_SHORTLIST_THRESHOLD must not LOWER the bar."""
    monkeypatch.setattr(settings, "degraded_shortlist_threshold", 10)
    assert degraded.shortlist_threshold(is_local_score=True) >= \
        settings.shortlist_score_threshold


# ── The outage window ────────────────────────────────────────────────────────

def test_window_opens_once_and_tracks_the_start():
    t0 = datetime(2026, 8, 1, 10, 0, 0)
    degraded.note_degraded(t0)
    degraded.note_degraded(t0 + timedelta(minutes=30))   # still down
    window = degraded.note_healthy(t0 + timedelta(hours=2))
    assert window is not None
    start, end = window
    assert start == t0, "the window must start when the outage began, not when it ended"
    assert end == t0 + timedelta(hours=2)


def test_window_closes_exactly_once():
    t0 = datetime(2026, 8, 1, 10, 0, 0)
    degraded.note_degraded(t0)
    assert degraded.note_healthy(t0 + timedelta(hours=1)) is not None
    # A second healthy cycle must NOT re-trigger a recheck.
    assert degraded.note_healthy(t0 + timedelta(hours=1, minutes=5)) is None


def test_healthy_without_an_outage_is_a_no_op():
    assert degraded.note_healthy(datetime(2026, 8, 1, 10, 0, 0)) is None


# ── The look-back arithmetic ─────────────────────────────────────────────────

def _cutoff(start: datetime, end: datetime) -> datetime:
    """Mirror of the cutoff rule inside recheck_provisional."""
    span = timedelta(hours=max(1, settings.degraded_recheck_max_hours))
    return max(start, end - span)


def test_short_outage_looks_back_only_that_far():
    """2 hours out → 2 hours of jobs, not 2 days."""
    end = datetime(2026, 8, 3, 12, 0, 0)
    start = end - timedelta(hours=2)
    assert _cutoff(start, end) == start


def test_one_day_outage_looks_back_one_day():
    end = datetime(2026, 8, 3, 12, 0, 0)
    start = end - timedelta(days=1)
    assert _cutoff(start, end) == start


def test_long_outage_is_capped_at_the_maximum():
    """A week-long outage still only rechecks the cap — older jobs are stale."""
    end = datetime(2026, 8, 8, 12, 0, 0)
    start = end - timedelta(days=7)
    cutoff = _cutoff(start, end)
    assert cutoff == end - timedelta(hours=settings.degraded_recheck_max_hours)
    assert cutoff > start


def test_cap_is_two_days_and_per_user_volume_is_bounded():
    assert settings.degraded_recheck_max_hours == 48
    assert settings.degraded_recheck_max_jobs == 20


# ── The recheck, against the database ────────────────────────────────────────

class _FakeReranker:
    """Stands in for the LLM: returns a canned score per job title.

    Mirrors the REAL Reranker surface — built with profile=, and score()
    returning the (score, reason, concerns, breakdown) 4-tuple. The earlier
    stand-in invented a user_id= kwarg, a resume_text() method and a bare-float
    score(), so these tests passed against an interface that does not exist
    while the production recheck raised on every single job.
    """

    def __init__(self, scores, profile=None):
        self._scores = scores

    def score(self, resume_text, job):
        return self._scores[job.external_id], "reason", [], {}


def _seed(session, ext, score, provisional, age_hours, status=None):
    from app.db.models import Job, JobSource, Application, ApplicationStatus as _S
    j = Job(source=JobSource.LINKEDIN, external_id=ext, company="DegCo",
            title=f"Role {ext}", url=f"http://x/{ext}", description="d",
            rerank_score=score)
    session.add(j); session.commit(); session.refresh(j)
    a = Application(job_id=j.id, status=status or _S.SHORTLISTED,
                    apply_track="manual", provisional=provisional)
    session.add(a); session.commit(); session.refresh(a)
    a.created_at = datetime.utcnow() - timedelta(hours=age_hours)
    session.add(a); session.commit()
    return j, a


@pytest.fixture
def _clean_deg():
    from sqlmodel import select as _sel
    from app.db.init_db import get_session as _gs
    from app.db.models import Job, Application

    def _wipe():
        with _gs() as s:
            for j in s.exec(_sel(Job).where(Job.external_id.like("deg-%"))).all():
                for a in s.exec(_sel(Application).where(Application.job_id == j.id)).all():
                    s.delete(a)
                s.delete(j)
            s.commit()
    _wipe()
    yield
    _wipe()


def test_recheck_keeps_good_and_removes_weak(monkeypatch, _clean_deg):
    """The whole point: a provisional job that no longer holds up is dropped."""
    from sqlmodel import select as _sel
    from app.db.init_db import get_session as _gs
    from app.db.models import Application, ApplicationStatus as _S

    with _gs() as s:
        _seed(s, "deg-good", 80, provisional=True, age_hours=1)
        _seed(s, "deg-weak", 78, provisional=True, age_hours=1)
        _seed(s, "deg-real", 82, provisional=False, age_hours=1)   # already reviewed

    scores = {"deg-good": 88.0, "deg-weak": 41.0, "deg-real": 5.0}
    monkeypatch.setattr("app.matching.reranker.Reranker",
                        lambda profile=None: _FakeReranker(scores))

    end = datetime.utcnow()
    stats = degraded.recheck_provisional((end - timedelta(hours=2), end), [None])

    assert stats["checked"] == 2, "only the PROVISIONAL rows may be re-scored"
    assert stats["kept"] == 1 and stats["removed"] == 1

    with _gs() as s:
        rows = {j.external_id: a for a, j in s.exec(
            _sel(Application, __import__("app.db.models", fromlist=["Job"]).Job)
            .join(__import__("app.db.models", fromlist=["Job"]).Job)).all()
            if j.external_id.startswith("deg-")}
        assert rows["deg-good"].status == _S.SHORTLISTED
        assert rows["deg-weak"].status == _S.SKIPPED
        # Already-reviewed rows must be untouched even with a terrible fake score.
        assert rows["deg-real"].status == _S.SHORTLISTED
        # Flag cleared either way — a job can never be re-billed on a later pass.
        assert rows["deg-good"].provisional is False
        assert rows["deg-weak"].provisional is False


def test_recheck_ignores_jobs_older_than_the_window(monkeypatch, _clean_deg):
    from app.db.init_db import get_session as _gs

    with _gs() as s:
        _seed(s, "deg-old", 80, provisional=True, age_hours=30)

    monkeypatch.setattr("app.matching.reranker.Reranker",
                        lambda profile=None: _FakeReranker({"deg-old": 10.0}))
    end = datetime.utcnow()
    # Outage lasted 2 hours — a 30-hour-old job is outside it.
    stats = degraded.recheck_provisional((end - timedelta(hours=2), end), [None])
    assert stats["checked"] == 0


def test_recheck_is_capped_per_user(monkeypatch, _clean_deg):
    from app.db.init_db import get_session as _gs

    monkeypatch.setattr(settings, "degraded_recheck_max_jobs", 3)
    with _gs() as s:
        for i in range(6):
            _seed(s, f"deg-{i}", 80 + i, provisional=True, age_hours=1)

    scores = {f"deg-{i}": 90.0 for i in range(6)}
    monkeypatch.setattr("app.matching.reranker.Reranker",
                        lambda profile=None: _FakeReranker(scores))
    end = datetime.utcnow()
    stats = degraded.recheck_provisional((end - timedelta(hours=2), end), [None])
    assert stats["checked"] == 3
