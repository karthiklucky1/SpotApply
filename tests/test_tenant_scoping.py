"""Cross-tenant scoping regressions: one user's activity must never consume
another user's daily limits or leak into their answer memory."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import (
    AnswerMemory,
    Application,
    ApplicationStatus,
    Job,
    JobSource,
    PendingQuestion,
)


def _mk_job(session, ext, user_id):
    j = Job(source=JobSource.GREENHOUSE, external_id=ext, company=f"Co-{ext}",
            title=f"Role {ext}", url=f"http://x/{ext}", description="d",
            user_id=user_id)
    session.add(j); session.commit(); session.refresh(j)
    return j


def _mk_submitted_app(session, job):
    a = Application(job_id=job.id, status=ApplicationStatus.SUBMITTED,
                    apply_track="manual", user_id=job.user_id,
                    submitted_at=datetime.utcnow())
    session.add(a); session.commit(); session.refresh(a)
    return a


@pytest.fixture
def _clean():
    def _wipe():
        with get_session() as s:
            for j in s.exec(select(Job).where(Job.external_id.like("ten-%"))).all():
                for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                    for pq in s.exec(select(PendingQuestion).where(PendingQuestion.application_id == a.id)).all():
                        s.delete(pq)
                    s.delete(a)
                s.delete(j)
            for m in s.exec(select(AnswerMemory).where(AnswerMemory.label_normalized.like("ten-%"))).all():
                s.delete(m)
            s.commit()
    _wipe()
    yield
    _wipe()


def test_daily_apply_limit_is_per_user(_clean):
    from app.autofill.agent import _todays_submission_count

    with get_session() as s:
        # user A submitted 3 today; user B submitted nothing.
        for i in range(3):
            _mk_submitted_app(s, _mk_job(s, f"ten-a{i}", "user-a"))

        assert _todays_submission_count(s, "user-a") == 3
        assert _todays_submission_count(s, "user-b") == 0
        # legacy/local rows (user_id NULL) are their own bucket
        assert _todays_submission_count(s, None) == 0


def test_telegram_answer_memory_scoped_to_owner(_clean):
    from app.telegram_bot.bot import _save_answer

    with get_session() as s:
        app_a = _mk_submitted_app(s, _mk_job(s, "ten-qa", "user-a"))
        app_b = _mk_submitted_app(s, _mk_job(s, "ten-qb", "user-b"))
        pq_a = PendingQuestion(application_id=app_a.id, field_label="ten-visa?",
                               field_selector="#v", field_type="text")
        pq_b = PendingQuestion(application_id=app_b.id, field_label="ten-visa?",
                               field_selector="#v", field_type="text")
        s.add(pq_a); s.add(pq_b); s.commit()
        s.refresh(pq_a); s.refresh(pq_b)
        pq_a_id, pq_b_id = pq_a.id, pq_b.id

    asyncio.run(_save_answer(pq_a_id, "Answer from A"))
    asyncio.run(_save_answer(pq_b_id, "Answer from B"))

    with get_session() as s:
        rows = s.exec(select(AnswerMemory).where(AnswerMemory.label_normalized == "ten-visa?")).all()
        by_user = {r.user_id: r.answer for r in rows}
        # Two separate rows, one per owner — no shared/global answer.
        assert by_user == {"user-a": "Answer from A", "user-b": "Answer from B"}
