"""The resume route while Auto-Fill's own tailor is still writing the resume.

Production 2026-11-03: clicking Auto-Fill makes fill-pack start a tailor in the
background (25-47 s); the extension asks /api/fill-pack/{id}/resume a few
seconds later, while it runs. The route answered a bare 409 "already
generating", the extension read that as "network/server", never asked again,
and three fills in a row (Greenhouse, Ashby, Lever) went out with no resume.

Pinned here: the route WAITS (bounded) and serves what the running tailor
wrote; a caller that retries is told "generating"; an older build that cannot
retry gets the uploaded resume with a notice rather than nothing; a draft the
running tailor parked at ERROR is never served.

Rows are prefixed ``rwt_`` and cleaned by that prefix.
"""
from __future__ import annotations

import base64
import threading
import time

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource

_P = "rwt_"


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from app.api import server
    monkeypatch.setattr(server.settings, "resume_tailor_wait_seconds", 2)
    monkeypatch.setattr(server, "_base_resume_bytes",
                        lambda uid: ("resume.pdf", "application/pdf", b"BASE-RESUME"))
    monkeypatch.setattr(server, "_autofill_resume_source", lambda session, uid: "tailored")
    _wipe()
    yield
    _wipe()


def _wipe():
    with get_session() as s:
        jids = [r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.commit()


def _seed(ext="a"):
    with get_session() as s:
        j = Job(user_id=None, source=JobSource.LEVER, external_id=_P + ext, company="Loft",
                title="Ground Software Engineer", url="https://jobs.lever.co/loft/1",
                description="d")
        s.add(j)
        s.commit()
        s.refresh(j)
        a = Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, user_id=None)
        s.add(a)
        s.commit()
        s.refresh(a)
        return a.id


def _background_tailor(app_id, tmp_path, after, *, status=ApplicationStatus.TAILORED,
                       write=True):
    """Stand-in for fill-pack's tailor: holds the claim, writes, releases."""
    from app.api import server
    assert server._claim_tailor(app_id)

    def run():
        time.sleep(after)
        with get_session() as s:
            a = s.get(Application, app_id)
            if write:
                f = tmp_path / "First_Last_Resume.docx"
                f.write_bytes(b"TAILORED-DOCX")
                a.tailored_resume_path = str(f)
            a.status = status
            if status == ApplicationStatus.ERROR:
                a.notes = "Grounding failed: invented employer."
            s.add(a)
            s.commit()
        server._release_tailor(app_id)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_the_route_waits_for_the_running_tailor_and_serves_it(client, monkeypatch, tmp_path):
    from app.api import server
    monkeypatch.setattr(server, "_export_verdict",
                        lambda *a, **k: type("V", (), {"blocked": False, "reason": ""})())
    app_id = _seed()
    t = _background_tailor(app_id, tmp_path, after=0.6)
    r = client.get(f"/api/fill-pack/{app_id}/resume?accept=.docx&retry=1")
    t.join(5)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("tailored") is not False
    assert base64.b64decode(d["base64"]) == b"TAILORED-DOCX"


def test_a_caller_that_retries_is_told_it_is_still_generating(client, tmp_path):
    app_id = _seed()
    t = _background_tailor(app_id, tmp_path, after=4)      # longer than the 2 s wait
    started = time.monotonic()
    r = client.get(f"/api/fill-pack/{app_id}/resume?accept=*&retry=1")
    waited = time.monotonic() - started
    t.join(6)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["generating"] is True and detail["retry_after"] > 0
    assert 1.5 <= waited < 4, "the route must wait (bounded) before answering"


def test_an_older_build_gets_the_uploaded_resume_with_a_notice(client, tmp_path):
    """No retry=1: that build reads any non-200 as 'network/server' and stops."""
    app_id = _seed()
    t = _background_tailor(app_id, tmp_path, after=4)
    r = client.get(f"/api/fill-pack/{app_id}/resume?accept=*")
    t.join(6)
    assert r.status_code == 200
    d = r.json()
    assert d["tailored"] is False and base64.b64decode(d["base64"]) == b"BASE-RESUME"
    assert "still being written" in d["notice"]


def test_a_draft_the_running_tailor_rejected_is_never_served(client, tmp_path):
    app_id = _seed()
    t = _background_tailor(app_id, tmp_path, after=0.5, status=ApplicationStatus.ERROR)
    r = client.get(f"/api/fill-pack/{app_id}/resume?accept=*&retry=1")
    t.join(5)
    assert r.status_code == 200
    d = r.json()
    assert d["tailored"] is False and base64.b64decode(d["base64"]) == b"BASE-RESUME"
    assert "grounding" in d["notice"].lower()


def test_a_tailor_that_finished_without_a_file_does_not_start_another(client, monkeypatch, tmp_path):
    """The background run failed: attach the uploaded resume now; a second paid
    generation inside this request would outlast the extension's timeout."""
    from app.api import server
    calls = []
    monkeypatch.setattr("app.tailoring.tailor.tailor_for_application",
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError()))
    app_id = _seed()
    t = _background_tailor(app_id, tmp_path, after=0.3, status=ApplicationStatus.SHORTLISTED,
                           write=False)
    r = client.get(f"/api/fill-pack/{app_id}/resume?accept=*&retry=1")
    t.join(5)
    assert r.status_code == 200
    d = r.json()
    assert d["tailored"] is False and "didn't finish" in d["notice"]
    assert calls == []
    assert not server._tailor_in_flight(app_id)


def test_the_wait_stays_under_the_extensions_request_timeout():
    """background.js aborts a request at FETCH_TIMEOUT_MS; waiting longer than
    that turns the fix back into the failure."""
    import pathlib

    from app.config import Settings
    js = pathlib.Path("extension/background.js").read_text()
    import re
    ms = int(re.search(r"const FETCH_TIMEOUT_MS = (\d+);", js).group(1))
    assert Settings.model_fields["resume_tailor_wait_seconds"].default * 1000 + 3000 <= ms
