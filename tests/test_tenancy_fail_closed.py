"""Fail-closed tenancy: unauthenticated requests must never see unscoped data,
and legacy ownerless jobs get adopted by their application's owner."""
from unittest.mock import patch

import pytest
from sqlmodel import select

from app.db.init_db import get_session, reconcile_job_owners
from app.db.models import Application, ApplicationStatus, Job, JobSource


# ── reconcile_job_owners ─────────────────────────────────────────────────────

@pytest.fixture
def _clean():
    def _wipe():
        with get_session() as s:
            for j in s.exec(select(Job).where(Job.external_id.like("rec-%"))).all():
                for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                    s.delete(a)
                s.delete(j)
            s.commit()
    _wipe()
    yield
    _wipe()


def _mk(s, ext, job_uid, app_uid):
    j = Job(source=JobSource.GREENHOUSE, external_id=ext, company=f"Co-{ext}",
            title=f"Role {ext}", url=f"http://x/{ext}", description="d",
            user_id=job_uid)
    s.add(j); s.commit(); s.refresh(j)
    a = Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED,
                    apply_track="manual", user_id=app_uid)
    s.add(a); s.commit()
    return j.id


def test_reconcile_adopts_ownerless_jobs(_clean):
    with get_session() as s:
        orphan_id = _mk(s, "rec-1", job_uid=None, app_uid="user-a")

    adopted = reconcile_job_owners()
    assert adopted >= 1

    with get_session() as s:
        assert s.get(Job, orphan_id).user_id == "user-a"

    # Idempotent: nothing left to adopt for this row on a second run.
    with get_session() as s:
        assert s.get(Job, orphan_id).user_id == "user-a"


def test_reconcile_never_reassigns_owned_jobs(_clean):
    with get_session() as s:
        owned_id = _mk(s, "rec-2", job_uid="user-b", app_uid="user-a")

    reconcile_job_owners()

    with get_session() as s:
        assert s.get(Job, owned_id).user_id == "user-b"  # untouched


def test_reconcile_leaves_unreferenced_null_jobs(_clean):
    with get_session() as s:
        j = Job(source=JobSource.GREENHOUSE, external_id="rec-3", company="C",
                title="R", url="http://x/rec-3", description="d", user_id=None)
        s.add(j); s.commit(); s.refresh(j)
        jid = j.id

    reconcile_job_owners()

    with get_session() as s:
        assert s.get(Job, jid).user_id is None


# ── 401 guards ───────────────────────────────────────────────────────────────

def test_list_endpoints_fail_closed_without_auth():
    from unittest.mock import PropertyMock
    from fastapi.testclient import TestClient
    import app.api.server as server
    from app.config import settings

    client = TestClient(server.app)
    # use_supabase is a computed property — patch it on the class.
    with patch.object(type(settings), "use_supabase", new_callable=PropertyMock, return_value=True):
        for path in ("/api/jobs", "/api/stats", "/stats", "/api/pipeline/live"):
            r = client.get(path)
            assert r.status_code == 401, f"{path} must 401 when unauthenticated, got {r.status_code}"


def test_list_endpoints_work_in_local_mode():
    from fastapi.testclient import TestClient
    import app.api.server as server

    client = TestClient(server.app)  # use_supabase False in tests → uid "local"
    for path in ("/api/jobs", "/api/stats", "/stats"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} should work in local mode, got {r.status_code}"
