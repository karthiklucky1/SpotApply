"""Old/new identity pairs are resolved without deleting anything.

Production dry run 2026-09-26: 19,648 (user, source, raw id) pairs where the
pre-scoping row and its tenant-scoped twin both exist — the in-place repair
skips them forever. `resolve_pairs` closes the redundant copy (reversible
marker), keeps the one carrying an application, and leaves a pair where BOTH
carry applications alone. Synthetic rows, prefixed and cleaned.
"""
from __future__ import annotations

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from scripts.diagnose_job_identity import (SUPERSEDED_MARKER, plan_pairs,
                                           resolve_pairs)

_T = "idpairtest"          # tenant; raw ids are prefixed idp_
UID = "idp_user"


@pytest.fixture()
def rows():
    made = []

    def mk(ext, with_app=False):
        with get_session() as s:
            j = Job(user_id=UID, source=JobSource.WORKDAY, external_id=ext, company="Idp",
                    title="Engineer " + ext, url=f"https://{_T}.wd5.myworkdayjobs.com/x/{ext}",
                    description="d")
            s.add(j)
            s.commit()
            s.refresh(j)
            s.exec(delete(Application).where(Application.job_id == j.id))
            if with_app:
                s.add(Application(job_id=j.id, user_id=UID, status=ApplicationStatus.SUBMITTED))
            s.commit()
            made.append(j.id)
            return j.id
    yield mk
    with get_session() as s:
        s.exec(delete(Application).where(Application.job_id.in_(made)))
        s.exec(delete(Job).where(Job.id.in_(made)))
        s.commit()


def _closed(jid):
    with get_session() as s:
        j = s.get(Job, jid)
        return j.is_closed, j.closed_reason


def test_pairs_are_closed_never_deleted_and_history_is_kept(rows):
    plain_old, plain_new = rows("idp_1"), rows(f"{_T}:idp_1")
    app_old, app_new = rows("idp_2", with_app=True), rows(f"{_T}:idp_2")
    both_old, both_new = rows("idp_3", with_app=True), rows(f"{_T}:idp_3", with_app=True)
    with get_session() as s:
        pairs = [p for p in plan_pairs(s, 100000)
                 if p[0] in (plain_old, plain_new, app_old, app_new, both_old, both_new)
                 or p[3] == "both_have_applications"]
    stats = resolve_pairs(pairs)
    assert _closed(plain_old) == (True, SUPERSEDED_MARKER), "the unscoped copy closes"
    assert _closed(plain_new)[0] is False
    assert _closed(app_old)[0] is False, "the copy with an application keeps its place"
    assert _closed(app_new) == (True, SUPERSEDED_MARKER)
    assert _closed(both_old)[0] is False and _closed(both_new)[0] is False
    assert stats["left_both_have_applications"] >= 1
    with get_session() as s:            # nothing deleted
        n = len(s.exec(select(Job.id).where(Job.user_id == UID)).all())
    assert n == 6


def test_the_documented_rollback_reopens_exactly_the_closed_rows(rows):
    old, new = rows("idp_9"), rows(f"{_T}:idp_9")
    with get_session() as s:
        pairs = [p for p in plan_pairs(s, 100000) if p[0] in (old, new)]
    resolve_pairs(pairs)
    from sqlalchemy import text
    with get_session() as s:
        s.execute(text("UPDATE job SET is_closed = :f, closed_reason = NULL "
                       "WHERE closed_reason = :m AND id = :i"),
                  {"f": False, "m": SUPERSEDED_MARKER, "i": old})
        s.commit()
    assert _closed(old) == (False, None)
