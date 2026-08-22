"""The grounding gate holds on EVERY exit, not just the browser download.

A tailored draft that fails the grounding check is parked at
ApplicationStatus.ERROR with the file still on disk. The download route has
refused these with a 409 since the gate shipped — but the extension's attach
route (/api/fill-pack/{id}/resume), the fill-pack payload, and the Tailoring
Studio preview (/application/{id}/details) all kept serving the rejected
document. The path every beta user actually applies through was the one with
no gate.

Also pinned here: the auto-tailor entry points those routes hide are counted
and capped like every other tailor (they used to be the unmetered spend the
Aug 2026 review flagged), and the abuse ceiling is a real backstop again.
"""
from __future__ import annotations

import base64

import pytest
from sqlmodel import delete

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_leaked_rows():
    """Clean up AFTER as well as before.

    Leaving one Application behind is not harmless: job_retention's
    `Job.id.not_in(select(Application.job_id))` then excludes whichever job
    later reuses that rowid, and SQLite hands out max(rowid)+1 — so an orphaned
    Application silently made test_description_retention strip 0 rows. It only
    showed up in CI's reversed-order run.
    """
    yield
    with get_session() as session:
        session.exec(delete(Application))
        session.exec(delete(Job))
        session.commit()


def _seed_error_app(notes="Grounding failed: bullet 'Led Kubernetes migration' has no source."):
    with get_session() as session:
        session.exec(delete(Application))
        session.exec(delete(Job))
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id="g1",
                  company="AcmeCo", title="Backend Engineer",
                  url="https://x.co/1", description="d")
        session.add(job)
        session.commit()
        session.refresh(job)
        app_row = Application(job_id=job.id, status=ApplicationStatus.ERROR,
                              tailored_resume_path="/tmp/definitely-missing.docx",
                              cover_letter_path="/tmp/definitely-missing.txt",
                              notes=notes)
        session.add(app_row)
        session.commit()
        session.refresh(app_row)
        return app_row.id


def test_attach_route_never_serves_a_grounding_failed_draft(client, monkeypatch):
    """With a base résumé available, the extension gets THAT, flagged untailored
    — never the rejected document."""
    from app.api import server
    monkeypatch.setattr(server, "_base_resume_bytes",
                        lambda uid: ("base.docx", "application/msword", b"BASE-RESUME"))
    app_id = _seed_error_app()
    r = client.get(f"/api/fill-pack/{app_id}/resume")
    assert r.status_code == 200
    d = r.json()
    assert d["tailored"] is False
    assert base64.b64decode(d["base64"]) == b"BASE-RESUME"
    assert "grounding" in d["notice"].lower() or "Grounding" in d["notice"]


def test_attach_route_409s_when_no_base_resume_exists(client, monkeypatch):
    from app.api import server
    monkeypatch.setattr(server, "_base_resume_bytes", lambda uid: None)
    app_id = _seed_error_app()
    r = client.get(f"/api/fill-pack/{app_id}/resume")
    assert r.status_code == 409
    assert "grounding" in r.json()["detail"].lower()


def test_details_preview_withholds_the_rejected_text(client):
    """The Tailoring Studio preview returned the full text of a rejected draft
    (copy-pasteable), while the download refused it. Same gate, both exits."""
    app_id = _seed_error_app()
    r = client.get(f"/application/{app_id}/details")
    assert r.status_code == 200
    d = r.json()
    assert d["resume"].startswith("(Withheld")
    assert "grounding" in d["resume"].lower()
    assert d["cover_letter"].startswith("(Withheld")


def test_fill_pack_blanks_rejected_docs_and_skips_retailoring(client, monkeypatch):
    """The fill-pack payload must not carry rejected text, and must not fire a
    background re-tailor of inputs that just failed the check."""
    import threading
    spawned = []
    real_thread = threading.Thread

    class _SpyThread(real_thread):
        def start(self):
            spawned.append(self._target)
            # Do not actually run a tailor in tests.

    monkeypatch.setattr(threading, "Thread", _SpyThread)
    app_id = _seed_error_app()
    r = client.get(f"/api/fill-pack/{app_id}")
    assert r.status_code == 200
    d = r.json()
    assert d.get("resume_text", "") == ""
    assert d.get("cover_letter", d.get("cover_text", "")) in ("", None)
    assert not spawned, "a grounding-blocked application must not be auto-retailored"


def test_abuse_ceiling_clamps_numeric_plan_limits(monkeypatch):
    """TAILOR_ABUSE_DAILY_CAP became dead code when every tier got a numeric
    tailor_daily — it was only reachable through the `daily_limit is None`
    branch. It must clamp numeric limits too, or a future high plan number
    ships with no backstop at all."""
    from app.api import server
    from app.db.models import PlanTier

    monkeypatch.setattr(server, "_get_trial", lambda uid: None)
    monkeypatch.setattr(server, "_get_user_plan", lambda uid: PlanTier.PRO)
    monkeypatch.setitem(server.PLAN_LIMITS[PlanTier.PRO], "tailor_daily", 500)
    monkeypatch.setattr(server.settings, "tailor_abuse_daily_cap", 25)

    class _Row:
        tailor_count = 30

    monkeypatch.setattr(server, "_get_or_create_usage", lambda session, uid: _Row())
    allowed, detail, usage = server._check_tailor_limit("some-uuid")
    assert allowed is False
    assert usage["daily_limit"] == 25
