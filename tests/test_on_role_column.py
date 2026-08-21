"""`Job.on_role` — the board's role filter, answered from the row.

"My roles" is ON by default in the Explorer and used to be re-derived per
request: ~20 `title ILIKE '%term%'` predicates OR'd together, run TWICE (page +
pagination count), unindexable because of the leading wildcard. Live numbers:
665ms without the filter, 1,600-1,900ms with it, on every keystroke.

What these tests protect is the part that is easy to get wrong: the results must
not change. A row that has not been stamped yet still gets the old title test,
so the pool speeds up as it is stamped instead of the filter quietly showing a
different set of jobs.
"""
from __future__ import annotations

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, UserProfile
from app.strategy import on_role as onr


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _job(ext: str, title: str, score=None, on_role=None) -> Job:
    return Job(user_id=None, source=JobSource.GREENHOUSE, external_id=ext,
               company=ext, title=title, url=f"http://x/{ext}", description="x",
               rerank_score=score, blended_score=score, on_role=on_role)


def _seed(rows):
    with get_session() as s:
        s.exec(delete(UserProfile))
        for j in s.exec(select(Job).where(Job.external_id.like("onr-%"))).all():
            s.delete(j)
        s.add(UserProfile(user_id=None, target_roles="Machine Learning Engineer"))
        for r in rows:
            s.add(r)
        s.commit()


def _cleanup():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("onr-%"))).all():
            s.delete(j)
        s.exec(delete(UserProfile))
        s.commit()


# ── the predicate ────────────────────────────────────────────────────────────

def test_compute_uses_the_boundary_aware_gate_not_a_substring():
    """`role_title_match`, the repo's canonical gate — stricter than the ILIKE
    the SQL filter used, which is the point: '%ai%' matched 'Chair'."""
    assert onr.compute("Senior Machine Learning Engineer", ["Machine Learning Engineer"]) is True
    assert onr.compute("Warehouse Associate", ["Machine Learning Engineer"]) is False
    # No roles set: there is no question to answer yet.
    assert onr.compute("Anything", []) is None
    assert onr.compute("Anything", None) is None


# ── the filter ───────────────────────────────────────────────────────────────

def test_the_filter_reads_the_stamped_verdict(client):
    _seed([
        _job("onr-on", "Senior Machine Learning Engineer", score=20, on_role=True),
        _job("onr-off", "Warehouse Associate", score=10, on_role=False),
    ])
    try:
        titles = {j["title"] for j in
                  client.get("/api/jobs?roles_only=1&limit=200").json()["jobs"]}
        assert "Senior Machine Learning Engineer" in titles
        assert "Warehouse Associate" not in titles
    finally:
        _cleanup()


def test_an_unstamped_row_still_gets_the_old_title_test(client):
    """The migration guarantee: until a row is stamped the filter behaves
    exactly as it did before, so a half-backfilled pool never shows a different
    set of jobs — only a slower one."""
    _seed([
        _job("onr-null-on", "Machine Learning Engineer II", score=20),   # on_role NULL
        _job("onr-null-off", "Warehouse Associate", score=10),           # on_role NULL
    ])
    try:
        titles = {j["title"] for j in
                  client.get("/api/jobs?roles_only=1&limit=200").json()["jobs"]}
        assert "Machine Learning Engineer II" in titles
        assert "Warehouse Associate" not in titles, "unstamped rows keep the title test"
    finally:
        _cleanup()


def test_the_semantic_catch_survives(client):
    """An off-title job the AI scored a real fit stays visible however it is
    stamped — 'same work, different title' must not be filtered away."""
    _seed([_job("onr-sem", "Applied Scientist", score=82, on_role=False)])
    try:
        titles = {j["title"] for j in
                  client.get("/api/jobs?roles_only=1&limit=200").json()["jobs"]}
        assert "Applied Scientist" in titles
    finally:
        _cleanup()


def test_a_stamped_row_never_evaluates_the_ilike():
    """The speed claim, in SQL: the title test sits under an `on_role IS NULL`
    guard, so a stamped row short-circuits before any wildcard match."""
    from app.api.server import app  # noqa: F401  (ensures the module is imported)
    import inspect
    import app.api.server as server

    src = inspect.getsource(server.api_jobs)
    assert "Job.on_role.is_(True)" in src
    assert "Job.on_role.is_(None)," in src, (
        "the ILIKE fallback must be guarded by `on_role IS NULL`, or every row "
        "still pays for it"
    )


# ── backfill ─────────────────────────────────────────────────────────────────

def test_backfill_stamps_only_unstamped_rows_by_default():
    _seed([
        _job("onr-b1", "Machine Learning Engineer", on_role=None),
        _job("onr-b2", "Warehouse Associate", on_role=None),
        _job("onr-b3", "Warehouse Associate", on_role=True),   # a stale True
    ])
    try:
        # >=, not ==: the shared test DB may carry other NULL-owner rows from
        # another file, and this backfill is per USER, not per prefix.
        assert onr.backfill(None, ["Machine Learning Engineer"]) >= 2
        with get_session() as s:
            got = {j.external_id: j.on_role for j in
                   s.exec(select(Job).where(Job.external_id.like("onr-b%"))).all()}
        assert got == {"onr-b1": True, "onr-b2": False, "onr-b3": True}

        # A role change recomputes everything, stale value included.
        assert onr.backfill(None, ["Machine Learning Engineer"], only_missing=False) >= 3
        with get_session() as s:
            got = {j.external_id: j.on_role for j in
                   s.exec(select(Job).where(Job.external_id.like("onr-b%"))).all()}
        assert got == {"onr-b1": True, "onr-b2": False, "onr-b3": False}
    finally:
        _cleanup()


def test_backfill_without_roles_is_a_no_op():
    assert onr.backfill(None, []) == 0
    assert onr.backfill(None, None) == 0
