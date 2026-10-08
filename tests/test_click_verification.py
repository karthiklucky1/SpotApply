"""Opening a job checks it is still open — by the same rules as delivery.

`POST /api/jobs/{id}/verify` (the dashboard on opening a job; the mobile
"still open?" button) ran its own HEAD check INSIDE an open database session,
never shared what it learned with the other people holding the same posting,
and moved every application short of SUBMITTED to Removed — TAILORED work and
INTERVIEWING candidates included, whose postings routinely close mid-process.
Its note was also learned as "not interested" (preference_learning).

Now: an ATS permalink goes through `delivery_gate.verify_for_delivery(force=True)`
(GET + body, single-flighted, one fetch per posting per interval, verdict
shared) and a conclusive REMOVED/EXPIRED closes every waiting copy; any other
link closes only the opener's copy; a refusal (429/403/timeout) closes nothing;
no session is open during the fetch.

Synthetic rows only, prefixed ``cvtest-`` and removed by that prefix.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import delete, select

from app.db import init_db
from app.db.init_db import get_session
from app.db.models import (
    Application, ApplicationStatus, Job, JobLiveness, JobLivenessState, JobSource,
)
from app.matching.preference_learning import _is_user_dismissal
from app.strategy import delivery_gate as gate

_P = "cvtest-"
URL = "https://job-boards.greenhouse.io/acme/jobs/777"


def _wipe():
    with get_session() as s:
        jids = list(s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all())
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(Job).where(Job.id.in_(jids)))
        s.exec(delete(JobLiveness).where(JobLiveness.external_id.like(f"{_P}%")))
        s.commit()


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    gate.metrics_snapshot(reset=True)
    yield
    _wipe()
    gate.metrics_snapshot(reset=True)


def _job(ext, user_id, *, source=JobSource.GREENHOUSE, url=URL, status=None,
         closed=False):
    with get_session() as s:
        j = Job(source=source, external_id=ext, company="Acme", url=url,
                title=f"Engineer {user_id or 'local'}", user_id=user_id,
                description="d", rerank_score=80.0, is_closed=closed,
                closed_reason="Reported no longer available by the user" if closed else None)
        s.add(j)
        s.commit()
        s.refresh(j)
        aid = None
        if status is not None:
            a = Application(job_id=j.id, user_id=user_id, status=status)
            s.add(a)
            s.commit()
            s.refresh(a)
            aid = a.id
        return j.id, aid


@pytest.fixture
def sessions(monkeypatch):
    """Count sessions open at any moment (route + gate + liveness all use
    init_db.get_session)."""
    from app.api import server
    real = init_db.get_session
    count = [0]

    @contextmanager
    def counting():
        count[0] += 1
        try:
            with real() as s:
                yield s
        finally:
            count[0] -= 1
    monkeypatch.setattr(init_db, "get_session", counting)
    monkeypatch.setattr(server, "get_session", counting)
    return count


def _fetch(monkeypatch, sessions=None, *, status=None, body="", error=None) -> list:
    calls = []

    def _fake(url, timeout):
        calls.append(sessions[0] if sessions is not None else None)
        return status, url, body, error
    monkeypatch.setattr(gate, "_fetch", _fake)
    return calls


def _verify(jid):
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app).post(f"/api/jobs/{jid}/verify")


def _app(aid):
    with get_session() as s:
        return s.get(Application, aid)


def _jobrow(jid):
    with get_session() as s:
        return s.get(Job, jid)


# ── a dead ATS posting ───────────────────────────────────────────────────────

def test_a_dead_posting_leaves_every_waiting_board_and_is_remembered(monkeypatch, sessions):
    calls = _fetch(monkeypatch, sessions, status=404)
    ext = _P + "1"
    jid, aid = _job(ext, None, status=ApplicationStatus.SHORTLISTED)
    other_jid, other_aid = _job(ext, _P + "other", status=ApplicationStatus.SHORTLISTED)

    d = _verify(jid).json()

    assert d["active"] is False and d["removed"] is True
    assert "confirmed when a user opened it" in d["closed_reason"]
    assert calls == [0], "a DB session was open during the network fetch"
    mine = _app(aid)
    assert mine.status == ApplicationStatus.SKIPPED and not _is_user_dismissal(mine)
    assert _app(other_aid).status == ApplicationStatus.SKIPPED, "the verdict is shared"
    with get_session() as s:
        st = s.exec(select(JobLiveness.state).where(JobLiveness.external_id == ext)).first()
    assert st == JobLivenessState.REMOVED.value


@pytest.mark.parametrize("status", [ApplicationStatus.TAILORED, ApplicationStatus.SUBMITTED,
                                    ApplicationStatus.INTERVIEWING])
def test_work_in_progress_keeps_its_place(monkeypatch, status):
    _fetch(monkeypatch, status=410)
    jid, aid = _job(_P + "2", None, status=status)
    d = _verify(jid).json()
    assert d["active"] is False and d["removed"] is False
    assert _app(aid).status == status, "a closed posting must not erase work or an interview"
    assert _jobrow(jid).is_closed


@pytest.mark.parametrize("status,error", [(429, None), (403, None), (503, None),
                                          (None, "ReadTimeout"), (200, None)])
def test_a_refusal_or_a_live_page_closes_nothing(monkeypatch, status, error):
    _fetch(monkeypatch, status=status, error=error, body="<h1>Engineer</h1>")
    jid, aid = _job(_P + "3", None, status=ApplicationStatus.SHORTLISTED)
    assert _verify(jid).json() == {"active": True}
    assert _app(aid).status == ApplicationStatus.SHORTLISTED
    assert not _jobrow(jid).is_closed


def test_a_popular_posting_is_fetched_at_most_once_per_interval(monkeypatch):
    """Ten people opening it in a minute is one fetch; but a LIVE verdict from
    hours ago (fresh for delivery) is looked at again — it may have closed."""
    ext = _P + "4"
    calls = _fetch(monkeypatch, status=200, body="<h1>Engineer</h1>")
    jids = [_job(ext, f"{_P}u{i}", status=ApplicationStatus.SHORTLISTED)[0] for i in range(3)]
    from app.api import server
    for i, jid in enumerate(jids):
        monkeypatch.setattr(server, "_get_user_id", lambda request, i=i: f"{_P}u{i}")
        assert _verify(jid).json() == {"active": True}
    assert len(calls) == 1

    with get_session() as s:
        row = s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).first()
        row.checked_at = datetime.utcnow() - timedelta(hours=2)
        s.add(row)
        s.commit()
    monkeypatch.setattr(server, "_get_user_id", lambda request: f"{_P}u0")
    _verify(jids[0])
    assert len(calls) == 2


# ── links that cannot speak for anyone else ──────────────────────────────────

def test_an_aggregator_link_closes_only_the_openers_copy(monkeypatch, sessions):
    from app.discovery import verify as v
    gate_calls = _fetch(monkeypatch, status=404)
    seen = []

    def _alive(url, timeout=4.0):
        seen.append(sessions[0])
        return False, "Link returned HTTP 404"
    monkeypatch.setattr(v, "check_job_alive", _alive)
    ext = _P + "5"
    agg = dict(source=JobSource.REMOTEOK, url="https://remoteok.com/l/5")
    jid, aid = _job(ext, None, status=ApplicationStatus.SHORTLISTED, **agg)
    other_jid, other_aid = _job(ext, _P + "other", status=ApplicationStatus.SHORTLISTED, **agg)

    d = _verify(jid).json()

    assert d == {"active": False, "removed": True,
                 "closed_reason": "Deactivated (Link returned HTTP 404)"}
    assert seen == [0], "a DB session was open during the network fetch"
    assert gate_calls == []
    assert _app(aid).status == ApplicationStatus.SKIPPED
    assert not _is_user_dismissal(_app(aid))
    assert _app(other_aid).status == ApplicationStatus.SHORTLISTED
    assert not _jobrow(other_jid).is_closed


def test_an_already_closed_job_is_not_fetched_again(monkeypatch):
    calls = _fetch(monkeypatch, status=200)
    jid, _aid = _job(_P + "6", None, status=ApplicationStatus.SUBMITTED, closed=True)
    d = _verify(jid).json()
    assert d["active"] is False and d["removed"] is False
    assert d["closed_reason"] == "Reported no longer available by the user"
    assert calls == []


# ── who may ask ──────────────────────────────────────────────────────────────

def test_only_the_owner_may_verify(monkeypatch):
    from app.api import server
    calls = _fetch(monkeypatch, status=404)
    jid, aid = _job(_P + "7", _P + "me", status=ApplicationStatus.SHORTLISTED)
    monkeypatch.setattr(server, "_get_user_id", lambda request: None)
    assert _verify(jid).status_code == 401
    monkeypatch.setattr(server, "_get_user_id", lambda request: _P + "someone-else")
    assert _verify(jid).status_code == 404
    assert _verify(999_999_999).status_code == 404
    assert calls == [] and _app(aid).status == ApplicationStatus.SHORTLISTED


# ── the dashboard ────────────────────────────────────────────────────────────

def test_the_dashboard_disables_a_card_only_when_it_left_the_board():
    html = (Path(__file__).resolve().parent.parent / "app/templates/dashboard.html").read_text()
    i = html.index("Live verification hook")
    hook = html[i:i + 2200]
    assert "!v.active && !v.removed" in hook
    grey = hook.index("card.style.pointerEvents = 'none'")
    assert hook.rindex("else if (!v.active)", 0, grey) > hook.index("!v.active && !v.removed")
    assert re.search(r"your application stays on your board", hook)


def test_opening_a_bare_workday_job_checks_only_its_own_employer(monkeypatch):
    """Review 2026-10-08: a bare requisition id is shared by other employers'
    postings, so the open fetches THIS row's URL and closes only same-employer
    copies; nothing is filed under the shared id."""
    gn = "https://gn.wd1.myworkdayjobs.com/GN/job/x_R29845"
    cs = "https://crowdstrike.wd5.myworkdayjobs.com/cs/job/y_R29845"
    calls = _fetch(monkeypatch, status=404)
    ext = _P + "R29845"
    jid, _aid = _job(ext, None, source=JobSource.WORKDAY, url=gn,
                     status=ApplicationStatus.SHORTLISTED)
    _cj, cs_aid = _job(ext, _P + "csfan", source=JobSource.WORKDAY, url=cs,
                       status=ApplicationStatus.SHORTLISTED)
    assert _verify(jid).json()["removed"] is True
    assert calls == [None]                      # one fetch: this row's URL
    assert _app(cs_aid).status == ApplicationStatus.SHORTLISTED
    with get_session() as s:
        assert s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).first() is None
