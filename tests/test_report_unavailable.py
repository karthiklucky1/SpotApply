""""This job is no longer available" — a report closes the reporter's copy,
and only proof closes anyone else's (owner, 2026-10-08: "so many jobs are
closed").

Before this, the only answers on returning from a posting were "Yes,
submitted" and "Not yet", and the card's x is /skip — which preference
learning reads as "not interested" and uses to sink the company. And a
conclusive liveness verdict only ever stopped NEW deliveries: a posting that
died after it reached boards stayed on them until it aged out.

Rules pinned here:
- the reporter's copy leaves the board at once, as a SYSTEM skip (a "job
  closed" note, never the dismissal marker); submitted-and-later keeps its stage;
- one report never changes another tenant's board: it queues a forced,
  rationed re-check through the delivery gate's own SSRF-guarded fetch;
- only a conclusive REMOVED/EXPIRED closes the other copies, and work a user
  has done (TAILORED and beyond) keeps its place;
- 429 / 403 / a timeout / a live page change nothing for anyone else.

Synthetic rows only, prefixed ``rptest-`` and removed by that prefix.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import delete, select

from app.common import daily_counter
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    Application, ApplicationStatus, FunnelEvent, Job, JobLiveness, JobLivenessState,
    JobSource,
)
from app.discovery.pipeline import SHARED_POOL_USER
from app.matching.preference_learning import USER_DISMISS_MARKER, _is_user_dismissal
from app.strategy import delivery_gate as gate

_P = "rptest-"
URL = "https://job-boards.greenhouse.io/acme/jobs/4242"


def _wipe():
    with get_session() as s:
        jids = list(s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all())
        if jids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(Job).where(Job.id.in_(jids)))
        s.exec(delete(JobLiveness).where(JobLiveness.external_id.like(f"{_P}%")))
        s.commit()
    for who in ("me", "other"):
        daily_counter.reset(f"liveness_report:user:{_P}{who}")


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    gate.metrics_snapshot(reset=True)
    yield
    _wipe()
    gate.metrics_snapshot(reset=True)


def _job(ext: str, user_id, *, source=JobSource.GREENHOUSE, url: str = URL,
         status=None, notes: str = "") -> tuple:
    """One copy of a posting, optionally with an application on it."""
    with get_session() as s:
        j = Job(source=source, external_id=ext, company="Acme",
                title=f"Engineer {user_id or 'local'}", url=url, user_id=user_id,
                description="d", rerank_score=80.0)
        s.add(j)
        s.commit()
        s.refresh(j)
        aid = None
        if status is not None:
            a = Application(job_id=j.id, user_id=user_id, status=status, notes=notes)
            s.add(a)
            s.commit()
            s.refresh(a)
            aid = a.id
        return j.id, aid


def _fetches(monkeypatch, *, status=None, body="", error=None) -> list:
    calls = []

    def _fake(url, timeout):
        calls.append(url)
        return status, url, body, error
    monkeypatch.setattr(gate, "_fetch", _fake)
    return calls


def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _app(aid):
    with get_session() as s:
        return s.get(Application, aid)


def _job_row(jid):
    with get_session() as s:
        return s.get(Job, jid)


def _events(jid):
    with get_session() as s:
        return list(s.exec(select(FunnelEvent).where(
            FunnelEvent.job_id == jid, FunnelEvent.stage == "user_report")).all())


# ── the reporter's own copy ──────────────────────────────────────────────────

def test_a_report_takes_the_job_off_the_board_without_teaching_dislike(monkeypatch):
    _fetches(monkeypatch, status=200, body="<h1>Engineer</h1> Apply now")
    jid, aid = _job(_P + "1", None, status=ApplicationStatus.SHORTLISTED)

    r = _client().post(f"/application/{aid}/unavailable")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["success"] and d["removed"] and d["status"] == "skipped"
    assert d["verification"] == "queued"

    a, j = _app(aid), _job_row(jid)
    assert a.status == ApplicationStatus.SKIPPED
    assert "job closed" in a.notes.lower()
    assert USER_DISMISS_MARKER not in a.notes
    assert not _is_user_dismissal(a), "a closed posting is not 'not interested'"
    assert j.is_closed and "reported no longer available" in j.closed_reason.lower()

    (ev,) = _events(jid)
    assert ev.reason == "unavailable:closed" and ev.passed is False
    assert json.loads(ev.metadata_json) == {"status_before": "shortlisted"}


@pytest.mark.parametrize("status", [ApplicationStatus.TAILORED,
                                    ApplicationStatus.READY_TO_SUBMIT])
def test_work_in_progress_on_a_dead_posting_also_leaves_the_board(monkeypatch, status):
    """The USER says it is gone: there is nothing left to submit to."""
    _fetches(monkeypatch, status=200)
    _jid, aid = _job(_P + "2", None, status=status)
    assert _client().post(f"/application/{aid}/unavailable").json()["removed"]
    assert _app(aid).status == ApplicationStatus.SKIPPED


@pytest.mark.parametrize("status", [ApplicationStatus.SUBMITTED,
                                    ApplicationStatus.INTERVIEWING])
def test_a_submitted_application_keeps_its_stage(monkeypatch, status):
    """Postings close after people apply; that is not a reason to lose track."""
    _fetches(monkeypatch, status=200)
    jid, aid = _job(_P + "3", None, status=status)
    d = _client().post(f"/application/{aid}/unavailable").json()
    assert d["success"] and not d["removed"] and d["status"] == status.value
    assert _app(aid).status == status
    assert _job_row(jid).is_closed
    assert len(_events(jid)) == 1


def test_a_second_report_changes_nothing(monkeypatch):
    calls = _fetches(monkeypatch, status=200)
    jid, aid = _job(_P + "4", None, status=ApplicationStatus.SHORTLISTED)
    c = _client()
    assert c.post(f"/application/{aid}/unavailable").json().get("already") is None
    d = c.post(f"/application/{aid}/unavailable").json()
    assert d["already"] is True
    assert len(_events(jid)) == 1
    assert len(calls) == 1, "a repeat report must not trigger a second fetch"


def test_only_fixed_reason_keys_reach_the_log(monkeypatch):
    _fetches(monkeypatch, status=200)
    jid, aid = _job(_P + "5", None, status=ApplicationStatus.SHORTLISTED)
    _client().post(f"/application/{aid}/unavailable",
                   params={"reason": "<img src=x> https://evil.example/a?b=c"})
    (ev,) = _events(jid)
    assert ev.reason == "unavailable:other"
    assert re.fullmatch(r"unavailable:(closed|not_found|other)", ev.reason)


# ── who may report ───────────────────────────────────────────────────────────

def test_anonymous_and_other_tenants_cannot_report(monkeypatch):
    from app.api import server
    _jid, aid = _job(_P + "6", _P + "me", status=ApplicationStatus.SHORTLISTED)
    monkeypatch.setattr(server, "_get_user_id", lambda request: None)
    assert _client().post(f"/application/{aid}/unavailable").status_code == 401
    monkeypatch.setattr(server, "_get_user_id", lambda request: _P + "other")
    assert _client().post(f"/application/{aid}/unavailable").status_code == 404
    assert _app(aid).status == ApplicationStatus.SHORTLISTED


# ── everyone else: only proof changes their board ────────────────────────────

def _others(ext: str, source=JobSource.GREENHOUSE):
    """The same posting on the shared pool and two other boards."""
    shared, _ = _job(ext, SHARED_POOL_USER, source=source)
    waiting = _job(ext, _P + "other", source=source, status=ApplicationStatus.SHORTLISTED)
    tailored = _job(ext, _P + "third", source=source, status=ApplicationStatus.TAILORED)
    unscored, _ = _job(ext, _P + "fourth", source=source)
    return shared, waiting, tailored, unscored


@pytest.mark.parametrize("status,error", [
    (429, None), (403, None), (None, "ConnectTimeout"), (200, None), (503, None),
])
def test_an_inconclusive_or_live_recheck_changes_nobody_elses_board(monkeypatch, status, error):
    calls = _fetches(monkeypatch, status=status, error=error, body="<h1>Engineer</h1>")
    ext = _P + "7"
    _jid, aid = _job(ext, None, status=ApplicationStatus.SHORTLISTED)
    shared, (w_jid, w_aid), (t_jid, t_aid), unscored = _others(ext)

    _client().post(f"/application/{aid}/unavailable")

    assert len(calls) == 1
    assert _app(aid).status == ApplicationStatus.SKIPPED            # the reporter's own
    assert _app(w_aid).status == ApplicationStatus.SHORTLISTED      # nobody else's
    assert _app(t_aid).status == ApplicationStatus.TAILORED
    for jid in (shared, w_jid, t_jid, unscored):
        assert not _job_row(jid).is_closed
    with get_session() as s:
        st = s.exec(select(JobLiveness.state).where(JobLiveness.external_id == ext)).first()
    assert st not in (JobLivenessState.REMOVED.value, JobLivenessState.EXPIRED.value)


@pytest.mark.parametrize("status,body", [
    (404, ""), (410, ""), (200, "Sorry, this position is no longer available."),
])
def test_a_conclusive_recheck_takes_it_off_every_waiting_board(monkeypatch, status, body):
    _fetches(monkeypatch, status=status, body=body)
    ext = _P + "8"
    _jid, aid = _job(ext, None, status=ApplicationStatus.SHORTLISTED)
    shared, (w_jid, w_aid), (t_jid, t_aid), unscored = _others(ext)
    # Same external id on ANOTHER source is another posting: untouched.
    lever_jid, lever_aid = _job(ext, _P + "other", source=JobSource.LEVER,
                                url="https://jobs.lever.co/acme/x",
                                status=ApplicationStatus.SHORTLISTED)

    _client().post(f"/application/{aid}/unavailable")

    for jid in (shared, w_jid, t_jid, unscored):
        assert _job_row(jid).is_closed, "every copy of the dead posting closes"
    waiting = _app(w_aid)
    assert waiting.status == ApplicationStatus.SKIPPED
    assert "job closed" in waiting.notes.lower() and not _is_user_dismissal(waiting)
    assert _app(t_aid).status == ApplicationStatus.TAILORED, "their work stays"
    assert not _job_row(lever_jid).is_closed
    assert _app(lever_aid).status == ApplicationStatus.SHORTLISTED
    m = gate.metrics_snapshot()
    assert m.get("report_confirmed_dead") == 1 and m.get("dead_applications_removed") == 1


def test_a_recent_live_verdict_does_not_outvote_the_report(monkeypatch):
    """LIVE checked an hour ago is fresh for delivery (12 h) but the user just
    saw it gone — look again. Inside the report interval, do not."""
    ext = _P + "9"
    with get_session() as s:
        s.add(JobLiveness(source="greenhouse", external_id=ext,
                          state=JobLivenessState.LIVE.value,
                          checked_at=datetime.utcnow() - timedelta(hours=1)))
        s.commit()
    calls = _fetches(monkeypatch, status=404)
    assert gate.verify_reported(JobSource.GREENHOUSE, ext, URL) == "confirmed_dead"
    assert len(calls) == 1

    ext2 = _P + "10"
    with get_session() as s:
        s.add(JobLiveness(source="greenhouse", external_id=ext2,
                          state=JobLivenessState.LIVE.value,
                          checked_at=datetime.utcnow() - timedelta(minutes=5)))
        s.commit()
    calls.clear()
    assert gate.verify_reported(JobSource.GREENHOUSE, ext2, URL) == "not_confirmed"
    assert calls == [], "a posting is re-fetched at most once per report interval"


def test_an_aggregator_link_is_never_fetched(monkeypatch):
    calls = _fetches(monkeypatch, status=404)
    ext = _P + "11"
    _jid, aid = _job(ext, None, source=JobSource.REMOTEOK, url="https://remoteok.com/l/1",
                     status=ApplicationStatus.SHORTLISTED)
    other = _job(ext, _P + "other", source=JobSource.REMOTEOK, url="https://remoteok.com/l/1",
                 status=ApplicationStatus.SHORTLISTED)
    _client().post(f"/application/{aid}/unavailable")
    assert calls == []
    assert _app(aid).status == ApplicationStatus.SKIPPED
    assert _app(other[1]).status == ApplicationStatus.SHORTLISTED


def test_reports_are_rationed_per_reporter(monkeypatch):
    monkeypatch.setattr(settings, "liveness_reports_per_user_daily", 1)
    calls = _fetches(monkeypatch, status=200)
    me = _P + "me"
    assert gate.verify_reported(JobSource.GREENHOUSE, _P + "12", URL, user_id=me) == "not_confirmed"
    assert gate.verify_reported(JobSource.GREENHOUSE, _P + "13", URL, user_id=me) == "capped"
    assert len(calls) == 1
    # Someone else's allowance is their own.
    assert gate.verify_reported(JobSource.GREENHOUSE, _P + "14", URL,
                                user_id=_P + "other") == "not_confirmed"


def test_an_already_dead_posting_closes_the_rest_without_a_fetch(monkeypatch):
    ext = _P + "15"
    with get_session() as s:
        s.add(JobLiveness(source="greenhouse", external_id=ext,
                          state=JobLivenessState.REMOVED.value, checked_at=datetime.utcnow()))
        s.commit()
    calls = _fetches(monkeypatch, status=200)
    _jid, w_aid = _job(ext, _P + "other", status=ApplicationStatus.SHORTLISTED)
    assert gate.verify_reported(JobSource.GREENHOUSE, ext, URL, user_id=_P + "me") == "confirmed_dead"
    assert calls == []
    assert _app(w_aid).status == ApplicationStatus.SKIPPED


def test_a_failing_recheck_never_surfaces(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(gate, "_cached", _boom)
    assert gate.verify_reported(JobSource.GREENHOUSE, _P + "16", URL) == "error"


# ── the existing drawer check is not "not interested" either ─────────────────

def test_a_click_verification_close_is_not_learned_as_a_dismissal():
    a = Application(job_id=0, status=ApplicationStatus.SKIPPED,
                    notes="\nJob marked closed during click verification: HTTP 404")
    assert not _is_user_dismissal(a)
    # A real dismissal still is.
    assert _is_user_dismissal(Application(job_id=0, status=ApplicationStatus.SKIPPED,
                                          notes=USER_DISMISS_MARKER))


# ── the dashboard ────────────────────────────────────────────────────────────

# The board's card markup lives in the macro + pane includes since 2026-10-10
# (one definition for the full render and the in-place pane endpoint), so the
# "dashboard HTML" is the template set, not one file.
_TPL = Path(__file__).resolve().parent.parent / "app/templates"
HTML = "\n".join((_TPL / f).read_text() for f in ("dashboard.html", "_board_macros.html", "_board_pane.html"))


def _function(name: str) -> str:
    m = re.search(r"(async\s+)?function\s+" + name + r"\s*\(", HTML)
    assert m, name
    depth, i = 0, HTML.index("{", m.end())
    for j in range(i, len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[j], 0)
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


def test_the_return_prompt_offers_no_longer_available():
    prompt = _function("showSubmitConfirm")
    assert 'id="hp-sc-gone"' in prompt and "This job is no longer available" in prompt
    assert "_postUnavailable(appId)" in prompt
    assert "/unavailable?reason=closed" in _function("_postUnavailable")


def test_the_job_view_can_report_it_too():
    """The prompt closes itself after 25 s; the job view keeps the option."""
    assert "reportUnavailable({{ app.id }}, this)" in HTML
    body = _function("reportUnavailable")
    assert "_postUnavailable(appId)" in body and "not interested" in body


def test_the_removed_tab_says_why():
    # Keyed on a column the board actually loads (_dashboard_load_options):
    # app.notes is deferred there, and reading it detached fails the render.
    assert "{% if job.is_closed %}<span" in HTML
    assert ">No longer available</span>" in HTML
    import inspect
    from app.api.server import _dashboard_load_options
    assert "Job.is_closed" in inspect.getsource(_dashboard_load_options)


def test_new_copy_says_resume_not_resume_with_accents():
    for name in ("showSubmitConfirm", "reportUnavailable", "_postUnavailable"):
        assert "résumé" not in _function(name).lower()


def test_the_removed_tab_renders_the_reason(monkeypatch):
    """End to end: report, then the board renders (a template that reads an
    unloaded column 500s here, not in a string check)."""
    _fetches(monkeypatch, status=200)
    _jid, aid = _job(_P + "17", None, status=ApplicationStatus.SHORTLISTED)
    c = _client()
    c.post(f"/application/{aid}/unavailable")
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert ">No longer available</span>" in r.text


# ── review 2026-10-08: what a report must never close ────────────────────────

def test_a_failed_tailor_card_leaves_the_board_too(monkeypatch):
    """ERROR (a tailor our checks blocked) is a board status; leaving it there
    after the user said the job is gone sent them to Remove, which is learned
    as "not interested"."""
    _fetches(monkeypatch, status=200)
    _jid, aid = _job(_P + "20", None, status=ApplicationStatus.ERROR)
    d = _client().post(f"/application/{aid}/unavailable").json()
    assert d["removed"] is True and _app(aid).status == ApplicationStatus.SKIPPED
    assert not _is_user_dismissal(_app(aid))


def test_a_stale_verdict_on_an_aggregator_link_closes_nobody_else(monkeypatch):
    """A cached REMOVED on an aggregator can be a HEAD that landed on a careers
    page. The source rule comes before ANY verdict, cached or new."""
    calls = _fetches(monkeypatch, status=404)
    ext = _P + "21"
    with get_session() as s:
        s.add(JobLiveness(source="remoteok", external_id=ext,
                          state=JobLivenessState.REMOVED.value, checked_at=datetime.utcnow()))
        s.commit()
    agg = dict(source=JobSource.REMOTEOK, url="https://remoteok.com/l/21")
    _jid, other = _job(ext, _P + "other", status=ApplicationStatus.SHORTLISTED, **agg)
    assert gate.verify_reported(JobSource.REMOTEOK, ext, agg["url"],
                                user_id=_P + "me") == "unverifiable"
    assert calls == [] and _app(other).status == ApplicationStatus.SHORTLISTED
    assert gate.close_dead_everywhere(JobSource.REMOTEOK, ext,
                                      JobLivenessState.REMOVED.value)["jobs_closed"] == 0


GN = "https://gn.wd1.myworkdayjobs.com/en-US/GN/job/Shakopee/Fitting-Advisor_R29845"
CS = "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers/job/Sunnyvale/Engineer_R29845"


def test_a_bare_workday_id_never_closes_another_employers_job(monkeypatch):
    """Workday requisition ids are unique per employer: CrowdStrike and GN both
    had R29845 (production, 2026-09-25). A legacy row keyed by the BARE id must
    only ever close copies of the same employer's posting."""
    calls = _fetches(monkeypatch, status=404)
    ext = _P + "R29845"
    _jid, aid = _job(ext, None, source=JobSource.WORKDAY, url=GN,
                     status=ApplicationStatus.SHORTLISTED)
    gn_jid, gn_aid = _job(ext, _P + "gnfan", source=JobSource.WORKDAY, url=GN,
                          status=ApplicationStatus.SHORTLISTED)
    cs_jid, cs_aid = _job(ext, _P + "csfan", source=JobSource.WORKDAY, url=CS,
                          status=ApplicationStatus.SHORTLISTED)

    _client().post(f"/application/{aid}/unavailable")

    assert calls == [GN], "the reporter's own URL, and only it"
    assert _app(gn_aid).status == ApplicationStatus.SKIPPED      # same employer
    assert _app(cs_aid).status == ApplicationStatus.SHORTLISTED  # another employer
    assert not _job_row(cs_jid).is_closed
    with get_session() as s:
        assert s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).first() is None, \
            "a verdict filed under a bare id would speak for every employer using it"


def test_another_employers_cached_verdict_is_not_trusted(monkeypatch):
    """The shared row under a bare id may be the OTHER employer's: check this URL."""
    ext = _P + "R30000"
    with get_session() as s:
        s.add(JobLiveness(source="workday", external_id=ext,
                          state=JobLivenessState.REMOVED.value, checked_at=datetime.utcnow()))
        s.commit()
    calls = _fetches(monkeypatch, status=200, body="<h1>Engineer</h1> Apply")
    _jid, gn_aid = _job(ext, _P + "gnfan", source=JobSource.WORKDAY, url=GN,
                        status=ApplicationStatus.SHORTLISTED)
    assert gate.verify_reported(JobSource.WORKDAY, ext, GN, user_id=_P + "me") == "not_confirmed"
    assert calls == [GN] and _app(gn_aid).status == ApplicationStatus.SHORTLISTED


def test_the_message_follows_what_the_report_did():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app/templates/dashboard.html").read_text()
    toast = _function("_unavailableToast")
    assert "r.removed" in toast and "keeps its place" in toast
    assert "_unavailableToast(r)" in html and "_UNAVAILABLE_TOAST" not in html
