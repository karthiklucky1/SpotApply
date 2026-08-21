"""The Kanban board must not read columns it never renders — and must still
read the ones it does.

The five board queries selected whole `Application` + `Job` entities: every
column of both tables for up to ~260 rows (`shortlist_render_cap` plus the four
secondary lists), on the page every user opens first. That is the same egress
path as the /api/jobs list, which is now projected.

`load_only` is used instead of a column projection because these objects are
handed to Jinja and touched through macros and four globals (`salary_of`,
`sponsorship_of`, `liveness_of`, `urgency_of`). A tuple projection turns any
access the list missed into a 500; `load_only` merely defers the column. The
render test below is what proves the list is complete — a deferred column
touched after the session closes raises `DetachedInstanceError`, so a missing
field shows up as a failed render, not as silently wrong HTML.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource

_JD = "We need a Python engineer. Salary $150,000 - $180,000 per year. Visa sponsorship available."


@pytest.fixture()
def seeded():
    now = datetime.utcnow()
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("dashld-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id="dashld-1",
                  company="LoadOnlyCo", title="Backend Engineer", location="Remote",
                  remote=True, url="https://lo.example/1", description=_JD,
                  rerank_score=81.0, blended_score=83.0, hire_probability_score=0.6,
                  ghost_score=0.1, posted_at=now - timedelta(hours=6),
                  first_seen=now - timedelta(hours=6), last_seen=now,
                  rerank_reasoning="Deferred: never rendered on the board",
                  corporate_insights='{"salary": {"text": "$150K-$180K/yr"}}')
        s.add(job)
        s.commit()
        s.refresh(job)
        s.add(Application(user_id=None, job_id=job.id,
                          status=ApplicationStatus.SHORTLISTED, apply_track="autofill"))
        s.commit()
        jid = job.id
    yield jid
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("dashld-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def test_the_board_renders_everything_it_reads(seeded):
    """The completeness check: every macro and global runs against the loaded
    columns. A field missing from the load_only list raises
    DetachedInstanceError here rather than in production."""
    res = _client().get("/dashboard")
    assert res.status_code == 200
    html = res.text
    assert "LoadOnlyCo" in html
    assert "Backend Engineer" in html
    # The drawer body is server-rendered per card, so `description` must stay
    # loaded — this is what stops the projection going one column too far.
    assert "We need a Python engineer" in html
    # And the cached salary parse (corporate_insights) still feeds the chip.
    assert "$150K-$180K/yr" in html


def test_the_columns_the_board_never_renders_are_deferred():
    """The point of the change: unread columns must not be in the SELECT."""
    from sqlmodel import select as _select
    from app.api.server import _dashboard_load_options

    q = (_select(Application, Job).join(Job)
         .options(*_dashboard_load_options())
         .where(Application.status == ApplicationStatus.SHORTLISTED))
    sql = str(q.compile(compile_kwargs={"literal_binds": True})).lower()

    for col in ("job.rerank_reasoning", "job.rerank_breakdown",
                "job.hire_probability_signals", "job.content_hash",
                "job.similarity_score", "job.prescore", "job.cross_source_slug",
                "application.rejection_analysis", "application.notes"):
        assert col not in sql, f"{col} is selected but never rendered"

    for col in ("job.description", "job.corporate_insights", "job.blended_score",
                "job.last_seen", "application.apply_track"):
        assert col in sql, f"{col} IS rendered and must stay loaded"
