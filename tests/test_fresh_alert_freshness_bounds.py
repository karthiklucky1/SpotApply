"""The alert gate must obey the TWO-BOUND freshness rule, not ``posted_at`` alone.

``app/common/freshness.py`` exists because one expression —
``coalesce(posted_at, first_seen, discovered_at)`` — conflated two different
ages and let an unreliable SOURCE date terminate a posting we had only just
found. Every other consumer of a freshness question was migrated to that
module's two bounds (KNOWN age tight, POSTED age loose). ``fresh_alerts.py``
was not: it still asked ``posted_at or first_seen``, so ``posted_at`` won.

That is the whole of P0. A shortlisted job we discovered ten minutes ago, whose
ATS claims it went up three days back, produced NO alert — and per the module's
own production post-mortem the median detection lag was ~91.5h, so almost every
shortlist looked "old" through that gate. Production measured 2 shortlists and
0 fresh alerts.

These tests are written against the two-bound contract and the honesty rule
that comes with it: we only claim a POSTING date when we actually trust one.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import FunnelEvent, Job, JobSource, UserNotification

PREFIX = "fab-"


def _clean():
    """Only this file's rows — a wholesale delete takes out other files' fixtures."""
    with get_session() as session:
        jobs = session.exec(select(Job).where(Job.external_id.like(f"{PREFIX}%"))).all()
        ids = [j.id for j in jobs]
        if ids:
            session.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(ids)))
            session.exec(delete(UserNotification).where(
                UserNotification.link.in_([f"/dashboard?fresh_job={i}" for i in ids])))
            session.exec(delete(Job).where(Job.id.in_(ids)))
        session.commit()


def _mk(i, *, known_age_h, posted_age_h=None, score=80.0, source=JobSource.LEVER):
    """A job we FOUND ``known_age_h`` ago, which the source dates ``posted_age_h`` ago."""
    now = datetime.utcnow()
    with get_session() as session:
        job = Job(
            user_id=None, source=source, external_id=f"{PREFIX}{i}", company=f"Co{i}",
            title="Backend Engineer", url=f"https://jobs.lever.co/co{i}/x",
            description="jd", rerank_score=score, blended_score=score,
            first_seen=now - timedelta(hours=known_age_h),
            discovered_at=now - timedelta(hours=known_age_h),
            posted_at=None if posted_age_h is None
            else now - timedelta(hours=posted_age_h),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job.id


def _notes():
    with get_session() as session:
        return session.exec(select(UserNotification).where(
            UserNotification.type == "fresh_job")).all()


# ── The regression itself ────────────────────────────────────────────────────

def test_just_found_job_with_an_older_source_date_still_alerts():
    """THE P0 REGRESSION.

    Row 1 of the worked table in app/common/freshness.py: "found today, source
    says 6d old -> FRESH". Under the old ``posted_at or first_seen`` gate this
    job produced no alert, which is how 2 shortlists became 0 alerts.
    """
    _clean()
    jid = _mk(1, known_age_h=0.2, posted_age_h=72)   # found 12 min ago, source says 3d
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    assert dispatch_fresh_alerts("local", [jid]) == 1
    notes = _notes()
    assert len(notes) == 1
    # ...and it must NOT claim the posting is 12 minutes old.
    assert "posted 12m ago" not in notes[0].message
    assert "found" in notes[0].message.lower()
    _clean()


def test_held_unscored_for_days_does_not_alert():
    """The KNOWN bound still binds. A posting we have sat on for two days has
    missed the 'be first to apply' moment, however fresh the source calls it."""
    _clean()
    jid = _mk(2, known_age_h=48, posted_age_h=1)     # found 2d ago, source says 1h
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    assert dispatch_fresh_alerts("local", [jid]) == 0
    assert _notes() == []
    _clean()


def test_evergreen_posting_does_not_alert():
    """Row 5 of the same table — the behaviour the loose bound must not lose.
    Found just now, but the source dates it well past the posted bound."""
    _clean()
    old_days = int(settings.fresh_alert_max_posted_age_days) + 10
    jid = _mk(3, known_age_h=0.1, posted_age_h=24 * old_days)
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    assert dispatch_fresh_alerts("local", [jid]) == 0
    assert _notes() == []
    _clean()


def test_undated_posting_alerts_on_known_age():
    """No posted_at at all is the common feed case; it must not block the alert."""
    _clean()
    jid = _mk(4, known_age_h=0.5, posted_age_h=None)
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    assert dispatch_fresh_alerts("local", [jid]) == 1
    assert len(_notes()) == 1
    _clean()


# ── Honesty: only claim a posting date we actually trust ─────────────────────

def test_genuinely_fresh_posting_keeps_the_be_first_copy():
    """When the source date IS recent we still make the strong claim."""
    _clean()
    jid = _mk(5, known_age_h=0.2, posted_age_h=2)
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    assert dispatch_fresh_alerts("local", [jid]) == 1
    note = _notes()[0]
    assert "posted 2h ago" in note.message
    assert "first" in note.title.lower()
    _clean()


def test_untrusted_posting_date_does_not_claim_to_be_first():
    """An older source date must not be sold as 'be one of the first'."""
    _clean()
    jid = _mk(6, known_age_h=0.2, posted_age_h=96)
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    assert dispatch_fresh_alerts("local", [jid]) == 1
    note = _notes()[0]
    assert "first" not in note.title.lower()
    assert "4d ago" in note.message      # the source's claim, stated as theirs
    _clean()


def test_latency_metric_only_counts_trusted_posting_dates():
    """``median_post_to_alert_min`` advertises a POST-to-alert number. An alert
    fired on known age has no trustworthy posted reference, so it must be
    counted as an alert but excluded from that median."""
    _clean()
    trusted = _mk(7, known_age_h=0.2, posted_age_h=3)
    untrusted = _mk(8, known_age_h=0.2, posted_age_h=120)
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    assert dispatch_fresh_alerts("local", [trusted, untrusted]) == 2
    with get_session() as session:
        events = session.exec(select(FunnelEvent).where(
            FunnelEvent.stage == "fresh_alert",
            FunnelEvent.job_id.in_([trusted, untrusted]))).all()
    assert len(events) == 2
    meta = {e.job_id: json.loads(e.metadata_json or "{}") for e in events}
    # Both are alerts; both carry latency_min so the alert COUNT stays right.
    assert all("latency_min" in m for m in meta.values())
    assert meta[trusted]["posted_trusted"] is True
    assert meta[untrusted]["posted_trusted"] is False
    # And the known-age clock is always recorded.
    assert all(isinstance(m.get("known_latency_min"), int) for m in meta.values())
    _clean()
