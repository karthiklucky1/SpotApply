"""A status click no longer reloads the page (2026-10-10).

Production, 2026-10-03..10: every Mark Applied / Interview / Rejected / Remove /
"No longer available" ended in ``location.reload()`` — a full /dashboard render
(p50 11-18 s that week) plus ~20 follow-up requests, for a one-row change. The
card now leaves its pane in the browser and the destination pane is fetched
from ``GET /api/board/pane``, drawn by the SAME template include the full
render uses (``_board_pane.html``), so a moved card cannot look different.

Synthetic rows only, prefixed and deleted by this file.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import delete, select

from app.api import server
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource

_U = "bp-user"
_OTHER = "bp-other"
_PREFIX = "bp-pane:"


def _as(monkeypatch, uid):
    monkeypatch.setattr(server, "_get_user_id", lambda request: uid)
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True))


def _seed(uid: str, status: ApplicationStatus, title: str) -> int:
    with get_session() as s:
        job = Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=f"{_PREFIX}{uid}:{title}",
                  company="Pane Co", title=title, url="https://boards.greenhouse.io/paneco/jobs/1",
                  description="x", discovered_at=datetime.utcnow(), first_seen=datetime.utcnow())
        s.add(job)
        s.commit()
        s.refresh(job)
        app_row = Application(user_id=uid, job_id=job.id, status=status,
                              submitted_at=datetime.utcnow(), updated_at=datetime.utcnow())
        s.add(app_row)
        s.commit()
        s.refresh(app_row)
        return app_row.id


@pytest.fixture(autouse=True)
def _clean():
    yield
    with get_session() as s:
        ids = [j.id for j in s.exec(select(Job).where(Job.external_id.like(f"{_PREFIX}%"))).all()]
        if ids:
            s.exec(delete(Application).where(Application.job_id.in_(ids)))
            s.exec(delete(Job).where(Job.id.in_(ids)))
            s.commit()


def test_pane_renders_the_owners_card_only(monkeypatch):
    mine = _seed(_U, ApplicationStatus.SUBMITTED, "Mine Engineer")
    theirs = _seed(_OTHER, ApplicationStatus.SUBMITTED, "Theirs Engineer")
    _as(monkeypatch, _U)
    out = server.board_pane(request=None, stage="submitted")
    assert out["stage"] == "submitted"
    assert f'data-app-id="{mine}"' in out["html"]
    assert f'data-app-id="{theirs}"' not in out["html"]
    assert "Theirs Engineer" not in out["html"]
    assert out["count"] == 1 and out["total"] == 1 and out["degraded"] is False


def test_each_pane_lists_its_own_statuses(monkeypatch):
    rej = _seed(_U, ApplicationStatus.REJECTED, "Rejected Role")
    skp = _seed(_U, ApplicationStatus.SKIPPED, "Removed Role")
    intv = _seed(_U, ApplicationStatus.OFFER, "Offer Role")
    _as(monkeypatch, _U)
    assert f'data-app-id="{rej}"' in server.board_pane(None, "rejected")["html"]
    assert f'id="skipped-card-{skp}"' in server.board_pane(None, "skipped")["html"]
    assert f'data-app-id="{intv}"' in server.board_pane(None, "interviewing")["html"]
    # And never in the wrong pane.
    assert f'data-app-id="{rej}"' not in server.board_pane(None, "interviewing")["html"]


def test_unknown_stage_is_refused(monkeypatch):
    _as(monkeypatch, _U)
    with pytest.raises(HTTPException) as e:
        server.board_pane(None, "shortlist-of-someone-else")
    assert e.value.status_code == 400


def test_anonymous_is_refused_before_any_read(monkeypatch):
    _as(monkeypatch, None)
    with pytest.raises(HTTPException) as e:
        server.board_pane(None, "submitted")
    assert e.value.status_code == 401


# ── The browser side: the handlers update in place ───────────────────────────

_DASH = Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html"
_HANDLERS = ("markOutcome", "markSubmitted", "markRejected", "removeApplication",
             "reportUnavailable", "submitFormFromModal")


def _handler_body(src: str, name: str) -> str:
    m = re.search(rf"\n(\s+)(?:async )?function {name}\(", src)
    assert m, name
    indent = m.group(1)
    start = m.start()
    nxt = re.compile(rf"\n{indent}(?:async )?function \w+\(")
    m2 = nxt.search(src, m.end())
    return src[start: m2.start() if m2 else len(src)]


def test_status_handlers_update_in_place_instead_of_reloading():
    src = _DASH.read_text(encoding="utf-8")
    for name in _HANDLERS:
        body = _handler_body(src, name)
        assert "location.reload()" not in body, f"{name} still reloads the page"
        assert "applyStatusChange(" in body, f"{name} does not update the board in place"
    # The did-you-submit prompt's Gone / Yes buttons too.
    i = src.index("document.getElementById('hp-sc-gone').onclick")
    prompt = src[i: src.index("Auto-dismiss after 25s", i)]
    assert "location.reload()" not in prompt and prompt.count("applyStatusChange(") == 2


def test_pane_template_is_the_one_the_full_render_uses():
    """Both surfaces must draw a pane from the same include, or an in-place move
    could look different from a reload."""
    src = _DASH.read_text(encoding="utf-8")
    for stage in ("shortlist", "submitted", "interviewing", "rejected", "skipped"):
        assert f"{{% with stage='{stage}', rows=" in src, stage
    assert src.count('{% include "_board_pane.html" %}') == 5
    assert (_DASH.parent / "_board_pane.html").exists()
    assert (_DASH.parent / "_board_macros.html").exists()
