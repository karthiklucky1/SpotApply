"""Interaction learning: dismissals/applies become per-user ranking signals."""
from __future__ import annotations

from sqlmodel import delete

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.matching.preference_learning import (
    USER_DISMISS_MARKER, build_preference_profile,
)

UID = "u_pref"


def _clean():
    with get_session() as s:
        s.exec(delete(Application))
        s.exec(delete(Job))
        s.commit()


def _job_app(ext, company, title, status, notes=None):
    with get_session() as s:
        j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=ext,
                company=company, title=title, url=f"http://x/{ext}", description="d")
        s.add(j)
        s.commit()
        s.refresh(j)
        s.add(Application(job_id=j.id, status=status, user_id=UID, notes=notes))
        s.commit()


def test_profile_learns_dislikes_and_likes():
    _clean()
    # Two REAL dismissals at BadCo → disliked company.
    _job_app("d1", "BadCo", "Sales Engineer", ApplicationStatus.SKIPPED,
             notes=USER_DISMISS_MARKER)
    _job_app("d2", "BadCo", "Sales Engineer II", ApplicationStatus.SKIPPED,
             notes=USER_DISMISS_MARKER)
    # Engagement at GoodCo → liked company + liked tokens.
    _job_app("g1", "GoodCo", "Machine Learning Platform Engineer",
             ApplicationStatus.SUBMITTED)
    # SYSTEM skip (cap expiry) must NOT count as user opinion.
    _job_app("s1", "NeutralCo", "Backend Engineer", ApplicationStatus.SKIPPED,
             notes="Expired after 41d (>40d cooldown) — slot reopened for newer 'X'.")

    p = build_preference_profile(UID)
    assert "badco" in p.disliked_companies
    assert "goodco" in p.liked_companies
    assert "neutralco" not in p.disliked_companies
    assert p.dismissed_total == 2 and p.engaged_total == 1

    # Ranking adjustments: BadCo sinks, GoodCo floats, neutral untouched.
    assert p.adjustment("BadCo", "Solutions Engineer") < 0
    assert p.adjustment("GoodCo", "ML Engineer") > 0
    assert p.adjustment("SomeCo", "Curator of Antiquities") == 0

    # 'sales' was dismissed twice and never engaged → title-token penalty
    # even at an unrelated company.
    assert p.adjustment("OtherCo", "Sales Engineer") < 0

    # The LLM feedback note mentions what was learned.
    note = p.feedback_note()
    assert "BadCo".lower() in note.lower()


def test_no_history_means_no_signal():
    _clean()
    p = build_preference_profile(UID)
    assert not p.has_signal
    assert p.feedback_note() == ""
    assert p.adjustment("AnyCo", "Any Title") == 0
