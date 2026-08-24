"""Expiry has TWO bounds, because "old" means two different things.

The scoring age gate used to answer "is this job stale?" with one expression::

    coalesce(posted_at, first_seen, discovered_at) < now - 5 days

``posted_at`` wins that coalesce, so a job DISCOVERED TODAY was stamped
terminally stale (``rerank_score = 8.0``) the moment a source claimed it went
up six days ago — before it could ever be scored, shortlisted or shown.

Production measured the damage: 82.9% of those expiry stamps were already >= 5
days old at first discovery, median detection lag was ~91.5h, 36.7% of intake
was >7d old the first time we saw it, and 11 of 13 users had been stamped down
to zero unscored jobs, which removed them from ``_scorable_user_ids`` entirely.
The lane looked idle because its input had been destroyed.

ATS ``posted_at`` cannot carry that weight: Greenhouse's ``updated_at`` moves
on edits, aggregators stamp their own crawl date, evergreen reqs are re-dated,
some feeds emit future dates. So ``first_seen - posted_at`` is not crawler
latency — it is mostly source noise.

The rule these tests pin::

    stale  <=>  known_age > scoring_max_job_age_days
            OR  posting_age > scoring_max_posted_age_days

with the known bound tight (the product promise) and the posted bound loose (it
exists only to suppress ancient/evergreen listings). The pair must move in
lockstep with the render window, or we pay for finals the board then hides.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete

from app.common.freshness import (
    EXPIRY_SENTINEL_SCORE, expired_without_scoring_expr, terminal_verdict_expr,
    is_fresh,
)
from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import Job, JobSource
from app.discovery.pipeline import SHARED_POOL_USER
from app.strategy.scoring_lane import _expire_stale_unscored


# Every row this file creates carries this prefix, and cleanup deletes ONLY
# those. A wholesale `delete(Job)` would take out job fixtures other test files
# had already built, which is a flaky suite, not a clean one.
_PREFIX = "es-"
_USER = "expiry-sem-user"   # file-unique, so _user_queue sees only our rows


def _job(user_id, ext, *, found_days_ago=0, posted_days_ago=None, score=None,
         reasoning=None):
    """A job we FIRST SAW `found_days_ago` ago, that the source says went up
    `posted_days_ago` ago. Those two are deliberately independent here — the
    whole bug was treating them as one number."""
    now = datetime.utcnow()
    with get_session() as session:
        j = Job(
            source=JobSource.GREENHOUSE, external_id=_PREFIX + ext, company="Acme",
            title=f"Role {ext}", url=f"https://x.test/{_PREFIX}{ext}", user_id=user_id,
            rerank_score=score, rerank_reasoning=reasoning,
            first_seen=now - timedelta(days=found_days_ago),
            discovered_at=now - timedelta(days=found_days_ago),
            posted_at=(None if posted_days_ago is None
                       else now - timedelta(days=posted_days_ago)),
        )
        session.add(j)
        session.commit()
        session.refresh(j)
        return j.id


def _cleanup():
    with get_session() as session:
        session.exec(delete(Job).where(Job.external_id.like(f"{_PREFIX}%")))
        session.commit()


def _mine_expired():
    """This file's rows that carry an age-expiry stamp, split by which bound
    fired. The gate's return value counts globally, so scoping the assertions
    to our own rows is what keeps this file independent of the suite."""
    from sqlmodel import select
    with get_session() as session:
        rows = session.exec(
            select(Job.rerank_reasoning)
            .where(Job.external_id.like(f"{_PREFIX}%"),
                   Job.expired_at.is_not(None))).all()
    reasons = [r[0] if isinstance(r, tuple) else r for r in rows]
    return {
        "total": len(reasons),
        "queue_stale": sum(1 for r in reasons if "held" in (r or "")),
        "ancient_posting": sum(1 for r in reasons if "posting date" in (r or "")),
    }


def _row(jid):
    with get_session() as session:
        return session.get(Job, jid)


def _windows(monkeypatch, known=5, posted=30):
    monkeypatch.setattr(settings, "scoring_max_job_age_days", known)
    monkeypatch.setattr(settings, "scoring_max_posted_age_days", posted)


# ── 5. A fresh discovery with an old/unreliable posted_at survives ───────────

def test_job_found_today_is_not_expired_by_a_stale_source_date(monkeypatch):
    """THE regression. Found this morning, source says it went up 12 days ago —
    well past the 5-day known bound but nowhere near the 30-day posted bound.
    Under the old single-coalesce gate this expired instantly."""
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    jid = _job(_USER, "found-today-posted-old", found_days_ago=0, posted_days_ago=12)

    _expire_stale_unscored()

    row = _row(jid)
    assert row.rerank_score is None, (
        "a job discovered today was expired because an unreliable ATS date "
        "called it old — the exact bug this split exists to fix")
    assert row.expired_at is None
    _cleanup()


def test_the_whole_immediate_expiry_band_survives(monkeypatch):
    """82.9% of production's expiry stamps were >=5d old at first discovery.
    Every posting age across that band, discovered today, must now survive."""
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    ids = {d: _job(_USER, f"band-{d}", found_days_ago=0, posted_days_ago=d)
           for d in (5, 6, 7, 10, 14, 21, 29)}

    _expire_stale_unscored()

    for days, jid in ids.items():
        assert _row(jid).rerank_score is None, (
            f"a job discovered today but posted {days}d ago was expired; the "
            f"posted bound is {settings.scoring_max_posted_age_days}d")
    _cleanup()


def test_a_future_dated_posting_is_not_expired(monkeypatch):
    """Some feeds emit future dates. A negative posting age must not trip the
    ancient bound (and must not wrap around into looking maximally stale)."""
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    jid = _job(_USER, "future", found_days_ago=0, posted_days_ago=-3)
    _expire_stale_unscored()
    assert _row(jid).rerank_score is None
    _cleanup()


def test_a_job_with_no_posted_at_is_judged_only_on_known_age(monkeypatch):
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    fresh = _job(_USER, "undated-fresh", found_days_ago=1, posted_days_ago=None)
    held = _job(_USER, "undated-held", found_days_ago=9, posted_days_ago=None)

    _expire_stale_unscored()

    assert _row(fresh).rerank_score is None
    assert _row(held).rerank_score == EXPIRY_SENTINEL_SCORE
    _cleanup()


# ── 6. A genuinely stale job still expires ───────────────────────────────────

def test_a_job_we_held_unscored_too_long_still_expires(monkeypatch):
    """The product promise the gate exists to keep: 'be first to apply'. A job
    that sat in OUR queue unscored for longer than the known bound has missed
    its moment, whatever its source date says."""
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    jid = _job(_USER, "held-too-long", found_days_ago=9, posted_days_ago=9)

    _expire_stale_unscored()

    row = _row(jid)
    assert row.rerank_score == EXPIRY_SENTINEL_SCORE
    assert row.expired_at is not None
    mine = _mine_expired()
    assert mine["queue_stale"] == 1
    assert mine["total"] == 1
    _cleanup()


def test_an_ancient_evergreen_posting_still_expires_on_first_sight(monkeypatch):
    """The behaviour the split must NOT lose. A req the source dates to last
    year is a long-filled or evergreen listing; discovering it today does not
    make it worth a Claude final."""
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    jid = _job(_USER, "evergreen", found_days_ago=0, posted_days_ago=400)

    _expire_stale_unscored()

    row = _row(jid)
    assert row.rerank_score == EXPIRY_SENTINEL_SCORE
    assert row.expired_at is not None
    assert "posting date" in (row.rerank_reasoning or "")
    assert _mine_expired()["ancient_posting"] == 1
    _cleanup()


def test_the_two_reasons_are_reported_separately(monkeypatch):
    """If ancient dwarfs queue-stale the SOURCES are the problem; if queue-stale
    dwarfs `scored` the SCORER is behind. One combined number could say
    neither."""
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    _job(_USER, "q1", found_days_ago=8, posted_days_ago=8)
    _job(_USER, "q2", found_days_ago=9, posted_days_ago=None)
    _job(_USER, "a1", found_days_ago=0, posted_days_ago=90)

    _expire_stale_unscored()

    mine = _mine_expired()
    assert mine["queue_stale"] == 2
    assert mine["ancient_posting"] == 1
    assert mine["total"] == 3
    _cleanup()


def test_already_scored_and_shared_pool_rows_are_untouched(monkeypatch):
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    scored = _job(_USER, "scored-old", found_days_ago=40, posted_days_ago=40, score=72.0)
    shared = _job(SHARED_POOL_USER, "shared-old", found_days_ago=40, posted_days_ago=40)

    _expire_stale_unscored()

    assert _row(scored).rerank_score == 72.0
    assert _row(shared).rerank_score is None
    _cleanup()


def test_both_bounds_at_zero_disables_the_gate(monkeypatch):
    init_db()
    _cleanup()
    _windows(monkeypatch, known=0, posted=0)
    jid = _job(_USER, "ancient", found_days_ago=300, posted_days_ago=300)
    assert _expire_stale_unscored()["total"] == 0
    assert _row(jid).rerank_score is None
    _cleanup()


def test_expired_jobs_leave_the_scoring_queue(monkeypatch):
    """An expiry has to actually drain the queue — that is what makes it free
    instead of a repeated prescore."""
    from app.strategy.scoring_lane import _user_queue
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    _job(_USER, "old-1", found_days_ago=40, posted_days_ago=40)
    fresh = _job(_USER, "fresh-1", found_days_ago=1, posted_days_ago=20)

    _expire_stale_unscored()

    assert _user_queue(_USER, cap=10) == [fresh]
    _cleanup()


# ── the Python mirror of the rule agrees with the SQL ────────────────────────

def test_python_and_sql_freshness_agree():
    """`is_fresh` is used on loaded rows; the SQL builds the same rule. They are
    written twice, so they are checked against each other."""
    init_db()
    _cleanup()
    cases = [
        # (found_days_ago, posted_days_ago, expected_fresh)
        (0, 12, True),      # found today, unreliable old source date
        (0, None, True),
        (0, -3, True),      # future-dated
        (9, 9, False),      # held too long
        (0, 400, False),    # ancient / evergreen
        (1, 29, True),
        (1, 31, False),
    ]
    for found, posted, expected in cases:
        jid = _job(_USER, f"mirror-{found}-{posted}", found_days_ago=found,
                   posted_days_ago=posted)
        row = _row(jid)
        assert is_fresh(row, 5, 30) is expected, (
            f"found={found}d posted={posted}d expected fresh={expected}")
    _cleanup()


# ── 7. Expiry stamps are not counted as genuinely scored ─────────────────────

def test_expiry_stamps_are_not_counted_as_scored(monkeypatch):
    """`rerank_score IS NOT NULL` is not "was scored". Production's "621k scored
    jobs" was mostly age stamps, which made the scorer look far more productive
    than it was."""
    from sqlmodel import func, select
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    _job(_USER, "real-verdict", found_days_ago=1, posted_days_ago=1, score=71.0)
    _job(_USER, "will-expire", found_days_ago=40, posted_days_ago=40)
    _expire_stale_unscored()

    with get_session() as session:
        def n(expr):
            v = session.exec(select(func.count(Job.id)).where(
                Job.external_id.like(f"{_PREFIX}%"), expr)).one()
            return int(v[0] if isinstance(v, tuple) else v)

        assert n(terminal_verdict_expr()) == 1
        assert n(expired_without_scoring_expr()) == 1
        # The naive predicate cannot tell them apart — which is the point.
        assert n(Job.rerank_score.is_not(None)) == 2
    _cleanup()


def test_a_real_verdict_that_lands_on_the_sentinel_value_still_counts():
    """The sentinel is an overloaded NUMBER, so a genuine Claude final of 8.0 is
    indistinguishable from an expiry stamp by score alone. `scored_at` is what
    resolves it: where a scorer actually ran, the score is trusted whatever its
    value; only legacy rows fall back to excluding the sentinel."""
    from sqlmodel import func, select

    from app.strategy.scoring_lane import _stamp_job
    init_db()
    _cleanup()
    jid = _job(_USER, "real-eight", found_days_ago=0, posted_days_ago=1)
    assert _stamp_job(jid, None, EXPIRY_SENTINEL_SCORE, "genuinely a terrible fit") is True

    row = _row(jid)
    assert row.rerank_score == EXPIRY_SENTINEL_SCORE
    assert row.scored_at is not None and row.expired_at is None

    with get_session() as session:
        def n(expr):
            v = session.exec(select(func.count(Job.id)).where(
                Job.external_id.like(f"{_PREFIX}%"), expr)).one()
            return int(v[0] if isinstance(v, tuple) else v)

        assert n(terminal_verdict_expr()) == 1, (
            "a real verdict was discarded because it happened to equal the "
            "expiry sentinel")
        assert n(expired_without_scoring_expr()) == 0
    _cleanup()


def test_legacy_expiry_rows_without_the_column_are_still_excluded():
    """Rows stamped before `expired_at` shipped carry only the 8.0 sentinel.
    They must not be counted as scoring work either."""
    from sqlmodel import func, select
    init_db()
    _cleanup()
    _job(_USER, "legacy-expiry", found_days_ago=40, posted_days_ago=40,
         score=EXPIRY_SENTINEL_SCORE,
         reasoning="Expired unscored (older than 5d before scoring caught up)")

    with get_session() as session:
        v = session.exec(
            select(func.count(Job.id)).where(
                Job.external_id.like(f"{_PREFIX}%"), terminal_verdict_expr())).one()
        assert int(v[0] if isinstance(v, tuple) else v) == 0
    _cleanup()


# ── 8. Stage instrumentation ─────────────────────────────────────────────────

def test_expiry_records_a_lifecycle_timestamp_not_just_a_score(monkeypatch):
    init_db()
    _cleanup()
    _windows(monkeypatch, known=5, posted=30)
    jid = _job(_USER, "stamped", found_days_ago=40, posted_days_ago=40)
    _expire_stale_unscored()
    row = _row(jid)
    assert row.expired_at is not None
    assert row.scored_at is None, "an expiry is not a scoring verdict"
    _cleanup()


def test_a_real_verdict_records_scored_at():
    """`first_seen -> scored` was unmeasurable: rerank_score said WHAT, nothing
    said WHEN."""
    from app.strategy.scoring_lane import _stamp_job
    init_db()
    _cleanup()
    jid = _job(_USER, "to-score", found_days_ago=0, posted_days_ago=1)

    assert _stamp_job(jid, None, 77.0, "good fit", prescore=61.0) is True

    row = _row(jid)
    assert row.rerank_score == 77.0
    assert row.scored_at is not None
    assert row.prescored_at is not None
    assert row.expired_at is None
    assert row.scored_at >= row.first_seen
    _cleanup()
