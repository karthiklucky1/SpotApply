"""A location-preference change re-decides the jobs it held (2026-10-28).

The founder's board: Boston postings in "Location pending" for 22 days after
they had turned on "Open to relocating". The verdict was decided at intake
under the OLD preferences ("Located in Boston … not your area, and the posting
does not say whether it is on-site or remote"), and only two things ever
re-decided it: new evidence about the posting, or — for SCORED jobs — the
profile-edit release. An unscored held job waited on verification alone.

`geo_verify.redecide_user_copies` decides the user's held and location-stamped
UNSCORED copies again from the stored geography and the NEW preferences.
Synthetic rows only, prefixed ``lpr_`` and cleaned by that prefix.
"""
from __future__ import annotations

import inspect
from datetime import datetime

import pytest
from sqlmodel import delete, select

from app.common.eligibility import ELIGIBLE, INELIGIBLE, UNKNOWN
from app.db.init_db import get_session
from app.db.models import Application, Job, JobGeography, JobSource, UserProfile
from app.discovery import geo_verify

_P = "lpr_"
UID = "lpr_user"

BOSTON_NO_MODE = dict(status="resolved", countries_json='["united states"]',
                      sites_json='["Boston, United States of America"]', work_mode=None)
DALLAS_ONSITE = dict(status="resolved", countries_json='["united states"]',
                     sites_json='["Dallas, TX"]', work_mode="onsite")
BERLIN_ONSITE = dict(status="resolved", countries_json='["germany"]',
                     sites_json='["Berlin, Germany"]', work_mode="onsite")


def _clean():
    with get_session() as s:
        s.exec(delete(Application).where(Application.user_id == UID))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.exec(delete(JobGeography).where(JobGeography.external_id.like(f"{_P}%")))
        s.exec(delete(UserProfile).where(UserProfile.user_id == UID))
        s.commit()


@pytest.fixture()
def user():
    _clean()
    with get_session() as s:
        s.add(UserProfile(user_id=UID, preferred_country="United States",
                          location="Cincinnati, OH", open_to_relocation=False))
        s.commit()
    yield
    _clean()


def _held(ext, geo, *, eligibility=UNKNOWN, reason="Located in Boston, which is not your area",
          score=None, reasoning=None, geography=True):
    with get_session() as s:
        if geography:
            s.add(JobGeography(source="workday", external_id=_P + ext, created_at=datetime.utcnow(),
                               updated_at=datetime.utcnow(), **geo))
        j = Job(user_id=UID, source=JobSource.WORKDAY, external_id=_P + ext, company="Bcbsma",
                title="Sr. AI Engineer", url=f"https://x/{ext}", description="d",
                location="Boston", eligibility=eligibility, eligibility_reason=reason,
                rerank_score=score, rerank_reasoning=reasoning, first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _relocate(on=True, targets=""):
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == UID)).one()
        p.open_to_relocation = on
        p.relocation_targets = targets
        s.add(p)
        s.commit()


def _job(jid):
    with get_session() as s:
        j = s.get(Job, jid)
        s.expunge(j)
        return j


def test_turning_on_relocation_releases_a_held_boston_job(user):
    jid = _held("boston", BOSTON_NO_MODE)
    _relocate(True)
    stats = geo_verify.redecide_user_copies(UID, force=True)
    assert stats["released"] == 1
    j = _job(jid)
    assert j.eligibility == ELIGIBLE and j.rerank_score is None   # in the queue now


def test_a_location_stamped_job_rejoins_the_queue(user):
    jid = _held("dallas", DALLAS_ONSITE, eligibility=INELIGIBLE,
                reason="Onsite in Dallas, TX; you are not open to relocation",
                score=geo_verify.INELIGIBLE_STAMP_SCORE,
                reasoning=geo_verify.INELIGIBLE_REASON_PREFIX + "Onsite in Dallas, TX")
    _relocate(True)
    geo_verify.redecide_user_copies(UID, force=True)
    j = _job(jid)
    assert j.eligibility == ELIGIBLE
    assert j.rerank_score is None and j.rerank_reasoning is None and j.scored_at is None


def test_a_job_that_still_fails_is_stamped_and_another_country_stays_out(user):
    boston = _held("boston2", BOSTON_NO_MODE)
    berlin = _held("berlin", BERLIN_ONSITE)
    _relocate(True, targets="Chicago, IL")          # will move, but only to Chicago
    geo_verify.redecide_user_copies(UID, force=True)
    # Boston, no work mode: not a relocation target and not home — still held.
    assert _job(boston).eligibility == UNKNOWN
    b = _job(berlin)
    assert b.eligibility == INELIGIBLE and b.rerank_score == geo_verify.INELIGIBLE_STAMP_SCORE


def test_scored_delivered_and_pre_rollout_copies_are_left_alone(user):
    scored = _held("scored", BOSTON_NO_MODE, score=82.0, reasoning="Strong fit")
    delivered = _held("delivered", BOSTON_NO_MODE)
    legacy = _held("legacy", BOSTON_NO_MODE, geography=False)
    with get_session() as s:
        s.add(Application(job_id=delivered, user_id=UID, status="shortlisted"))
        s.commit()
    _relocate(True)
    stats = geo_verify.redecide_user_copies(UID, force=True)
    assert _job(scored).rerank_score == 82.0          # the release path owns scored jobs
    assert _job(delivered).eligibility == UNKNOWN     # on a board: its verdict is kept
    assert _job(legacy).eligibility == UNKNOWN and stats["no_geography"] == 1


def test_an_unreadable_profile_decides_nothing(user, monkeypatch):
    jid = _held("noprefs", BOSTON_NO_MODE)
    import app.common.tenant_prefs as tp
    monkeypatch.setattr(tp, "geo_prefs_for_user", lambda uid: None)
    assert geo_verify.redecide_user_copies(UID, force=True)["examined"] == 0
    assert _job(jid).eligibility == UNKNOWN


def test_newest_first_one_run_at_a_time_and_not_twice_a_minute(user):
    """The setup saves the profile and then the roles seconds apart; both
    re-decide. One run per user at a time, none again inside a minute unless a
    location edit forces it, and the newest copies are the ones read."""
    old = _held("old", BOSTON_NO_MODE)
    new = _held("new", BOSTON_NO_MODE)
    _relocate(True)
    assert geo_verify.redecide_user_copies(UID, limit=1, force=True)["released"] == 1
    assert _job(new).eligibility == ELIGIBLE and _job(old).eligibility == UNKNOWN
    assert geo_verify.redecide_user_copies(UID).get("skipped") == 1          # inside the minute
    with geo_verify._redecide_lock:
        geo_verify._redecide_running.add(UID)
    try:
        assert geo_verify.redecide_user_copies(UID, force=True).get("skipped") == 1
    finally:
        with geo_verify._redecide_lock:
            geo_verify._redecide_running.discard(UID)


def test_the_profile_save_and_the_role_save_both_re_decide():
    from app.api import server
    from app.strategy import adoption
    upd = inspect.getsource(server.update_profile)
    assert "redecide_user_copies" in upd and "_LOCATION_PREF_FIELDS" in upd
    assert '"force": True' in upd
    seed = inspect.getsource(adoption.seed_new_user)
    assert seed.index("redecide_user_copies") < seed.index("adopt_and_match(user_id)")
