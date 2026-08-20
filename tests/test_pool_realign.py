"""Changing roles re-points the pool; a lightly-edited résumé leaves it alone.

Scores are computed against whatever résumé was in place, and the scoring queue
is `rerank_score IS NULL`, so a scored job is never revisited. That left a user
who moved from AI Engineer to Software Developer with a board ranked against a
CV they no longer use. The rules under test:

* the same roles (a résumé that only gained a line) change NOTHING;
* on-role jobs lose their old score so the lane re-judges them;
* jobs the OLD roles brought in that do not fit the new ones leave the board
  and are never scored — including the unscored ones, which must exit the
  queue without costing a Claude call;
* jobs matching neither role list are ambiguous and left completely alone;
* anything the user or agent already acted on is untouched.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.strategy.realign import (
    OFF_ROLE_PREFIX,
    realign_if_roles_changed,
    realign_pool_to_roles,
    roles_changed,
)

_UID = "realign-user"


def _mk(session, ext, title, score=None, status=None):
    j = Job(source=JobSource.GREENHOUSE, external_id=ext, company="RCo",
            title=title, url=f"http://x/{ext}", description="d",
            user_id=_UID, rerank_score=score)
    session.add(j); session.commit(); session.refresh(j)
    a = None
    if status is not None:
        a = Application(job_id=j.id, status=status, apply_track="manual",
                        user_id=_UID)
        session.add(a); session.commit(); session.refresh(a)
    return j, a


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            for j in s.exec(select(Job).where(Job.user_id == _UID)).all():
                for a in s.exec(select(Application).where(
                        Application.job_id == j.id)).all():
                    s.delete(a)
                s.delete(j)
            s.commit()
    _wipe()
    yield
    _wipe()


# ── When it runs at all ──────────────────────────────────────────────────────

@pytest.mark.parametrize("old,new,expected", [
    (["AI Engineer"], ["AI Engineer"], False),
    (["AI Engineer"], ["ai engineer"], False),            # case only
    (["AI Engineer", "ML Engineer"], ["ML Engineer", "AI Engineer"], False),  # order only
    (["AI Engineer"], ["Software Developer"], True),
    (["AI Engineer"], ["AI Engineer", "ML Engineer"], True),
    ([], ["Software Developer"], True),
])
def test_roles_changed_ignores_case_and_order(old, new, expected):
    assert roles_changed(old, new) is expected


def test_a_lightly_edited_resume_leaves_the_board_alone():
    """Same roles → the user's board must not move at all."""
    with get_session() as s:
        _mk(s, "ra-1", "AI Engineer", score=88.0,
            status=ApplicationStatus.SHORTLISTED)

    out = realign_if_roles_changed(_UID, ["AI Engineer"], ["AI Engineer"])
    assert out == {"skipped": "roles unchanged"}

    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-1")).first()
        app = s.exec(select(Application).where(
            Application.job_id == job.id)).first()
        assert job.rerank_score == 88.0, "an unchanged role list must not re-score"
        assert app.status == ApplicationStatus.SHORTLISTED


# ── What it does to each job ─────────────────────────────────────────────────

def test_on_role_jobs_are_requeued_for_rescoring():
    """Their score came from the OLD résumé, so it has to be re-earned."""
    with get_session() as s:
        _mk(s, "ra-on", "Senior Software Developer", score=71.0,
            status=ApplicationStatus.SHORTLISTED)

    realign_pool_to_roles(_UID, ["Software Developer"], old_roles=["AI Engineer"])

    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-on")).first()
        assert job.rerank_score is None, (
            "an on-role job must go back on the scoring queue so the new "
            "résumé decides its score")


def test_old_role_jobs_leave_the_board_but_stay_in_the_pool():
    with get_session() as s:
        _mk(s, "ra-off", "AI Engineer", score=80.0,
            status=ApplicationStatus.SHORTLISTED)

    stats = realign_pool_to_roles(_UID, ["Software Developer"],
                                  old_roles=["AI Engineer"])

    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-off")).first()
        app = s.exec(select(Application).where(
            Application.job_id == job.id)).first()
        assert job is not None, "the posting stays in the pool — nothing is deleted"
        assert app.status == ApplicationStatus.SKIPPED
        assert "target roles" in (app.notes or "")
    assert stats["unshortlisted"] == 1


