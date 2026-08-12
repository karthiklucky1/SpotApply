"""Blanking stale JD text — and the three things it must never touch.

`description` was 3.3 GB of a 5.76 GB database (~5.8 KB across 1.1M rows), more
than half the disk, while the funnel only ever looks at postings from the last
5 days. Stripping it is the biggest single saving available; getting the
carve-outs wrong is how it becomes data loss.
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.strategy.job_retention import strip_dead_descriptions

_TAG = "striptest-"
_BIG = "x" * 4000


def _mk(session, ext, *, age_days, score=None, reasoning=None, desc=_BIG):
    j = Job(source=JobSource.LINKEDIN, external_id=f"{_TAG}{ext}", company="StripCo",
            title=f"Role {ext}", url=f"http://x/{_TAG}{ext}", description=desc,
            rerank_score=score, rerank_reasoning=reasoning)
    session.add(j); session.commit(); session.refresh(j)
    j.first_seen = datetime.utcnow() - timedelta(days=age_days)
    j.discovered_at = j.first_seen
    session.add(j); session.commit(); session.refresh(j)
    return j


@pytest.fixture
def clean():
    def _wipe():
        with get_session() as s:
            for j in s.exec(select(Job).where(Job.external_id.like(f"{_TAG}%"))).all():
                for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                    s.delete(a)
                s.delete(j)
            s.commit()
    _wipe()
    yield
    _wipe()


def _desc(ext):
    with get_session() as s:
        j = s.exec(select(Job).where(Job.external_id == f"{_TAG}{ext}")).first()
        return j.description


def test_stale_unscored_description_is_blanked(clean):
    with get_session() as s:
        _mk(s, "old-unscored", age_days=30)
    assert strip_dead_descriptions(days=14) >= 1
    assert _desc("old-unscored") == ""


def test_fresh_jobs_are_untouched(clean):
    """The funnel still needs these — they are inside the scoring window."""
    with get_session() as s:
        _mk(s, "fresh", age_days=2)
    strip_dead_descriptions(days=14)
    assert _desc("fresh") == _BIG


def test_a_scored_job_keeps_its_text_for_training(clean):
    """(description, rerank_score) pairs are the distillation training set."""
    with get_session() as s:
        _mk(s, "scored", age_days=90, score=82.0, reasoning="Strong match on Python + ML")
    strip_dead_descriptions(days=14)
    assert _desc("scored") == _BIG, (
        "stripping a REAL score's JD destroys the local-scorer training corpus, "
        "which is what keeps scoring alive when credits run out"
    )


def test_expired_unscored_stamp_is_not_treated_as_a_real_score(clean):
    """Score 8 from the staleness stamp is a placeholder, not a judgement."""
    with get_session() as s:
        _mk(s, "stamped", age_days=30, score=8.0,
            reasoning="Expired unscored (older than 5d before scoring caught up — too stale to apply)")
    strip_dead_descriptions(days=14)
    assert _desc("stamped") == ""


def test_a_job_with_an_application_is_never_touched(clean):
    """The user acted on it — skill-gap and re-tailoring read the JD back."""
    with get_session() as s:
        j = _mk(s, "applied", age_days=120)
        s.add(Application(job_id=j.id, status=ApplicationStatus.SUBMITTED,
                          apply_track="manual"))
        s.commit()
    strip_dead_descriptions(days=14)
    assert _desc("applied") == _BIG


def test_already_blank_rows_are_not_rewritten(clean):
    """Re-running must converge, not churn the same rows every cycle."""
    with get_session() as s:
        _mk(s, "blank", age_days=30, desc="")
    assert strip_dead_descriptions(days=14) == 0


def test_zero_disables(clean):
    with get_session() as s:
        _mk(s, "disabled", age_days=365)
    assert strip_dead_descriptions(days=0) == 0
    assert _desc("disabled") == _BIG
