"""The board's "already tailored" state, and the one case it must NOT claim.

`tailored_resume_path` / `cover_letter_path` are written by tailor.py BEFORE it
branches on grounding, so an application blocked at ERROR has both paths set
while `/application/{id}/details` withholds the text and `/download-resume`
409s. Every surface used to branch on those paths, which offered a "View Docs"
button for a document that does not open.

`Application.tailored_at` is stamped only in the delivering branch, and these
tests pin that distinction on the API, the projection and the rendered board.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource

_PREFIX = "tstate-"          # every row this file makes; deleted by this prefix
_USER = "tailored-state-user"


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _mkjob(session, ext: str, title: str) -> Job:
    job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id=_PREFIX + ext,
              company="TState " + ext, title=title, url=f"https://x/{ext}",
              description="a backend role", rerank_score=80, blended_score=80,
              posted_at=datetime.utcnow() - timedelta(hours=2),
              first_seen=datetime.utcnow() - timedelta(hours=2))
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@pytest.fixture()
def rows():
    """One delivered application, one blocked one, one never tailored."""
    made = {}
    with get_session() as s:
        for ext, title, status, tailored_at in (
            ("done", "Delivered Role", ApplicationStatus.TAILORED, datetime.utcnow()),
            ("blocked", "Blocked Role", ApplicationStatus.ERROR, None),
            ("none", "Untailored Role", ApplicationStatus.SHORTLISTED, None),
        ):
            job = _mkjob(s, ext, title)
            app_row = Application(
                user_id=None, job_id=job.id, status=status,
                # BOTH blocked and delivered carry paths — that is the point.
                tailored_resume_path=(None if ext == "none"
                                      else f"/tmp/{_PREFIX}{ext}/resume.docx"),
                cover_letter_path=(None if ext == "none"
                                   else f"/tmp/{_PREFIX}{ext}/cover.txt"),
                tailored_at=tailored_at,
            )
            s.add(app_row)
            s.commit()
            s.refresh(app_row)
            made[ext] = (job.id, app_row.id)
    yield made
    with get_session() as s:
        job_ids = [j for j, _ in made.values()]
        if job_ids:
            s.exec(delete(Application).where(Application.job_id.in_(job_ids)))
            s.exec(delete(Job).where(Job.id.in_(job_ids)))
        s.commit()


def test_details_reports_delivered_documents_as_tailored(client, rows):
    _, app_id = rows["done"]
    d = client.get(f"/application/{app_id}/details").json()
    assert d["tailored"] is True
    assert d["blocked"] is False
    assert d["tailored_at"] is not None


def test_details_never_claims_a_blocked_draft_is_tailored(client, rows):
    """The regression this column exists for.

    The paths are on the row, so the old `has_docs` check said "tailored" and
    the board offered View Docs — for a document /details withholds and
    /download-resume refuses with 409.
    """
    _, app_id = rows["blocked"]
    d = client.get(f"/application/{app_id}/details").json()
    assert d["tailored"] is False, "a grounding-blocked draft is not a delivered one"
    assert d["blocked"] is True
    assert d["tailored_at"] is None


def test_details_reports_an_untailored_application(client, rows):
    _, app_id = rows["none"]
    d = client.get(f"/application/{app_id}/details").json()
    assert d["tailored"] is False
    assert d["blocked"] is False


def test_api_jobs_exposes_tailored_at_for_the_explorer_rows(client, rows):
    """The All Jobs table builds its actions from this and had no such field."""
    seen = {}
    for page in range(1, 12):
        data = client.get(f"/api/jobs?page={page}&limit=100&max_age_days=0").json()
        for j in data.get("jobs", []):
            if str(j.get("title", "")).endswith(" Role") and j.get("application"):
                seen[j["title"]] = j["application"]
        if page >= (data.get("pages") or 1):
            break
    assert "Delivered Role" in seen, "seeded rows should be reachable from /api/jobs"
    assert seen["Delivered Role"]["tailored_at"] is not None
    assert seen["Blocked Role"]["tailored_at"] is None
    assert seen["Untailored Role"]["tailored_at"] is None


def test_tailoring_stamps_tailored_at_only_when_it_delivers():
    """Pin the writer, not just the readers.

    tailor.py sets the two paths unconditionally and then branches; only the
    branch that reaches ApplicationStatus.TAILORED may stamp tailored_at.
    """
    import inspect

    from app.tailoring import tailor as _tailor

    src = inspect.getsource(_tailor)
    assert "app.tailored_at = datetime.utcnow()" in src, (
        "tailored_at is no longer stamped — the board cannot tell a delivered "
        "resume from a blocked one without it"
    )
    # The stamp must sit in the TAILORED branch. Everything between the last
    # `status = ApplicationStatus.ERROR` and the stamp would be the error path.
    stamp = src.index("app.tailored_at = datetime.utcnow()")
    delivered = src.index("app.status = ApplicationStatus.TAILORED")
    assert 0 < stamp - delivered < 400, (
        "tailored_at must be stamped beside `status = TAILORED`, not on a path "
        "that also runs when grounding blocks the draft"
    )


def test_board_macros_branch_on_tailored_at_not_the_document_paths():
    """The template is where the bug was visible, so guard it there too."""
    from pathlib import Path

    html = Path(__file__).resolve().parent.parent / "app" / "templates" / "dashboard.html"
    src = html.read_text()
    assert "app.tailored_resume_path or app.cover_letter_path" not in src, (
        "a board surface is back to deriving 'tailored' from the document "
        "paths, which are also written for drafts blocked at ERROR"
    )
    # Both surfaces (drawer macro + shortlist row) compute the same tri-state.
    assert src.count("'blocked' if app.status.value == 'error' else "
                     "('done' if app.tailored_at else 'none')") == 2
    # The re-tailor prompt is gated on data, not on the button's visible text —
    # renaming the label must not silently disable it.
    assert "btn.dataset.tailorAgain === '1'" in src
    assert "/re-tailor/i.test" not in src


def test_legacy_delivered_rows_are_backfilled_not_regressed():
    """A row tailored BEFORE tailored_at existed must still read as tailored.

    The column ships as a bare nullable ALTER, and the board moved its
    "already tailored" test onto it. With no backfill every application
    delivered before the deploy loses its View Documents button and offers a
    fresh paid Tailor for documents already on disk — the same bare-ALTER
    regression app/common/freshness.py records for first_seen.
    """
    from app.db.init_db import init_db

    with get_session() as s:
        job = _mkjob(s, "legacy", "Legacy Delivered Role")
        legacy = Application(
            user_id=None, job_id=job.id, status=ApplicationStatus.TAILORED,
            tailored_resume_path=f"/tmp/{_PREFIX}legacy/resume.docx",
            cover_letter_path=f"/tmp/{_PREFIX}legacy/cover.txt",
            tailored_at=None,          # the pre-migration state
        )
        blocked = Application(
            user_id=None, job_id=job.id, status=ApplicationStatus.ERROR,
            tailored_resume_path=f"/tmp/{_PREFIX}legacy/blocked.docx",
            cover_letter_path=f"/tmp/{_PREFIX}legacy/blocked.txt",
            tailored_at=None,
        )
        s.add(legacy)
        s.add(blocked)
        s.commit()
        s.refresh(legacy)
        s.refresh(blocked)
        legacy_id, blocked_id, job_id = legacy.id, blocked.id, job.id
    try:
        init_db()   # idempotent; this is what runs on every boot
        with get_session() as s:
            assert s.get(Application, legacy_id).tailored_at is not None, (
                "a delivered application predating the column was not backfilled"
            )
            assert s.get(Application, blocked_id).tailored_at is None, (
                "a draft blocked at ERROR has document paths too — backfilling it "
                "would advertise a resume the API refuses to serve"
            )
    finally:
        with get_session() as s:
            s.exec(delete(Application).where(Application.job_id == job_id))
            s.exec(delete(Job).where(Job.id == job_id))
            s.commit()


def test_drawer_reapplies_tailor_state_after_its_template_is_recloned():
    """The reported bug: tailor, close the drawer, reopen — button was back to
    "Tailor". openJobDrawer re-clones an inert <template> on every open and
    closeJobDrawer wipes the live DOM, so state has to be re-applied from data.
    """
    from pathlib import Path

    html = Path(__file__).resolve().parent.parent / "app" / "templates" / "dashboard.html"
    src = html.read_text()
    assert "if (appId && _tailorState[appId]) applyTailorState(appId, _tailorState[appId]);" in src, (
        "openJobDrawer no longer re-applies tailored state to the freshly "
        "cloned body — the View Documents button will vanish on reopen again"
    )
