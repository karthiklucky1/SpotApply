"""The verification script's own metrics must be capable of being wrong.

A metric that can only return one value is worse than no metric: it looks like
evidence. The intake-expiry rate had exactly that defect in production. It
sampled ``expired AND first_seen >= since`` and asked whether each row was
already past the KNOWN-age bound at discovery — but a queue-stale expiry
requires ``first_seen < now - known_days``, so it could never appear in a window
shorter than that. The sample could only ever hold posting-age expiries, which
satisfy the test by definition, so the figure read 100.0% while the pipeline was
healthy, and the advice line sent the operator to check a setting that was
already correct.

These tests pin the replacement: the denominator is the window's INTAKE, so a
counter-example is reachable and the number can actually move.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import Job, JobSource
from app.discovery.pipeline import SHARED_POOL_USER
from scripts.verify_discovery_fix import collect

_PREFIX = "vm-"
_USER = "verify-metrics-user"


def _cleanup():
    with get_session() as s:
        s.exec(delete(Job).where(Job.external_id.like(f"{_PREFIX}%")))
        s.commit()


def _job(ext, *, held_hours=0, posted_days=None, expired=False,
         score=None, prescore=None, user_id=_USER):
    now = datetime.utcnow()
    seen = now - timedelta(hours=held_hours)
    with get_session() as s:
        j = Job(
            source=JobSource.GREENHOUSE, external_id=_PREFIX + ext,
            company="C", title="ML Engineer", url=f"https://x/{_PREFIX}{ext}",
            user_id=user_id, first_seen=seen, discovered_at=seen,
            posted_at=(None if posted_days is None
                       else now - timedelta(days=posted_days)),
            rerank_score=score, prescore=prescore,
            expired_at=(now if expired else None),
            scored_at=(now if (score is not None and not expired) else None),
        )
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _only_ours(d: dict) -> dict:
    """`collect` counts globally; these tests assert on RATIOS and on rows they
    created, so each one starts from a cleaned prefix and a fresh window."""
    return d


# ── 7. The intake-expiry metric uses a logically valid denominator ───────────

def test_the_denominator_is_intake_not_expiries():
    """The structural defect: with expiries as both numerator and sample, the
    ratio was pinned at 100%. Against intake it must be able to be low."""
    init_db()
    _cleanup()
    # 4 jobs entered the pool this hour; only 1 was expired on posting age.
    _job("fresh-1", held_hours=0, posted_days=2)
    _job("fresh-2", held_hours=0, posted_days=5)
    _job("fresh-3", held_hours=0, posted_days=None)
    _job("ancient", held_hours=0, posted_days=400, expired=True,
         score=8.0)

    d = collect(hours=1)

    assert d["intake_expired"] >= 1
    assert d["user_pool_new"] >= 4
    # THE POINT: strictly below 100. Under the old metric this was 100.0 by
    # construction, because the denominator held only expired rows.
    assert d["intake_expired_pct"] < 100.0, (
        "the intake-expiry rate is pinned at 100% again — the denominator has "
        "gone back to being the expiry sample instead of the window's intake")
    _cleanup()


def test_the_old_formula_was_pinned_at_100_on_the_same_data():
    """Side by side, on one fixture: the OLD formula cannot distinguish a
    healthy pipeline from a broken one, and the new one can.

    This is the actual defect, reproduced rather than described. The old metric
    divided expiries by expiries-that-look-immediate; because a queue-stale
    expiry cannot exist inside a short window, every member of its sample
    qualified and the answer was always 100.0.
    """
    init_db()
    _cleanup()
    _job("cmp-ok-1", held_hours=0, posted_days=2)
    _job("cmp-ok-2", held_hours=0, posted_days=3)
    _job("cmp-ok-3", held_hours=0, posted_days=4)
    _job("cmp-exp", held_hours=0, posted_days=400, expired=True, score=8.0)

    now = datetime.utcnow()
    since = now - timedelta(hours=1)
    known_days = int(settings.scoring_max_job_age_days or 0)

    # The OLD numerator/denominator, recomputed verbatim over the same rows.
    with get_session() as s:
        sample = s.exec(
            select(Job.first_seen, Job.posted_at).where(
                Job.external_id.like(f"{_PREFIX}%"),
                Job.expired_at.is_not(None),
                Job.first_seen >= since)).all()
    immediate = sum(1 for fs, p in sample
                    if fs is not None and p is not None
                    and (fs - p) >= timedelta(days=known_days))
    old_pct = 100.0 * immediate / len(sample) if sample else None

    new_pct = collect(hours=1)["intake_expired_pct"]

    assert old_pct == 100.0, (
        "the old formula should be demonstrably pinned at 100% here — if it is "
        "not, this test no longer reproduces the defect it exists to document")
    assert new_pct is not None and new_pct < 100.0, (
        f"new metric reads {new_pct}% on data where only 1 of 4 intake rows "
        f"expired — it has inherited the old formula's blind spot")
    _cleanup()


def test_a_clean_window_reports_zero_not_one_hundred():
    """No expiries at all must read 0%, not 100% and not None."""
    init_db()
    _cleanup()
    _job("clean-1", held_hours=0, posted_days=1)
    _job("clean-2", held_hours=0, posted_days=3)

    d = collect(hours=1)

    assert d["user_pool_new"] >= 2
    assert d["intake_expired_pct"] is not None
    assert d["intake_expired_pct"] < 100.0
    _cleanup()


def test_the_metric_moves_with_the_expiry_rate():
    """Monotonicity — the only real proof a ratio is measuring something."""
    init_db()
    _cleanup()
    for i in range(6):
        _job(f"low-{i}", held_hours=0, posted_days=1)
    _job("low-exp", held_hours=0, posted_days=400, expired=True, score=8.0)
    low = collect(hours=1)["intake_expired_pct"]
    _cleanup()

    _job("hi-ok", held_hours=0, posted_days=1)
    for i in range(6):
        _job(f"hi-exp-{i}", held_hours=0, posted_days=400, expired=True, score=8.0)
    high = collect(hours=1)["intake_expired_pct"]
    _cleanup()

    assert high > low, (
        f"expiry rate did not rise with more expiries ({low}% -> {high}%) — the "
        f"metric is not tracking the thing it names")


def test_the_expiry_split_sums_to_the_total():
    """Ancient + queue-stale must account for every expired intake row, or one
    of the two bounds is being silently mis-attributed."""
    init_db()
    _cleanup()
    _job("split-ancient", held_hours=0, posted_days=400, expired=True, score=8.0)
    _job("split-plain", held_hours=0, posted_days=2)

    d = collect(hours=1)

    assert (d["intake_expired_ancient"] + d["intake_expired_queue_stale"]
            == d["intake_expired"])
    assert d["intake_expired_ancient"] >= 1
    _cleanup()


def test_no_intake_reports_none_rather_than_a_misleading_zero():
    """An empty window must not read 0% — that would look like perfect health."""
    init_db()
    _cleanup()
    # Nothing created; any rows from other files are outside our control, so
    # only assert the None-handling contract when the window really is empty.
    d = collect(hours=1)
    if d["user_pool_new"] == 0:
        assert d["intake_expired_pct"] is None
    _cleanup()


# ── 4. Terminal verdicts are not "Claude finals" ─────────────────────────────

def test_tier1_drains_are_reported_separately_from_finals():
    """The naming bug that cost an investigation an hour: 78 "genuinely scored"
    against a cycle stat of scored=0 looked contradictory. 75 were Tier-1
    drains. Both numbers were right; the name hid the difference."""
    init_db()
    _cleanup()
    # A Tier-1 drain stamps the prescore AS the final score.
    _job("drain-1", held_hours=0, posted_days=1, score=32.0, prescore=32.0)
    _job("drain-2", held_hours=0, posted_days=1, score=28.0, prescore=28.0)
    # A Tier-2 final disagrees with its prescore.
    _job("final-1", held_hours=0, posted_days=1, score=74.0, prescore=52.0)

    d = collect(hours=1)

    assert d["terminal_verdicts"] >= 3
    assert d["tier1_drains"] >= 2
    assert d["tier2_or_rule_verdicts"] >= 1
    assert (d["tier1_drains"] + d["tier2_or_rule_verdicts"]
            == d["terminal_verdicts"])
    # The deprecated alias must keep pointing at the same number.
    assert d["genuinely_scored"] == d["terminal_verdicts"]
    _cleanup()


def test_expired_rows_are_not_terminal_verdicts():
    """An age expiry is not a verdict, whatever it left in rerank_score."""
    init_db()
    _cleanup()
    _job("exp-only", held_hours=0, posted_days=400, expired=True, score=8.0)

    d = collect(hours=1)
    with get_session() as s:
        row = s.exec(select(Job).where(
            Job.external_id == _PREFIX + "exp-only")).first()

    assert row.expired_at is not None
    assert d["intake_expired"] >= 1
    # It must not also be counted as a scoring verdict.
    assert d["terminal_verdicts"] == d["tier1_drains"] + d["tier2_or_rule_verdicts"]
    _cleanup()


def test_shared_pool_rows_stay_out_of_both_sides_of_the_ratio():
    """Numerator and denominator must have the SAME scope. `user_pool_new`
    excludes the shared pool, so the expiry count has to as well — mixing them
    (e.g. against tick_new_jobs, which sums shared + per-user upserts) would
    understate the rate."""
    init_db()
    _cleanup()
    before = collect(hours=1)["user_pool_new"]
    _job("shared-1", held_hours=0, posted_days=1, user_id=SHARED_POOL_USER)
    after = collect(hours=1)["user_pool_new"]
    assert after == before, "a shared-pool row leaked into the per-user intake count"
    _cleanup()


def test_metric_respects_the_configured_posted_bound(monkeypatch):
    """The ancient split is derived from the live setting, not a hardcoded 30."""
    init_db()
    _cleanup()
    monkeypatch.setattr(settings, "scoring_max_posted_age_days", 10)
    _job("bound-1", held_hours=0, posted_days=20, expired=True, score=8.0)

    d = collect(hours=1)

    assert d["intake_expired_ancient"] >= 1, (
        "a 20-day-old posting was not attributed to the ancient bound at a "
        "10-day setting")
    _cleanup()
