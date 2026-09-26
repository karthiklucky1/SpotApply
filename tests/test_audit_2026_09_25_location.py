"""Location findings from the 2026-09-25 production audit (1, 2 and 3).

1. A stamped verdict outlives the rules, evidence and preferences it was
   written under. `slate.place()` now re-decides at delivery from the posting's
   CURRENT geography and the user's CURRENT profile, writes the fresh verdict
   back, and records the rule/verifier versions in the placement event.
2. Structured metadata can contradict the employer's own sentence: an Ashby
   posting stored as TELECOMMUTE required three days a week in San Francisco.
   The attendance sentence now wins, and the office it names becomes a site.
3. Relocation was a boolean. "Will relocate to Chicago only" admitted an
   on-site role in Dallas, and a profile with no home location and relocation
   off admitted it too.

Synthetic data only. Rows are prefixed ``aud925_`` and cleaned by that prefix.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlmodel import delete, select

from app.common.eligibility import (ELIGIBLE, INELIGIBLE, RULES_VERSION, UNKNOWN,
                                    GeoPrefs, Geography, decide)
from app.db.init_db import get_session
from app.db.models import (Application, FunnelEvent, Job, JobGeography, JobSource,
                           UserProfile)

_P = "aud925_"
UID = "aud925_user"

DALLAS_ONSITE = Geography(status="resolved", countries=["united states"],
                          sites=["Dallas, TX"], work_mode="onsite")


# ── 3. relocation destinations bind ──────────────────────────────────────────

def test_relocation_to_chicago_only_does_not_admit_dallas():
    prefs = GeoPrefs(country="united states", open_to_relocation=True,
                     home_location="Cincinnati, OH", relocation_targets=("chicago, il",))
    d = decide(DALLAS_ONSITE, prefs)
    assert d.status == INELIGIBLE and d.code == "relocation_target_excluded"


def test_relocation_to_the_named_city_is_admitted():
    prefs = GeoPrefs(country="united states", open_to_relocation=True,
                     home_location="Cincinnati, OH", relocation_targets=("dallas, tx",))
    assert decide(DALLAS_ONSITE, prefs).status == ELIGIBLE


def test_relocation_to_a_state_admits_its_cities():
    prefs = GeoPrefs(country="united states", open_to_relocation=True,
                     home_location="Cincinnati, OH", relocation_targets=("texas",))
    assert decide(DALLAS_ONSITE, prefs).status == ELIGIBLE


@pytest.mark.parametrize("targets", [(), ("nationwide",)])
def test_unstated_or_nationwide_relocation_keeps_its_old_meaning(targets):
    prefs = GeoPrefs(country="united states", open_to_relocation=True,
                     home_location="Cincinnati, OH", relocation_targets=targets)
    assert decide(DALLAS_ONSITE, prefs).status == ELIGIBLE


def test_home_town_is_eligible_whatever_the_targets_say():
    prefs = GeoPrefs(country="united states", open_to_relocation=True,
                     home_location="Dallas, TX", relocation_targets=("chicago, il",))
    assert decide(DALLAS_ONSITE, prefs).status == ELIGIBLE


def test_no_home_location_and_no_relocation_is_held_not_admitted():
    """The audit's second reproduction: a Dallas on-site job passed a profile
    with no home location and relocation disabled."""
    d = decide(DALLAS_ONSITE, GeoPrefs(country="united states", home_location=""))
    assert d.status == UNKNOWN and d.code == "home_location_missing"
    assert "add your city" in d.reason


def test_a_remote_role_needs_no_home_location():
    remote = Geography(status="resolved", countries=["united states"],
                       sites=["Remote - US"], work_mode="remote")
    assert decide(remote, GeoPrefs(country="united states")).status == ELIGIBLE


def test_tenant_prefs_read_the_saved_targets():
    from app.common.tenant_prefs import geo_prefs

    class P:
        preferred_country = "United States"
        remote_ok = True
        open_to_relocation = True
        location = "Cincinnati, OH"
        relocation_targets = "Chicago, IL; Austin, TX"
    prefs = geo_prefs(P())
    assert prefs.relocation_targets == ("chicago, il", "austin, tx")


# ── 2. the employer's attendance sentence beats a structured "remote" ────────

HINGE_LIKE = ("About the role. This is a hybrid role: you will work 3 days per week "
              "in our San Francisco office. We offer great benefits.")


def _ashby_remote(description: str):
    from app.discovery.base import GeoEvidence, RawJob
    return RawJob(source="ashby", external_id=_P + "h1", company="Co", title="SWE",
                  location="US Remote", remote=True, url="https://x/h1",
                  description=description,
                  geo=GeoEvidence(sites=["United States"], country="US",
                                  work_mode="remote", work_mode_field="workplaceType"))


def test_structured_telecommute_contradicted_by_attendance_is_hybrid():
    from app.discovery.geo_verify import derive
    g = derive(_ashby_remote(HINGE_LIKE))
    assert g.work_mode == "hybrid"
    assert "San Francisco" in g.sites
    assert "3 days per week" in g.evidence_quote
    # …and the decision follows the office, not the form field.
    far = decide(g, GeoPrefs(country="united states", home_location="Austin, TX"))
    near = decide(g, GeoPrefs(country="united states", home_location="San Francisco, CA"))
    assert far.status == INELIGIBLE and "San Francisco" in far.reason
    assert near.status == ELIGIBLE


def test_an_occasional_onsite_week_does_not_make_a_remote_role_onsite():
    from app.discovery.geo_verify import derive
    g = derive(_ashby_remote("We are a remote team. You may be required to be on-site "
                             "for a planning week each quarter."))
    assert g.work_mode == "remote"


def test_attendance_with_no_named_office_is_held_not_guessed():
    from app.discovery.geo_verify import derive
    g = derive(_ashby_remote("This is a hybrid role with 2 days per week in the office."))
    assert g.work_mode == "hybrid"
    d = decide(g, GeoPrefs(country="united states", home_location="Austin, TX"))
    assert d.status == UNKNOWN and d.code == "attendance_place_unresolved"


# ── 1. placement re-decides from current evidence and records the versions ──

def _clean():
    with get_session() as s:
        jids = [r for r in s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        jids = [j[0] if isinstance(j, tuple) else j for j in jids]
        if jids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.exec(delete(JobGeography).where(JobGeography.external_id.like(f"{_P}%")))
        s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
        s.commit()


@pytest.fixture()
def clean(monkeypatch):
    import app.common.plan_limits as pl
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 50)
    _clean()
    yield
    _clean()


def _seed(ext: str, *, stamped: str, relocation_targets: str = ""):
    with get_session() as s:
        s.add(UserProfile(user_id=UID, preferred_country="United States",
                          location="Cincinnati, OH", open_to_relocation=True,
                          relocation_targets=relocation_targets))
        s.add(JobGeography(source="greenhouse", external_id=_P + ext, status="resolved",
                           countries_json='["united states"]', sites_json='["Dallas, TX"]',
                           work_mode="onsite", created_at=datetime.utcnow(),
                           updated_at=datetime.utcnow()))
        j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + ext,
                company="Co", title="Engineer", url=f"https://x/{ext}",
                description="d", location="Dallas, TX", rerank_score=85,
                eligibility=stamped, eligibility_reason="stamped earlier",
                first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        s.exec(delete(FunnelEvent).where(FunnelEvent.job_id == j.id))
        s.exec(delete(Application).where(Application.job_id == j.id))
        s.commit()
        return j.id


def _place(jid: int):
    from app.strategy import slate
    with get_session() as s:
        job = s.get(Job, jid)
        p = slate.place(s, job, 85.0, user_id=UID)
        s.commit()
        return p


def _last_event(jid: int) -> dict:
    with get_session() as s:
        ev = s.exec(select(FunnelEvent).where(FunnelEvent.job_id == jid)
                    .order_by(FunnelEvent.id.desc())).first()
        return json.loads(ev.metadata_json) if ev else {}


def test_a_stale_eligible_stamp_is_re_decided_at_delivery(clean):
    """THE DEFECT: the slate trusted the stamp. The profile now says Chicago
    only, so the Dallas on-site job must not reach the board."""
    jid = _seed("stale", stamped=ELIGIBLE, relocation_targets="Chicago, IL")
    p = _place(jid)
    assert not p.created and p.outcome == "ineligible"
    with get_session() as s:
        job = s.get(Job, jid)
        assert job.eligibility == INELIGIBLE
        assert "relocate" in job.eligibility_reason
    meta = _last_event(jid)["eligibility"]
    assert meta["rules_version"] == RULES_VERSION
    assert meta["code"] == "relocation_target_excluded"


def test_a_delivered_job_records_the_versions_it_was_decided_under(clean):
    jid = _seed("ok", stamped=ELIGIBLE, relocation_targets="Texas")
    p = _place(jid)
    assert p.created and p.outcome == "placed"
    meta = _last_event(jid)["eligibility"]
    assert meta["decision"] == ELIGIBLE
    assert meta["rules_version"] == RULES_VERSION
    assert meta["geography"] == "resolved"
    assert "verifier_version" in meta


def test_a_pre_rollout_copy_keeps_its_stamp(clean):
    """No geography row: nothing fresher to decide from."""
    with get_session() as s:
        j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + "legacy",
                company="Co", title="Engineer", url="https://x/legacy", description="d",
                location="Remote", rerank_score=85, eligibility=None,
                first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        s.exec(delete(FunnelEvent).where(FunnelEvent.job_id == j.id))
        s.exec(delete(Application).where(Application.job_id == j.id))
        s.commit()
        jid = j.id
    p = _place(jid)
    assert p.created
    assert _last_event(jid)["eligibility"]["geography"] == "none"


def test_the_recheck_script_is_read_only_by_default(clean):
    from scripts.recheck_eligibility import run
    jid = _seed("script", stamped=ELIGIBLE, relocation_targets="Chicago, IL")
    with get_session() as s:
        s.add(Application(job_id=jid, user_id=UID, status="shortlisted"))
        s.commit()
    stats = run(apply=False, limit=50, batch=10, user=UID)
    assert stats["eligible->ineligible"] == 1
    with get_session() as s:
        assert s.get(Job, jid).eligibility == ELIGIBLE, "a dry run writes nothing"
    run(apply=True, limit=50, batch=10, user=UID)
    with get_session() as s:
        assert s.get(Job, jid).eligibility == INELIGIBLE
        app_row = s.exec(select(Application).where(Application.job_id == jid)).first()
        assert app_row.status.value == "shortlisted", "the entry itself is never removed"


# ── a refusal is not re-offered every pass; a profile edit releases it ───────

def test_the_backstop_does_not_re_offer_a_refused_job(clean, monkeypatch):
    """Cost review: every 5-minute pass offered the same refused job to the slate
    again (three point reads each) until the freshness window closed."""
    from app.matching import pipeline
    jid = _seed("refused", stamped=INELIGIBLE, relocation_targets="Chicago, IL")
    offered = []
    import app.strategy.slate as slate
    monkeypatch.setattr(slate, "place", lambda s, job, score, **kw: offered.append(job.id)
                        or slate.Placement(False, "ineligible"))
    pipeline._reshortlist_scored_jobs(UID, 0)
    assert jid not in offered


def test_a_location_profile_edit_releases_refused_jobs(clean):
    from app.api import server
    jid = _seed("release", stamped=INELIGIBLE, relocation_targets="Chicago, IL")
    assert server._release_location_stamps(UID) >= 1
    with get_session() as s:
        assert s.get(Job, jid).eligibility is None
    # …and the slate then decides it afresh against the NEW preferences.
    with get_session() as s:
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == UID)).first()
        prof.relocation_targets = "Texas"
        s.add(prof)
        s.commit()
    assert _place(jid).created


def test_a_delivered_job_keeps_its_verdict_on_a_profile_edit(clean):
    from app.api import server
    jid = _seed("kept", stamped=INELIGIBLE)
    with get_session() as s:
        s.add(Application(job_id=jid, user_id=UID, status="shortlisted"))
        s.commit()
    server._release_location_stamps(UID)
    with get_session() as s:
        assert s.get(Job, jid).eligibility == INELIGIBLE


# ── one role, one recommendation; the employer's own posting preferred ──────

def _plain_job(ext, *, source=JobSource.GREENHOUSE, title="Backend Engineer", company="Dupco"):
    with get_session() as s:
        j = Job(user_id=UID, source=source, external_id=_P + ext, company=company,
                title=title, url=f"https://x/{ext}", description="d", location="Remote",
                rerank_score=85, first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        s.exec(delete(FunnelEvent).where(FunnelEvent.job_id == j.id))
        s.exec(delete(Application).where(Application.job_id == j.id))
        s.commit()
        return j.id


def test_the_same_role_is_not_delivered_twice(clean):
    a = _plain_job("dupA")
    b = _plain_job("dupB", title="Backend  Engineer")        # same role, other door
    assert _place(a).created
    p = _place(b)
    assert not p.created and p.outcome == "duplicate"


def test_the_employers_posting_replaces_an_unopened_aggregator_copy(clean):
    agg = _plain_job("aggA", source=JobSource.SERPAPI)
    own = _plain_job("ownA", source=JobSource.GREENHOUSE)
    assert _place(agg).created
    assert _place(own).created
    with get_session() as s:
        agg_app = s.exec(select(Application).where(Application.job_id == agg)).first()
        assert agg_app.status.value == "skipped"
        assert "employer's own posting" in agg_app.notes


def test_an_opened_aggregator_copy_is_kept(clean):
    agg = _plain_job("aggB", source=JobSource.SERPAPI)
    own = _plain_job("ownB", source=JobSource.GREENHOUSE)
    assert _place(agg).created
    with get_session() as s:
        row = s.exec(select(Application).where(Application.job_id == agg)).first()
        row.viewed_at = datetime.utcnow()
        s.add(row)
        s.commit()
    assert _place(own).outcome == "duplicate"
