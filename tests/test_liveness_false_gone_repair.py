"""The one-off repair for postings a body-text false positive closed.

2026-10-30: "until the job is filled" (Everpure) and "at which the role is
filled" (Intuitive) read as removal notices, so two LIVE postings were filed
REMOVED / body_no_longer_active and every copy closed; the Everpure copy left
a user's board a minute after it was placed. `scripts/repair_body_gone_closures`
reopens those, and the rules it must keep are pinned here:

- a candidate needs ALL of: that verdict, a scoped id, a shared row the gate
  closed, and a board listing AFTER the verdict;
- a dry run makes no request and no write;
- only a LIVE re-check (with the fixed classifier) reopens; still-dead and
  inconclusive answers leave everything closed;
- only the gate's own closures reopen — a user's report, retention and board
  absence keep theirs;
- an application comes back to the board only when the closure's note is its
  last line, no dismissal marker is on it, and a placement event proves it
  was placed;
- a second run finds nothing.

Synthetic rows only, prefixed ``rgtest-`` and removed by that prefix.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import (
    Application, ApplicationStatus, FunnelEvent, Job, JobLiveness, JobLivenessState,
    JobSource,
)
from app.discovery import liveness as lv
from app.discovery.pipeline import SHARED_POOL_USER
from app.matching.preference_learning import USER_DISMISS_MARKER
from app.strategy import delivery_gate as gate
from scripts import repair_body_gone_closures as repair

_P = "rgtest-"
EXT = f"{_P}8331163"
URL = "https://job-boards.greenhouse.io/purestorage/jobs/8331163"
GATE_REASON = "Deactivated (posting REMOVED, confirmed on re-verification after delivery)"
NOTE = "\nJob closed — the posting was REMOVED when re-checked on 2026-10-30."
VERDICT_AT = datetime(2026, 10, 30, 0, 25, 30)
EVERPURE_BODY = ("<p><em>There is no application deadline and we accept applications "
                 "on an ongoing basis until the job is filled.</em></p>")


def _wipe():
    with get_session() as s:
        jids = list(s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all())
        if jids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(Job).where(Job.id.in_(jids)))
        s.exec(delete(JobLiveness).where(JobLiveness.external_id.like(f"{_P}%")))
        s.commit()


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    yield
    _wipe()


def _verdict(ext=EXT, source="greenhouse", reason=repair.BODY_REASON,
             state=JobLivenessState.REMOVED.value, at=VERDICT_AT):
    with get_session() as s:
        s.add(JobLiveness(source=source, external_id=ext, state=state, reason=reason,
                          http_status=200, checked_at=at))
        s.commit()


def _job(user_id, *, ext=EXT, source=JobSource.GREENHOUSE, closed_reason=GATE_REASON,
         last_seen=VERDICT_AT + timedelta(hours=14), status=None, notes=None,
         placed=False) -> tuple:
    with get_session() as s:
        j = Job(source=source, external_id=ext, company="Purestorage",
                title="AI Enablement Engineer", url=URL, user_id=user_id,
                description="d", is_closed=closed_reason is not None,
                closed_reason=closed_reason, last_seen=last_seen)
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
        if placed:
            s.add(FunnelEvent(job_id=j.id, stage="placement", passed=True, reason="placed"))
            s.commit()
        return j.id, aid


def _job_row(jid):
    with get_session() as s:
        return s.get(Job, jid)


def _app(aid):
    with get_session() as s:
        return s.get(Application, aid)


def _ok(url, ext):
    return JobLivenessState.LIVE.value, "http_200", 200


def _never(url, ext):
    raise AssertionError("a dry run must not make a request")


def _seed_everpure() -> dict:
    """The production shape, plus every copy the repair must leave alone."""
    _verdict()
    ids = {}
    ids["shared"], _ = _job(SHARED_POOL_USER)
    ids["placed"], ids["placed_app"] = _job(f"{_P}u1", status=ApplicationStatus.SKIPPED,
                                            notes=NOTE.strip(), placed=True)
    ids["copy"], _ = _job(f"{_P}u2")
    ids["reported"], ids["reported_app"] = _job(
        f"{_P}u3", closed_reason="Reported no longer available by the user",
        status=ApplicationStatus.SKIPPED,
        notes="Job closed — reported no longer available on 2026-10-30.", placed=True)
    ids["dismissed"], ids["dismissed_app"] = _job(
        f"{_P}u4", status=ApplicationStatus.SKIPPED, placed=True,
        notes=f"{USER_DISMISS_MARKER}{NOTE}")
    ids["unplaced"], ids["unplaced_app"] = _job(f"{_P}u5", status=ApplicationStatus.SKIPPED,
                                                notes=NOTE.strip())
    ids["retained"], _ = _job(f"{_P}u6", closed_reason="per-user retention (7d, no application)")
    ids["tailored"], ids["tailored_app"] = _job(f"{_P}u7", status=ApplicationStatus.TAILORED,
                                                placed=True)
    return ids


def test_the_production_sentence_rechecks_live_through_the_real_gate(monkeypatch):
    """End to end: the gate's own check, the FIXED classifier, the real body."""
    monkeypatch.setattr(gate, "_fetch", lambda url, timeout: (200, url, EVERPURE_BODY, None))
    ids = _seed_everpure()
    t = repair.repair(apply=True)
    assert (t.candidates, t.repaired) == (1, 1)
    state, _ = lv.load_states([("greenhouse", EXT)])[("greenhouse", EXT)]
    assert state == JobLivenessState.LIVE.value, "the cached REMOVED would re-close it"
    assert not _job_row(ids["shared"]).is_closed
    assert _job_row(ids["shared"]).closed_reason is None


