"""mark_ghost_jobs regression tests — added with the projection rewrite that
fixed the per-board full-row scan (recurring Supabase statement timeouts).
Behavior must stay identical: close jobs missing from the board, skip their
active applications, never close on an empty fetch, never cross tenants."""
from __future__ import annotations

from sqlmodel import delete, select

from app.db.init_db import get_session, init_db
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.discovery.pipeline import mark_ghost_jobs


def _seed(session, user_id, ext, company="Acme", closed=False):
    job = Job(source=JobSource.GREENHOUSE, external_id=ext, company=company,
              title=f"Role {ext}", url=f"https://x.test/{ext}",
              user_id=user_id, is_closed=closed)
    session.add(job)
    session.commit()
    return job.id


def _cleanup():
    with get_session() as session:
        session.exec(delete(Application))
        session.exec(delete(Job))
        session.commit()


def test_closes_missing_jobs_and_skips_their_applications():
    init_db()
    _cleanup()
    with get_session() as session:
        gone = _seed(session, "u1", "gone-1")
        stays = _seed(session, "u1", "stays-1")
        session.add(Application(job_id=gone, user_id="u1",
                                status=ApplicationStatus.SHORTLISTED))
        session.commit()

    mark_ghost_jobs("greenhouse", "Acme", ["stays-1"], user_id="u1")

    with get_session() as session:
        j_gone = session.get(Job, gone)
        j_stays = session.get(Job, stays)
        assert j_gone.is_closed and "Removed from company" in j_gone.closed_reason
        assert not j_stays.is_closed
        app = session.exec(select(Application).where(Application.job_id == gone)).first()
        assert app.status == ApplicationStatus.SKIPPED
    _cleanup()


def test_submitted_applications_are_never_skipped():
    init_db()
    _cleanup()
    with get_session() as session:
        gone = _seed(session, "u1", "gone-2")
        session.add(Application(job_id=gone, user_id="u1",
                                status=ApplicationStatus.SUBMITTED))
        session.commit()
    mark_ghost_jobs("greenhouse", "Acme", ["other"], user_id="u1")
    with get_session() as session:
        app = session.exec(select(Application).where(Application.job_id == gone)).first()
        assert app.status == ApplicationStatus.SUBMITTED   # history preserved
    _cleanup()


def test_empty_active_list_never_closes_anything():
    init_db()
    _cleanup()
    with get_session() as session:
        jid = _seed(session, "u1", "keep-1")
    mark_ghost_jobs("greenhouse", "Acme", [], user_id="u1")
    with get_session() as session:
        assert not session.get(Job, jid).is_closed
    _cleanup()


def test_tenant_scoping_never_closes_other_users_jobs():
    init_db()
    _cleanup()
    with get_session() as session:
        mine = _seed(session, "u1", "shared-ext")
        theirs = _seed(session, "u2", "shared-ext")
    mark_ghost_jobs("greenhouse", "Acme", ["something-else"], user_id="u1")
    with get_session() as session:
        assert session.get(Job, mine).is_closed
        assert not session.get(Job, theirs).is_closed
    _cleanup()
