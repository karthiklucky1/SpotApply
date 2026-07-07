"""Company-cap + 40-day cooldown rules (job-based, not company-based).

Verifies _check_and_enforce_company_cap:
  • Counts SUBMITTED / INTERVIEWING toward the active cap (the original leak).
  • Blocks a 3rd role while the company is at the cap and within cooldown.
  • Reopens a slot once an existing application is >= 40 days old.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, Application, ApplicationStatus
from app.matching.pipeline import _check_and_enforce_company_cap, _COMPANY_COOLDOWN_DAYS


@pytest.fixture(autouse=True)
def _mock_company_cap():
    from unittest.mock import patch
    from app.config import settings
    with patch.object(settings, "company_cap", 2):
        yield


def _mk_job(session, ext, company="CapCo", score=80, user_id=None):
    j = Job(source=JobSource.LINKEDIN, external_id=ext, company=company,
            title=f"Role {ext}", url=f"http://x/{ext}", description="d",
            rerank_score=score, user_id=user_id)
    session.add(j); session.commit(); session.refresh(j)
    return j


def _mk_app(session, job, status, age_days=0):
    a = Application(job_id=job.id, status=status, apply_track="manual",
                    user_id=job.user_id)
    session.add(a); session.commit(); session.refresh(a)
    if age_days:
        a.created_at = datetime.utcnow() - timedelta(days=age_days)
        a.submitted_at = a.created_at if status == ApplicationStatus.SUBMITTED else None
        session.add(a); session.commit()
    return a


@pytest.fixture
def _clean():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("cap-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()
    yield
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("cap-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_submitted_counts_toward_cap(_clean):
    """Two SUBMITTED apps must block a 3rd fresh role (the original bug)."""
    with get_session() as s:
        j1 = _mk_job(s, "cap-1"); j2 = _mk_job(s, "cap-2")
        _mk_app(s, j1, ApplicationStatus.SUBMITTED, age_days=5)
        _mk_app(s, j2, ApplicationStatus.SUBMITTED, age_days=5)
        j3 = _mk_job(s, "cap-3")
        allowed = _check_and_enforce_company_cap(s, j3, j3.rerank_score)
        assert allowed is False


def test_under_cap_allows(_clean):
    with get_session() as s:
        j1 = _mk_job(s, "cap-1")
        _mk_app(s, j1, ApplicationStatus.SHORTLISTED, age_days=1)
        j2 = _mk_job(s, "cap-2")
        assert _check_and_enforce_company_cap(s, j2, j2.rerank_score) is True


def test_cooldown_expiry_reopens_slot(_clean):
    """Once an existing app ages past 40 days it expires and a slot reopens."""
    with get_session() as s:
        j1 = _mk_job(s, "cap-1"); j2 = _mk_job(s, "cap-2")
        old = _mk_app(s, j1, ApplicationStatus.SUBMITTED, age_days=_COMPANY_COOLDOWN_DAYS + 5)
        _mk_app(s, j2, ApplicationStatus.SUBMITTED, age_days=_COMPANY_COOLDOWN_DAYS + 2)
        j3 = _mk_job(s, "cap-3")
        allowed = _check_and_enforce_company_cap(s, j3, j3.rerank_score)
        s.commit()
        assert allowed is True
        # the oldest app should now be SKIPPED (expired)
        refreshed = s.get(Application, old.id)
        assert refreshed.status == ApplicationStatus.SKIPPED
        assert "Expired" in (refreshed.notes or "")


def test_at_cap_within_cooldown_blocks(_clean):
    with get_session() as s:
        j1 = _mk_job(s, "cap-1"); j2 = _mk_job(s, "cap-2")
        _mk_app(s, j1, ApplicationStatus.SHORTLISTED, age_days=10)
        _mk_app(s, j2, ApplicationStatus.SUBMITTED, age_days=20)
        j3 = _mk_job(s, "cap-3")
        assert _check_and_enforce_company_cap(s, j3, j3.rerank_score) is False


def test_cap_is_per_user(_clean):
    """User A maxing out a company must not consume user B's slots."""
    with get_session() as s:
        ja1 = _mk_job(s, "cap-a1", user_id="user-a")
        ja2 = _mk_job(s, "cap-a2", user_id="user-a")
        _mk_app(s, ja1, ApplicationStatus.SUBMITTED, age_days=5)
        _mk_app(s, ja2, ApplicationStatus.SUBMITTED, age_days=5)

        # user A is at the cap for CapCo…
        ja3 = _mk_job(s, "cap-a3", user_id="user-a")
        assert _check_and_enforce_company_cap(s, ja3, ja3.rerank_score) is False

        # …but user B still has both slots free at the same company.
        jb1 = _mk_job(s, "cap-b1", user_id="user-b")
        assert _check_and_enforce_company_cap(s, jb1, jb1.rerank_score) is True


def test_cap_legacy_rows_do_not_block_tenants(_clean):
    """Old single-user rows (user_id NULL) must not count against real tenants."""
    with get_session() as s:
        j1 = _mk_job(s, "cap-1"); j2 = _mk_job(s, "cap-2")   # user_id=None
        _mk_app(s, j1, ApplicationStatus.SUBMITTED, age_days=5)
        _mk_app(s, j2, ApplicationStatus.SUBMITTED, age_days=5)

        jb = _mk_job(s, "cap-b9", user_id="user-b")
        assert _check_and_enforce_company_cap(s, jb, jb.rerank_score) is True