def test_unscored_off_role_jobs_are_parked_instead_of_scored():
    """The budget rule: an off-role job must never cost a Claude call.

    The scoring lane's work list is `rerank_score IS NULL`, so leaving one
    unscored would queue it. Parking stamps a marker score to take it out.
    """
    with get_session() as s:
        _mk(s, "ra-off-new", "AI Engineer", score=None)

    stats = realign_pool_to_roles(_UID, ["Software Developer"],
                                  old_roles=["AI Engineer"])

    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-off-new")).first()
        assert job.rerank_score is not None, (
            "an off-role job left unscored would be picked up by the scoring "
            "lane and charged to the user's daily finals")
        assert job.rerank_reasoning.startswith(OFF_ROLE_PREFIX)
    assert stats["parked"] == 1


def test_unscored_on_role_jobs_are_left_on_the_queue():
    """Already unscored + on-role: nothing to do, the lane will get to it."""
    with get_session() as s:
        _mk(s, "ra-on-new", "Software Developer II", score=None)

    stats = realign_pool_to_roles(_UID, ["Software Developer"],
                                  old_roles=["AI Engineer"])

    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-on-new")).first()
        assert job.rerank_score is None, "still queued, and it will be judged new"
    assert stats["rescore"] == 0 and stats["parked"] == 0


def test_jobs_matching_neither_role_list_are_left_completely_alone():
    """The conservative rule that protects relevant work.

    role_title_match keys off distinctive domain tokens, so plenty of genuinely
    relevant titles ("Backend Developer" for a Software Developer user) match
    NEITHER list. Parking those would hide real jobs, so they are not touched:
    a wrong park costs the user a job they never see, a wrong keep costs one
    cheap score.
    """
    from app.discovery.title_filter import role_title_match
    assert not role_title_match("Backend Developer", ["Software Developer"])
    assert not role_title_match("Backend Developer", ["AI Engineer"])

    with get_session() as s:
        _mk(s, "ra-amb", "Backend Developer", score=77.0,
            status=ApplicationStatus.SHORTLISTED)
        _mk(s, "ra-amb2", "Backend Developer", score=None)

    stats = realign_pool_to_roles(_UID, ["Software Developer"],
                                  old_roles=["AI Engineer"])

    with get_session() as s:
        scored = s.exec(select(Job).where(Job.external_id == "ra-amb")).first()
        unscored = s.exec(select(Job).where(Job.external_id == "ra-amb2")).first()
        app = s.exec(select(Application).where(
            Application.job_id == scored.id)).first()
        assert scored.rerank_score == 77.0, "ambiguous jobs keep their score"
        assert unscored.rerank_score is None, "and stay on the scoring queue"
        assert app.status == ApplicationStatus.SHORTLISTED, "and stay on the board"
    assert stats == {"rescore": 0, "parked": 0, "unshortlisted": 0,
                     "unparked": 0, "protected": 0, "capped": 0}


# ── What it must never touch ─────────────────────────────────────────────────

@pytest.mark.parametrize("status", [
    ApplicationStatus.TAILORED,
    ApplicationStatus.AUTOFILLED,
    ApplicationStatus.AWAITING_USER,
    ApplicationStatus.READY_TO_SUBMIT,
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.INTERVIEWING,
    ApplicationStatus.OFFER,
    ApplicationStatus.ACCEPTED,
])
def test_applications_in_flight_are_never_disturbed(status):
    """Changing roles must not touch an application the user is part-way through
    — even when the job is now completely off-role."""
    with get_session() as s:
        _mk(s, "ra-live", "AI Engineer", score=90.0, status=status)

    stats = realign_pool_to_roles(_UID, ["Software Developer"],
                                  old_roles=["AI Engineer"])

    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-live")).first()
        app = s.exec(select(Application).where(
            Application.job_id == job.id)).first()
        assert app.status == status, "work in flight must survive a role change"
        assert job.rerank_score == 90.0
    assert stats["protected"] == 1


