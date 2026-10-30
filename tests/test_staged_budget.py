"""The staged finals ceiling and "Strong match · not on board" (founder 2026-10-30).

Production 2026-10-30: one Pro user's board filled by 15:30 UTC, the day's 250
finals were spent, and 121 fresh jobs that had already passed Tier-1 waited for
the next day. The ceiling is now one counter at three heights:

    fill        250   until the board is full
    challenge   400   board full — a later, better job may replace an UNOPENED one
    engaged     500   board full AND the user tailored/applied to 2 jobs today

and the slate is REPLACE ONLY: a strong job that cannot take a slot keeps its
score and stays in All Jobs, under "Strong match · not on board", as does the
entry a challenger replaced.

Rows are prefixed ``stg_`` and cleaned by that prefix.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    PLAN_LIMITS, Application, ApplicationStatus, Job, JobSource, PlanTier,
)
from app.matching import finals_budget as fb
from app.strategy import scoring_lane as sl
from app.strategy import slate

_P = "stg_"
UID = "stg_user"


@pytest.fixture(autouse=True)
def _clean_state():
    fb.reset_state()
    _clean()
    yield
    _clean()
    fb.reset_state()


def _clean():
    with get_session() as s:
        jids = [r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.commit()


def _day(monkeypatch, *, delivered: int, spent: int, engaged: int = 0, gate: int = 80):
    monkeypatch.setattr(fb, "delivered_today", lambda uid, cached=True: delivered)
    # A healthy ~15% hit rate (measured 2026-10-30: 35 of 250), so the yield
    # stop — a different guard — stays out of these tests.
    monkeypatch.setattr(fb, "day_counts", lambda uid: (spent, int(spent * 0.15)))
    monkeypatch.setattr(fb, "engaged_today", lambda uid: engaged)
    monkeypatch.setattr(fb, "challenger_gate", lambda uid: gate)


def _pro(uid=UID, cap=40):
    pro = PLAN_LIMITS[PlanTier.PRO]
    return fb.allowance(uid, per_cycle_cap=cap, ceiling=pro["finals_daily"],
                        target=pro["shortlist_daily"],
                        challenge_extra=pro["finals_challenge_extra"],
                        engaged_extra=pro["finals_engaged_extra"])


# ── the numbers ──────────────────────────────────────────────────────────────

def test_pro_climbs_250_400_500_and_free_does_not_move():
    pro, free = PLAN_LIMITS[PlanTier.PRO], PLAN_LIMITS[PlanTier.FREE]
    base = pro["finals_daily"]
    assert (base, base + pro["finals_challenge_extra"],
            base + pro["finals_challenge_extra"] + pro["finals_engaged_extra"]) == (250, 400, 500)
    assert free["finals_challenge_extra"] == free["finals_engaged_extra"] == 0
    assert settings.finals_engaged_min_jobs == 2
    for tier in (PlanTier.BASIC, PlanTier.AGENCY):          # legacy rows count as Pro
        for key in ("finals_challenge_extra", "finals_engaged_extra"):
            assert PLAN_LIMITS[tier][key] == pro[key], (tier, key)


# ── fill ─────────────────────────────────────────────────────────────────────

def test_a_board_that_never_fills_stops_at_the_base(monkeypatch):
    """The extras exist to let a FULL board improve. A thin day — nothing good
    enough to fill it — still stops at finals_daily, exactly as before."""
    _day(monkeypatch, delivered=12, spent=250)
    a = _pro()
    assert a.n == 0
    assert a.reason.startswith("daily cost ceiling") and "fill stage" in a.reason
    assert a.target_met is False


# ── challenge ────────────────────────────────────────────────────────────────

def test_a_full_board_opens_the_challenge_stage(monkeypatch):
    _day(monkeypatch, delivered=35, spent=250, gate=80)
    a = _pro()
    assert a.n == 40, "the board is full and 250 are spent: the challenge stage must open"
    assert a.gate == 80, "challenge finals are bought only at the raised challenger gate"
    assert "challenge stage" in a.reason and a.target_met is True

    _day(monkeypatch, delivered=35, spent=390)
    assert _pro().n == 10, "what is LEFT of the stage, not a final more"


def test_the_challenge_stage_ends_at_400_and_says_how_to_open_more(monkeypatch):
    _day(monkeypatch, delivered=35, spent=400, engaged=0)
    a = _pro()
    assert a.n == 0
    assert a.reason.startswith("daily cost ceiling"), "the stop must keep its bucket name"
    assert sl._stop_bucket(a.reason) == "cost_ceiling"
    assert "400/400" in a.reason and "challenge stage" in a.reason
    assert "2 tailored/applied today opens +100" in a.reason
    assert a.target_met is True, "a full board is a delivered day, whatever stopped the spend"


# ── engaged ──────────────────────────────────────────────────────────────────

def test_working_the_board_opens_the_engaged_stage(monkeypatch):
    _day(monkeypatch, delivered=35, spent=400, engaged=2)
    a = _pro()
    assert a.n == 40 and "engaged stage" in a.reason

    _day(monkeypatch, delivered=35, spent=500, engaged=5)
    a = _pro()
    assert a.n == 0 and "500/500" in a.reason and "engaged stage" in a.reason


def test_one_tailored_job_is_not_enough(monkeypatch):
    _day(monkeypatch, delivered=35, spent=400, engaged=1)
    assert _pro().n == 0


def test_engagement_is_read_only_once_the_challenge_stage_is_spent(monkeypatch):
    """It is a query; until the challenge stage runs out it cannot change the
    answer, so the lanes (one decision per user per cycle) must not pay for it."""
    def _boom(uid):
        raise AssertionError("engaged_today read before it could matter")
    for delivered, spent in ((10, 100), (35, 260), (35, 399)):
        _day(monkeypatch, delivered=delivered, spent=spent)
        monkeypatch.setattr(fb, "engaged_today", _boom)
        assert _pro().n > 0


def test_min_jobs_zero_opens_the_engaged_extra_with_the_board(monkeypatch):
    monkeypatch.setattr(settings, "finals_engaged_min_jobs", 0)
    _day(monkeypatch, delivered=35, spent=400, engaged=0)
    assert _pro().n == 40


def test_the_kill_switch_also_removes_the_extras(monkeypatch):
    """SLATE_CHALLENGE_ENABLED=0 restores the old stop — including its ceiling."""
    monkeypatch.setattr(settings, "slate_challenge_enabled", False)
    _day(monkeypatch, delivered=35, spent=100)
    assert _pro().n == 0
    assert fb.staged_ceiling(UID, 250, 260, True, 150, 100) == (250, "fill")


def test_no_plan_ceiling_stays_unbounded_by_the_extras():
    assert fb.staged_ceiling(UID, 0, 999, True, 150, 100) == (0, "fill")


# ── the lane wires the plan in ───────────────────────────────────────────────

def test_the_lane_passes_the_plans_extras(monkeypatch):
    monkeypatch.setattr(sl, "_plan_budget", lambda uid: (250, 35))
    monkeypatch.setattr(sl, "_plan_extras", lambda uid: (150, 100))
    monkeypatch.setattr(settings, "prescore_budget_multiplier", 0)
    _day(monkeypatch, delivered=35, spent=300)
    assert sl._finals_allowance(UID, 40).n == 40
    monkeypatch.setattr(sl, "_plan_extras", lambda uid: (0, 0))
    assert sl._finals_allowance(UID, 40).n == 0


def test_the_plan_extras_fall_back_to_the_widest_plan(monkeypatch):
    import app.api.server as server

    def _down(uid):
        raise RuntimeError("billing unavailable")
    monkeypatch.setattr(server, "_get_user_plan", _down)
    assert sl._plan_extras(UID) == (150, 100)
    assert sl._plan_extras("local") == (0, 0)


# ── engaged_today reads what only a person writes ────────────────────────────

def _app(session, ext, *, uid=UID, track="autofill", status=ApplicationStatus.SHORTLISTED,
         tailored_at=None, submitted_at=None, updated_at=None, notes=None, score=90.0,
         with_application=True):
    j = Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=_P + ext,
            company=f"Co-{ext}", title=f"zzstaged Engineer {ext}", location="Austin, TX",
            remote=False, url=f"https://x/{_P}{ext}", description="d",
            rerank_score=score, scored_at=datetime.utcnow(), first_seen=datetime.utcnow())
    session.add(j)
    session.commit()
    session.refresh(j)
    if not with_application:
        return j
    a = Application(job_id=j.id, user_id=uid, status=status, apply_url=j.url,
                    apply_track=track, tailored_at=tailored_at, submitted_at=submitted_at,
                    notes=notes)
    if updated_at is not None:
        a.updated_at = updated_at
    session.add(a)
    session.commit()
    return j


def test_engaged_today_counts_tailors_and_submits_only():
    now = fb._utc_now()                 # THE budget clock (pinned by conftest)
    with get_session() as s:
        _app(s, "t", tailored_at=now, status=ApplicationStatus.TAILORED)
        _app(s, "s", submitted_at=now, status=ApplicationStatus.SUBMITTED)
        _app(s, "old", tailored_at=now - timedelta(days=2), status=ApplicationStatus.TAILORED)
        _app(s, "touched", updated_at=now)                       # a lane bumped it
        _app(s, "import", track="email_import", submitted_at=now,
             status=ApplicationStatus.SUBMITTED)
        _app(s, "other", uid="stg_other", tailored_at=now, status=ApplicationStatus.TAILORED)
    assert fb.engaged_today(UID) == 2
    assert fb.engaged_today("local") == 0 and fb.engaged_today(None) == 0


# ── replace only, and what it leaves in All Jobs ─────────────────────────────

def test_a_replaced_entry_carries_the_challenger_note(monkeypatch):
    """The All Jobs filter matches on SLATE_CHALLENGER_NOTE; it must be what
    place() actually writes, or a replaced job silently vanishes from it."""
    import app.common.plan_limits as pl
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 2)
    with get_session() as s:
        _app(s, "w1", score=76.0)
        _app(s, "w2", score=80.0)
    with get_session() as s:
        j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + "champ",
                company="Co-champ", title="zzstaged Engineer champ", location="Austin, TX",
                remote=False, url="https://x/champ", description="d", rerank_score=92.0,
                first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        res = slate.place(s, j, 92.0, user_id=UID)
        s.commit()
    assert res.outcome == "replaced"
    with get_session() as s:
        out = s.get(Application, res.displaced_id)
    assert out.status == ApplicationStatus.SKIPPED
    assert slate.SLATE_CHALLENGER_NOTE in (out.notes or "")


def test_all_jobs_lists_strong_matches_that_are_not_on_the_board():
    from fastapi.testclient import TestClient
    from app.api.server import app
    from app.matching.preference_learning import USER_DISMISS_MARKER

    with get_session() as s:
        _app(s, "lost", with_application=False)       # a challenger that took no slot
        _app(s, "replaced", status=ApplicationStatus.SKIPPED,
             notes=f"{slate.SLATE_CHALLENGER_NOTE} ('X' at 95 vs 76)")
        _app(s, "dismissed", status=ApplicationStatus.SKIPPED, notes=USER_DISMISS_MARKER)
        _app(s, "dupe", status=ApplicationStatus.SKIPPED,
             notes=f"{slate.SLATE_REPLACED_MARKER}: replaced by the employer's own posting")
        _app(s, "onboard")                                      # SHORTLISTED
        _app(s, "weak", score=60.0, with_application=False)   # below the bar

    client = TestClient(app)
    r = client.get("/api/jobs", params={"status": "not_on_board", "search": "zzstaged",
                                        "max_age_days": 0})
    assert r.status_code == 200, r.text
    data = r.json()
    titles = sorted(j["title"].rsplit(" ", 1)[-1] for j in data["jobs"])
    assert titles == ["lost", "replaced"], titles
    assert data["total"] == 2
    by_title = {j["title"].rsplit(" ", 1)[-1]: j for j in data["jobs"]}
    assert by_title["lost"]["application"] is None
    assert by_title["replaced"]["application"]["replaced"] is True

    # The ordinary listing still answers `replaced` for every application row.
    r = client.get("/api/jobs", params={"search": "zzstaged", "max_age_days": 0})
    rows = {j["title"].rsplit(" ", 1)[-1]: j for j in r.json()["jobs"]}
    assert rows["onboard"]["application"]["replaced"] is False
    assert rows["dupe"]["application"]["replaced"] is False
