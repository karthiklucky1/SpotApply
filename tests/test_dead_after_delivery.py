"""Death AFTER delivery (2026-10-10).

The gate verified a posting at delivery and never again; the pulse lane
re-fetched boards every 5-60 min and closed nothing; `mark_ghost_jobs`
recorded absence for shared rows only. A posting that closed the day after it
reached a board stayed there until the 5-day sweep — the "dead jobs" users
kept opening. Two conservative fixes, pinned here:

  * absence from a COMPLETE board listing closes EVERY copy (0 HTTP), with
    guards against partial/empty listings and mass absence;
  * postings already on boards are re-checked on a bounded schedule, and only
    REMOVED/EXPIRED closes them — a refusal never does.

Rows are prefixed `dad-` and cleaned up by that prefix.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlmodel import delete, select

from app.api import server
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    Application, ApplicationStatus, FunnelEvent, Job, JobLiveness, JobLivenessState, JobSource,
)
from app.discovery import liveness as lv
from app.discovery.pipeline import SHARED_POOL_USER
from app.strategy import delivery_gate as gate

_P = "dad-"
_CO = "Dead After Delivery Co"


@pytest.fixture(autouse=True)
def _clean():
    gate.metrics_snapshot(reset=True)
    yield
    with get_session() as s:
        s.exec(delete(JobLiveness).where(JobLiveness.external_id.like(f"{_P}%")))
        ids = [j for j in s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if ids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(ids)))
            s.exec(delete(Application).where(Application.job_id.in_(ids)))
            s.exec(delete(Job).where(Job.id.in_(ids)))
        s.commit()


def _job(user_id, ext, *, source=JobSource.GREENHOUSE, company=_CO, closed=False) -> int:
    with get_session() as s:
        j = Job(user_id=user_id, source=source, external_id=ext, company=company,
                title=f"Engineer {ext}", url=f"https://boards.greenhouse.io/x/jobs/{ext}",
                description="d", discovered_at=datetime.utcnow(), first_seen=datetime.utcnow(),
                is_closed=closed)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _app(user_id, job_id, status) -> int:
    with get_session() as s:
        a = Application(user_id=user_id, job_id=job_id, status=status,
                        created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        s.add(a)
        s.commit()
        s.refresh(a)
        return a.id


def _state(job_id):
    with get_session() as s:
        j = s.get(Job, job_id)
        a = s.exec(select(Application).where(Application.job_id == job_id)).first()
        return (bool(j.is_closed), j.closed_reason or "", a.status if a else None, (a.notes or "") if a else "")


# ── re-verification of postings already on boards ───────────────────────────

def _patch_fetch(monkeypatch, status, body=""):
    monkeypatch.setattr(gate, "_fetch", lambda url, timeout: (status, url, body, None))


def test_a_delivered_posting_found_404_is_closed_everywhere(monkeypatch):
    monkeypatch.setattr(settings, "liveness_gate_enabled", True)
    import app.common.daily_counter as dc
    monkeypatch.setattr(dc, "reserve", lambda name, cap, day=None: True)
    shared = _job(SHARED_POOL_USER, f"{_P}rv1")
    mine = _job("dad-u1", f"{_P}rv1")
    _app("dad-u1", mine, ApplicationStatus.SHORTLISTED)
    _patch_fetch(monkeypatch, 404)
    out = gate.reverify_delivered(max_seconds=10, max_checks=10, min_age_hours=24, daily_cap=100)
    assert out["candidates"] == 1 and out["checked"] == 1 and out["dead"] == 1
    assert out["closed"] == 2
    assert _state(mine)[0] is True and _state(mine)[2] == ApplicationStatus.SKIPPED
    assert _state(shared)[0] is True


def test_a_refusal_or_a_live_answer_changes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "liveness_gate_enabled", True)
    import app.common.daily_counter as dc
    monkeypatch.setattr(dc, "reserve", lambda name, cap, day=None: True)
    mine = _job("dad-u1", f"{_P}rv2")
    _app("dad-u1", mine, ApplicationStatus.SHORTLISTED)
    _patch_fetch(monkeypatch, 429)
    out = gate.reverify_delivered(max_seconds=10, max_checks=10, min_age_hours=24, daily_cap=100)
    assert out["checked"] == 1 and out["dead"] == 0 and _state(mine)[0] is False
    # A verdict younger than the age bar — even an inconclusive one — is not
    # re-checked (a throttling host is not hammered); once it ages, it is.
    out = gate.reverify_delivered(max_seconds=10, max_checks=10, min_age_hours=24, daily_cap=100)
    assert out["checked"] == 0 and out["due"] == 0
    with get_session() as s:
        row = s.exec(select(JobLiveness).where(JobLiveness.external_id == f"{_P}rv2")).first()
        row.checked_at = datetime.utcnow() - timedelta(hours=25)
        s.add(row)
        s.commit()
    _patch_fetch(monkeypatch, 200, body="Senior Engineer. Apply now.")
    out = gate.reverify_delivered(max_seconds=10, max_checks=10, min_age_hours=24, daily_cap=100)
    assert out["checked"] == 1 and _state(mine)[0] is False
    out = gate.reverify_delivered(max_seconds=10, max_checks=10, min_age_hours=24, daily_cap=100)
    assert out["checked"] == 0 and out["due"] == 0


def test_the_daily_cap_stops_requests_not_bookkeeping(monkeypatch):
    monkeypatch.setattr(settings, "liveness_gate_enabled", True)
    import app.common.daily_counter as dc
    monkeypatch.setattr(dc, "reserve", lambda name, cap, day=None: False)
    calls = []
    monkeypatch.setattr(gate, "_fetch", lambda url, timeout: calls.append(url) or (404, url, "", None))
    mine = _job("dad-u1", f"{_P}rv3")
    _app("dad-u1", mine, ApplicationStatus.SHORTLISTED)
    out = gate.reverify_delivered(max_seconds=10, max_checks=10, min_age_hours=24, daily_cap=1)
    assert out["skipped_cap"] == 1 and out["checked"] == 0 and calls == []
    # Known dead (the free board signal landed after delivery) still closes, no request.
    lv.record("greenhouse", f"{_P}rv3", JobLivenessState.REMOVED.value, reason="absent_from_complete_board_fetch")
    out = gate.reverify_delivered(max_seconds=10, max_checks=10, min_age_hours=24, daily_cap=1)
    assert out["closed_known_dead"] == 1 and calls == [] and _state(mine)[0] is True


def test_engaged_work_keeps_its_place_on_reverification(monkeypatch):
    monkeypatch.setattr(settings, "liveness_gate_enabled", True)
    import app.common.daily_counter as dc
    monkeypatch.setattr(dc, "reserve", lambda name, cap, day=None: True)
    mine = _job("dad-u1", f"{_P}rv4")
    _app("dad-u1", mine, ApplicationStatus.SHORTLISTED)
    theirs = _job("dad-u2", f"{_P}rv4")
    _app("dad-u2", theirs, ApplicationStatus.SUBMITTED)
    _patch_fetch(monkeypatch, 410)
    gate.reverify_delivered(max_seconds=10, max_checks=10, min_age_hours=24, daily_cap=100)
    assert _state(mine)[2] == ApplicationStatus.SKIPPED
    assert _state(theirs) [2] == ApplicationStatus.SUBMITTED and _state(theirs)[0] is True


# ── wiring: the lanes call it ────────────────────────────────────────────────

def test_the_pulse_lane_routes_absence_through_board_absence():
    """Postings that left a completely fetched board are closed by main's
    board_absence census (pulse_lane._queue_absence → _run_absence); the
    branch's own closer was retired in the merge so there is ONE
    implementation. The re-verification below is the complementary HTTP path."""
    from app.strategy import pulse_lane
    src = inspect.getsource(pulse_lane._run_pulse_tick_locked)
    assert "_queue_absence(" in src and "close_absent_from_board" not in src


def test_the_scoring_lane_closes_every_copy_and_reverifies_after_scoring():
    from app.strategy import scoring_lane
    shortlist = inspect.getsource(scoring_lane._shortlist_user)
    assert "_close_dead(jid, _pair)" in shortlist and "_dead_at_place" in shortlist
    assert "close_dead_everywhere" in shortlist
    cycle = inspect.getsource(scoring_lane._run_scoring_cycle)
    i_rev = cycle.index("reverify_delivered")
    # AFTER the shortlist phase, never before scoring (the housekeeping rule).
    assert i_rev > cycle.index("_shortlist_user(uid, results, stats)")
    assert "liveness_reverify_per_cycle" in cycle


def test_workday_tells_listing_completeness_apart_from_detail_completeness():
    from app.discovery import workday, smartrecruiters
    src = inspect.getsource(workday.WorkdayScraper.fetch)
    assert "self.listing_complete = True" in src
    assert src.count("self.listing_complete = False") >= 2      # page failure, cap truncation — never a detail GET
    assert "capped = True" in src and src.index("capped = True") > src.index("self.listing_complete = False")
    assert "self.listing_complete = self.fetch_complete" in inspect.getsource(smartrecruiters)


def test_reverify_settings_are_bounded():
    assert 0 < settings.liveness_reverify_per_cycle <= 100
    assert 0 < settings.liveness_reverify_seconds_per_cycle <= 60
    assert settings.liveness_reverify_hours >= settings.liveness_recheck_hours
    assert settings.liveness_reverify_daily_cap >= 100


# ── the metric ───────────────────────────────────────────────────────────────

def test_admin_dead_jobs_counts_per_source(monkeypatch):
    monkeypatch.setattr(server, "_require_admin_user", lambda request: "admin")
    dead = _job("dad-u1", f"{_P}metric-dead", closed=True)
    live = _job("dad-u1", f"{_P}metric-live")
    with get_session() as s:
        for jid in (dead, live):
            s.add(FunnelEvent(job_id=jid, stage="placement", passed=True, reason="placed",
                              created_at=datetime.utcnow() - timedelta(days=1)))
        s.commit()
    with get_session() as s:
        j = s.get(Job, dead)
        j.closed_reason = "Reported no longer available by the user"
        s.add(j)
        s.commit()
    out = server.admin_dead_jobs(request=None, days=14)
    gh = next(x for x in out["sources"] if x["source"] == "greenhouse")
    assert gh["placed"] >= 2 and gh["closed_after"] >= 1 and gh["reported_by_users"] >= 1
    assert out["total_placed"] >= 2 and out["dead_after_delivery_rate"] is not None
    assert out["degraded"] is False


def test_admin_dead_jobs_is_admin_only(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True))
    anonymous = SimpleNamespace(headers={}, cookies={})
    with pytest.raises(HTTPException) as e:
        server.admin_dead_jobs(request=anonymous, days=14)
    assert e.value.status_code in (401, 403)