def test_an_empty_role_list_never_blanks_a_board():
    """No roles = no opinion. Never mass-skip on an empty list."""
    with get_session() as s:
        _mk(s, "ra-empty", "AI Engineer", score=88.0,
            status=ApplicationStatus.SHORTLISTED)

    stats = realign_pool_to_roles(_UID, [])

    with get_session() as s:
        app = s.exec(select(Application).join(Job).where(
            Job.external_id == "ra-empty")).first()
        assert app.status == ApplicationStatus.SHORTLISTED
    assert stats == {"rescore": 0, "parked": 0, "unshortlisted": 0,
                     "unparked": 0, "protected": 0, "capped": 0}


def test_roles_changing_back_re_judges_the_parked_jobs():
    """Parking is reversible: the marker score is cleared when the job is
    on-role again, so the pool never needs re-scraping."""
    with get_session() as s:
        _mk(s, "ra-back", "AI Engineer", score=None)

    realign_pool_to_roles(_UID, ["Software Developer"],
                          old_roles=["AI Engineer"])         # parks it
    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-back")).first()
        assert job.rerank_reasoning.startswith(OFF_ROLE_PREFIX)

    realign_pool_to_roles(_UID, ["AI Engineer"],
                          old_roles=["Software Developer"])  # back on-role
    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-back")).first()
        assert job.rerank_score is None, (
            "a parked job must return to the scoring queue when the user's "
            "roles cover it again")


def test_a_parked_job_can_return_to_the_board_when_roles_change_back():
    """Parking sets a SKIPPED application, and the re-shortlist backstop skips
    ANY job that has an application — so without undoing our own skip the job
    would be re-scored and still never reach the board again."""
    with get_session() as s:
        _mk(s, "ra-round", "AI Engineer", score=88.0,
            status=ApplicationStatus.SHORTLISTED)

    realign_pool_to_roles(_UID, ["Software Developer"], old_roles=["AI Engineer"])
    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-round")).first()
        app = s.exec(select(Application).where(Application.job_id == job.id)).first()
        assert app.status == ApplicationStatus.SKIPPED

    stats = realign_pool_to_roles(_UID, ["AI Engineer"],
                                  old_roles=["Software Developer"])

    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-round")).first()
        app = s.exec(select(Application).where(Application.job_id == job.id)).first()
        assert app is None, (
            "our own park must be undone, or the re-shortlist backstop will "
            "never put this job back on the board")
        assert job.rerank_score is None, "and it is re-judged on the new résumé"
    assert stats["unparked"] == 1


def test_a_job_the_user_skipped_stays_skipped():
    """Only OUR park is reversible — an explicit decline is not undone."""
    with get_session() as s:
        j, a = _mk(s, "ra-userskip", "Software Developer", score=88.0,
                   status=ApplicationStatus.SKIPPED)
        a.notes = "Not interested — bad location"
        s.add(a); s.commit()

    stats = realign_pool_to_roles(_UID, ["Software Developer"],
                                  old_roles=["AI Engineer"])

    with get_session() as s:
        job = s.exec(select(Job).where(Job.external_id == "ra-userskip")).first()
        app = s.exec(select(Application).where(Application.job_id == job.id)).first()
        assert app is not None and app.status == ApplicationStatus.SKIPPED
    assert stats["unparked"] == 0


def test_rescore_volume_is_capped(monkeypatch):
    """One role change must not queue an unbounded backlog of paid re-scores."""
    from app.config import settings
    monkeypatch.setattr(settings, "realign_max_rescore", 2)
    with get_session() as s:
        for i in range(5):
            _mk(s, f"ra-cap-{i}", "Software Developer", score=70.0 + i)

    stats = realign_pool_to_roles(_UID, ["Software Developer"],
                                  old_roles=["AI Engineer"])

    assert stats["rescore"] == 2
    assert stats["capped"] == 3, "the rest keep their score and are reported"
