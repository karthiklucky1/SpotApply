"""/api/jobs must read only the columns it returns.

The Explorer's list query used to be `select(Job, Application)`, which ships the
whole row — including `description`, the biggest column in the table and the one
retention exists to blank. The response builds 17 scalar fields and never uses
it. Unprojected reads of that column are what put Supabase at 205% of its egress
quota on 2 MB of stored data (docs/CAPACITY.md), and this query runs on every
filter keystroke, every page click and every refresh.

test_architecture_invariants cannot catch it: its AST check matches
`select(Job)` with exactly ONE argument, and this call had two entities.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import delete

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource

SERVER = Path(__file__).resolve().parent.parent / "app" / "api" / "server.py"


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _seed_one_with_application():
    now = datetime.utcnow()
    with get_session() as session:
        session.exec(delete(Application))
        session.exec(delete(Job))
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id="proj-1",
                  company="Acme", title="Backend Engineer", location="Remote",
                  remote=True, url="https://acme.example/j/1",
                  description="X" * 8000,          # the column that must not travel
                  rerank_score=82.0, blended_score=84.0, similarity_score=0.7,
                  rerank_reasoning="Strong Python + distributed systems overlap",
                  posted_at=now - timedelta(hours=5),
                  first_seen=now - timedelta(hours=5))
        session.add(job)
        session.commit()
        session.refresh(job)
        session.add(Application(user_id=None, job_id=job.id,
                                status=ApplicationStatus.SHORTLISTED,
                                apply_track="autofill"))
        session.commit()
        return job.id


def test_the_response_carries_the_application_and_never_the_description(client):
    jid = _seed_one_with_application()
    res = client.get("/api/jobs?limit=5")
    assert res.status_code == 200
    payload = res.json()
    row = next(j for j in payload["jobs"] if j["id"] == jid)

    # The joined Application still resolves through the projection.
    assert row["application"] is not None
    assert row["application"]["status"] == ApplicationStatus.SHORTLISTED.value
    assert row["application"]["apply_track"] == "autofill"
    assert row["application"]["created_at"]

    # Every field the UI renders survives.
    assert row["company"] == "Acme"
    assert row["title"] == "Backend Engineer"
    assert row["source"] == JobSource.GREENHOUSE.value
    assert row["rerank"] == 82.0 and row["blended"] == 84.0
    assert row["reason"].startswith("Strong Python")
    assert row["posted"] and row["is_new"] is True

    # And the payload does not carry the job description, at any depth.
    assert "description" not in row
    assert "XXXXXXXX" not in res.text


def test_a_job_without_an_application_reports_none(client):
    with get_session() as session:
        session.exec(delete(Application))
        session.exec(delete(Job))
        session.add(Job(user_id=None, source=JobSource.LEVER, external_id="proj-2",
                        company="Beta", title="Data Engineer", url="https://b/2",
                        description="d", first_seen=datetime.utcnow()))
        session.commit()
    rows = client.get("/api/jobs?limit=5").json()["jobs"]
    assert rows and rows[0]["application"] is None


# Functions that still select whole Job+Application entities. Each one ships
# every column of both tables — descriptions included — so this list is a debt
# register, not a blessing: `dashboard` renders the Kanban board and is on the
# same egress path as /api/jobs (docs/research/explorer-refresh-2026-08.md).
# Shrink it; never grow it. An unlisted offender fails this test.
_UNPROJECTED_JOB_APP_SELECTS = {
    "sync_emails",
    "export_applications_csv",
}


def test_no_new_unprojected_job_selects():
    """AST, not grep: a multi-entity select must not appear in a new place.

    test_architecture_invariants cannot see these — its check matches
    `select(Job)` with exactly ONE argument, so `select(Job, Application)`, the
    hottest read in the app, was invisible to it.
    """
    src = SERVER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = [(n.lineno, getattr(n, "end_lineno", n.lineno), n.name) for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def _owner(line: int) -> str:
        inner = [f for f in funcs if f[0] <= line <= f[1]]
        return min(inner, key=lambda f: f[1] - f[0])[2] if inner else "<module>"

    # A statement carrying `.options(...)` is choosing its columns through a
    # loader strategy rather than a tuple — the same allowance
    # test_architecture_invariants makes for `select(Job).options(load_only(...))`.
    # (The options may be built by a helper, as the dashboard's are, so this
    # cannot look for `load_only` in the statement itself; what those options
    # actually defer is asserted against real SQL in
    # tests/test_dashboard_load_only.py.)
    projected_spans = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        if any(getattr(c.func, "attr", None) == "options"
               for c in ast.walk(node) if isinstance(c, ast.Call)):
            projected_spans.append((node.lineno, getattr(node, "end_lineno", node.lineno)))

    entities = {"Job", "Application"}
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "select"):
            continue
        if len(node.args) < 2:
            continue                     # single-entity form: the other guard's job
        names = [a.id for a in node.args if isinstance(a, ast.Name)]
        if len([n for n in names if n in entities]) < 2:
            continue
        if any(lo <= node.lineno <= hi for lo, hi in projected_spans):
            continue                     # load_only present: columns are chosen
        owner = _owner(node.lineno)
        if owner not in _UNPROJECTED_JOB_APP_SELECTS:
            offenders.append(f"{owner} (line {node.lineno}): select({', '.join(names)})")
    assert not offenders, (
        "a multi-entity select ships every column of both tables, descriptions "
        "included — project the columns the response actually returns: "
        + "; ".join(offenders)
    )


def test_the_jobs_list_endpoint_is_projected():
    """api_jobs specifically must never regress to a whole-entity select — it is
    the read that runs on every filter keystroke."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "api_jobs")
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "select":
            names = [a.id for a in node.args if isinstance(a, ast.Name)]
            assert "Job" not in names and "Application" not in names, (
                f"api_jobs line {node.lineno} selects whole entities again"
            )