def test_apply_reopens_the_gates_closures_and_restores_only_proven_placements():
    ids = _seed_everpure()
    t = repair.repair(apply=True, check=_ok)
    for k in ("shared", "placed", "copy", "dismissed", "unplaced", "tailored"):
        assert not _job_row(ids[k]).is_closed, k
    # Someone else's reason for closing it stands.
    assert _job_row(ids["reported"]).is_closed
    assert _job_row(ids["retained"]).is_closed

    back = _app(ids["placed_app"])
    assert back.status == ApplicationStatus.SHORTLISTED
    assert "Job closed" not in (back.notes or "")
    # The user's own word, a dismissal, an unproven placement: left Removed.
    assert _app(ids["reported_app"]).status == ApplicationStatus.SKIPPED
    assert _app(ids["dismissed_app"]).status == ApplicationStatus.SKIPPED
    assert _app(ids["unplaced_app"]).status == ApplicationStatus.SKIPPED
    assert _app(ids["tailored_app"]).status == ApplicationStatus.TAILORED
    assert (t.jobs_reopened, t.applications_restored, t.applications_left) == (6, 1, 2)


def test_a_dry_run_requests_nothing_and_writes_nothing():
    ids = _seed_everpure()
    t = repair.repair(apply=False, check=_never)
    assert t.candidates == 1 and t.repaired == 0
    assert _job_row(ids["shared"]).is_closed
    assert _app(ids["placed_app"]).status == ApplicationStatus.SKIPPED
    state, _ = lv.load_states([("greenhouse", EXT)])[("greenhouse", EXT)]
    assert state == JobLivenessState.REMOVED.value


@pytest.mark.parametrize("answer", [
    (JobLivenessState.REMOVED.value, "body_no_longer_active", 200),   # really gone
    (JobLivenessState.RATE_LIMITED.value, "http_429", 429),
    (JobLivenessState.UNKNOWN.value, "transport:ConnectTimeout", None),
])
def test_only_a_live_recheck_reopens(answer):
    ids = _seed_everpure()
    t = repair.repair(apply=True, check=lambda url, ext: answer)
    assert t.repaired == 0
    assert _job_row(ids["shared"]).is_closed and _job_row(ids["placed"]).is_closed
    assert _app(ids["placed_app"]).status == ApplicationStatus.SKIPPED
    state, _ = lv.load_states([("greenhouse", EXT)])[("greenhouse", EXT)]
    assert state == JobLivenessState.REMOVED.value


def test_a_second_run_finds_nothing():
    _seed_everpure()
    repair.repair(apply=True, check=_ok)
    assert repair.candidates() == []


def test_what_is_not_a_candidate():
    # The board has not listed it since the verdict.
    _verdict(ext=f"{_P}stale")
    _job(SHARED_POOL_USER, ext=f"{_P}stale", last_seen=VERDICT_AT - timedelta(hours=1))
    # A verdict the old patterns did not write.
    _verdict(ext=f"{_P}404", reason="http_404")
    _job(SHARED_POOL_USER, ext=f"{_P}404")
    # A bare Workday id names more than one employer's posting.
    _verdict(ext=f"{_P}R1", source="workday")
    _job(SHARED_POOL_USER, ext=f"{_P}R1", source=JobSource.WORKDAY)
    # No shared row to say the board still lists it.
    _verdict(ext=f"{_P}noshared")
    _job(f"{_P}u1", ext=f"{_P}noshared")
    # The shared row was closed by something other than the gate.
    _verdict(ext=f"{_P}absent")
    _job(SHARED_POOL_USER, ext=f"{_P}absent",
         closed_reason="Removed from company greenhouse ATS board")
    assert repair.candidates() == []


@pytest.mark.parametrize("reason", [
    "Deactivated (posting REMOVED, confirmed before delivery)",
    "Deactivated (posting REMOVED before delivery)",
    "Deactivated (posting gone before delivery)",
    "Deactivated (posting REMOVED, confirmed when a user opened it)",
])
def test_every_gate_closure_reason_is_recognised(reason):
    """Intuitive's shared row read "confirmed before delivery" and its copy
    "gone before delivery": the gate closes under four spellings."""
    _verdict()
    sid, _ = _job(SHARED_POOL_USER, closed_reason=reason)
    assert [c.external_id for c in repair.candidates()] == [EXT]
    repair.repair(apply=True, check=_ok)
    assert not _job_row(sid).is_closed
