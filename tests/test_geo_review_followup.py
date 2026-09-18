"""The second geography review (of 34a6820): the counterexamples that still
reproduced, each pinned through the path it failed on — not `decide()` alone.

  1. Evidence is COMBINED at every verification step. An official detail
     endpoint restating a structured country cannot overturn a restriction the
     posting's own text states; a conflict intake recorded is retained, and the
     model is never asked to break a structured-vs-text tie.
  2. A model's quote must SUPPORT each claim. A verbatim sentence about a
     comfortable office establishes no country; a quote whose first 40
     characters are real and whose tail is invented is not verbatim at all.
  3. The legacy string filters defer to the verdict. "Remote · Tiranë,
     Albania · Austin, TX" is ELIGIBLE for a US user by the verdict and Albanian
     to the whole-string regex; retrieval, the rule filter and the reranker's
     pre-filter all keep it. Rows with no verdict keep the string gate.
  4. A STRUCTURED-ONLY change (Lever `country` US → GB under the same "Remote"
     and the same text) reaches the shared door, re-derives the geography and
     re-decides every copy — without rewriting the text or clearing the
     embedding. Rows from before the hash existed adopt a baseline on touch.
  5. "Include remote roles" off makes a remote role INELIGIBLE, and
     "Candidates must be based in California" is a STATE restriction: a
     Cincinnati user is out, a San Jose user is in, and a user whose profile
     names no state is HELD rather than read as anywhere-in-the-US.
  6. The daily model-call cap is PERSISTED and reserved atomically: it does
     not reset with the process, and a counter that cannot be written means
     no call at all.

Provider calls and fetches are mocked; nothing here leaves the box. Rows carry
the `geo2-` prefix and are deleted by it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.common import daily_counter as dc
from app.common.eligibility import (
    CONFLICT, ELIGIBLE, INELIGIBLE, UNKNOWN, GeoPrefs, Geography, decide,
)
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    Application, FunnelEvent, Job, JobGeography, JobHiringContext, JobSource,
    PlatformCounter, UserProfile,
)
from app.discovery import geo_verify as gv
from app.discovery import pipeline as P
from app.discovery.base import GeoEvidence, RawJob

_P = "geo2-"
US_HOME = GeoPrefs(country="united states", remote_ok=True, open_to_relocation=False,
                   home_location="Cincinnati, OH")
US_MOVER = GeoPrefs(country="united states", remote_ok=True, open_to_relocation=True,
                    home_location="Cincinnati, OH")
US_ONSITE = GeoPrefs(country="united states", remote_ok=False, open_to_relocation=False,
                     home_location="Cincinnati, OH")
US_SJ = GeoPrefs(country="united states", remote_ok=True, home_location="San Jose, CA")
US_NOWHERE = GeoPrefs(country="united states", remote_ok=True, home_location="")
U1, U2, U3 = _P + "u1", _P + "u2", _P + "u3"
LEVER_URL = "https://jobs.lever.co/acme/0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(settings, "geo_verify_enabled", True)
    monkeypatch.setattr(settings, "geo_hold_unresolved", True)
    monkeypatch.setattr(settings, "geo_verify_fetch_enabled", True)
    monkeypatch.setattr(settings, "geo_verify_llm_enabled", True)
    gv.reset_state()
    yield
    with get_session() as s:
        jids = list(s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all())
        if jids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(Job).where(Job.id.in_(jids)))
        s.exec(delete(JobGeography).where(JobGeography.external_id.like(f"{_P}%")))
        s.exec(delete(JobHiringContext).where(JobHiringContext.external_id.like(f"{_P}%")))
        s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
        s.exec(delete(PlatformCounter).where(PlatformCounter.name.like(f"{_P}%")))
        s.commit()
    gv.reset_state()


def _raw(ext, *, location="", remote=False, desc="Build the platform with Python. " * 5,
         geo=None, source="greenhouse", url=None, title="Machine Learning Engineer",
         company=None):
    return RawJob(source=source, external_id=_P + ext, company=company or f"Acme {ext}",
                  title=title, location=location, remote=remote,
                  url=url or f"https://boards.greenhouse.io/acme/jobs/{ext}",
                  description=desc, posted_at=datetime.utcnow(), geo=geo)


def _profile(uid, **kw):
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="Machine Learning Engineer",
                          preferred_country=kw.pop("preferred_country", "United States"), **kw))
        s.commit()


def _copy(uid, ext):
    with get_session() as s:
        return s.exec(select(Job).where(Job.user_id == uid,
                                        Job.external_id == _P + ext)).first()


def _shared(ext):
    with get_session() as s:
        return s.exec(select(Job).where(Job.user_id == P.SHARED_POOL_USER,
                                        Job.external_id == _P + ext)).first()


def _geo_row(ext, source="greenhouse"):
    with get_session() as s:
        return s.exec(select(JobGeography).where(JobGeography.source == source,
                                                 JobGeography.external_id == _P + ext)).first()


def _shared_then_user(raw, uid, prefs=US_HOME):
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    return P._upsert([raw], user_id=uid, preferred_country="United States",
                     user_keywords=["machine learning engineer"], geo_prefs=prefs)


def _lever_raw(ext, country, desc, location="Remote"):
    return _raw(ext, source="lever", url=LEVER_URL, location=location, remote=True, desc=desc,
                geo=GeoEvidence(sites=[location], sites_field="categories.allLocations",
                                country=country, country_field="country",
                                work_mode="remote", work_mode_field="workplaceType"))


def _lever_detail(country, location="Remote"):
    return json.dumps({"categories": {"location": location, "allLocations": [location]},
                       "country": country, "workplaceType": "remote"})


def _us_profile():
    """The RuleFilter/Reranker profile of a US user (no sponsorship, no band)."""
    return SimpleNamespace(years_experience=0, salary_min=0, salary_max=0, salary_currency="USD",
                           requires_sponsorship=False, preferred_country="United States",
                           key_skills="", degree="", job_type_preference="full_time",
                           include_internships_in_discovery=False)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Evidence is combined at every step; a conflict is retained
# ═════════════════════════════════════════════════════════════════════════════

def test_the_detail_endpoint_cannot_overturn_a_restriction_the_text_states(monkeypatch):
    """Lever says country=US; the posting says 'Candidates must be based in
    Germany'. Intake records a CONFLICT. Verification then fetched the official
    detail endpoint, derived a probe with an EMPTY description, and replaced
    the conflict with a resolved US — the Germany sentence still in the very
    text it had just ignored. The probe now carries the description, the
    conflict is retained, and the model is not asked to pick a side."""
    _profile(U1, location="Cincinnati, OH")
    desc = "Fully remote role. Candidates must be based in Germany. Python and SQL."
    raw = _lever_raw("conflict", "US", desc)
    assert gv.derive(raw).status == CONFLICT
    _shared_then_user(raw, U1)
    copy = _copy(U1, "conflict")
    assert copy.eligibility == UNKNOWN and "conflict" in copy.eligibility_reason.lower()
    row = _geo_row("conflict", "lever")
    assert row.status == CONFLICT and row.next_attempt_at <= datetime.utcnow(), "due for verification"

    fetched, llm = [], []

    def _fake_fetch(url, timeout):
        fetched.append(url)
        assert "api.lever.co" in url
        return (200, _lever_detail("US"), None)

    def _no_llm(*a, **k):
        llm.append(1)
        raise AssertionError("the rules read both sides; excerpts alone cannot adjudicate")

    monkeypatch.setattr(gv, "_fetch", _fake_fetch)
    monkeypatch.setattr(gv, "_cheapest_backend", lambda: ("openai", "gpt-4o-mini", object()))
    monkeypatch.setattr(gv, "_call_llm", _no_llm)

    # The page step, on its own, now derives the SAME conflict.
    ev = gv.page_evidence(LEVER_URL, "lever", description=desc)
    assert ev.how == "ats_detail" and ev.geo is not None and ev.geo.status == CONFLICT
    assert "germany" in " ".join(ev.geo.conflicts)
    fetched.clear()                      # that probe was ours; the sweep's is what is counted

    out = gv.verify_pending()
    assert out["resolved"] == 0 and out["still_unknown"] == 1
    assert len(fetched) == 1 and not llm
    row = _geo_row("conflict", "lever")
    assert row.status == CONFLICT, "the endpoint restating US does not resolve the text's Germany"
    assert json.loads(row.countries_json) == ["united states", "germany"]
    assert row.next_attempt_at > datetime.utcnow() + timedelta(days=300), "parked for a person, not re-fetched every cycle"
    assert _copy(U1, "conflict").eligibility == UNKNOWN, "held: nobody invented a country"
    assert _copy(U1, "conflict").rerank_score is None
    assert gv.verify_pending()["pending_examined"] == 0
    # ...whereas an endpoint whose structured evidence AGREES with the text
    # resolves it: same posting, Lever now says country=DE.
    with get_session() as s:
        r = s.get(JobGeography, row.id)
        r.next_attempt_at = datetime.utcnow() - timedelta(minutes=1)
        s.add(r)
        s.commit()
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: (200, _lever_detail("DE"), None))
    out = gv.verify_pending()
    assert out["resolved"] == 1
    row = _geo_row("conflict", "lever")
    assert row.status == "resolved" and json.loads(row.countries_json) == ["germany"]
    assert _copy(U1, "conflict").eligibility == INELIGIBLE


def test_a_conflict_intake_found_is_never_handed_to_the_model(monkeypatch):
    """No official endpoint (a Teamtailor permalink), page has no JSON-LD:
    the sequence used to fall through to the model, which read the excerpts,
    restated the text's side, and 'resolved' the conflict. It is retained."""
    _profile(U1)
    desc = "Hybrid schedule from our office. Only open to candidates located in Germany. Python."
    raw = _raw("tt-conflict", source="teamtailor", location="Austin, TX",
               url=f"https://acme.teamtailor.com/jobs/{_P}tt-conflict", desc=desc,
               geo=GeoEvidence(sites=["Austin, TX"], sites_field="location"))
    assert gv.derive(raw).status == CONFLICT
    _shared_then_user(raw, U1)
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: (200, "<html><body>Engineer</body></html>", None))
    monkeypatch.setattr(gv, "_cheapest_backend", lambda: ("openai", "gpt-4o-mini", object()))
    monkeypatch.setattr(gv, "_call_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call on a conflict")))
    out = gv.verify_pending()
    assert out["still_unknown"] == 1
    row = _geo_row("tt-conflict", "teamtailor")
    assert row.status == CONFLICT and row.last_error == "conflict_retained"
    assert out["geo_verify"]["conflict_retained"] == 1
    assert _copy(U1, "tt-conflict").eligibility == UNKNOWN


# ═════════════════════════════════════════════════════════════════════════════
# 2. The quote must support the claim
# ═════════════════════════════════════════════════════════════════════════════

def test_a_quote_that_names_no_place_supports_no_claim(monkeypatch):
    _profile(U1)
    desc = ("Our office provides a comfortable and collaborative place to work. "
            "We value curiosity. Python and SQL required.")
    raw = _raw("office", source="teamtailor", location="",
               url=f"https://acme.teamtailor.com/jobs/{_P}office", desc=desc)
    _shared_then_user(raw, U1)
    assert _copy(U1, "office").eligibility == UNKNOWN
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: (403, "", None))
    monkeypatch.setattr(gv, "_cheapest_backend", lambda: ("openai", "gpt-4o-mini", object()))
    monkeypatch.setattr(gv, "_call_llm", lambda p, m, c, ex: (json.dumps({
        "country": "United States", "locations": [], "work_mode": "onsite",
        "permitted_regions": ["United States"],
        # VERBATIM — it really is in the excerpts — and about nothing geographic.
        "evidence_quote": "Our office provides a comfortable and collaborative place to work.",
        "conflicts": []}), {"input": 300, "output": 40}, m))
    out = gv.verify_pending()
    assert out["resolved"] == 0 and out["still_unknown"] == 1
    row = _geo_row("office", "teamtailor")
    assert row.status == "unknown" and row.last_error == "unsupported_quote"
    assert json.loads(row.countries_json) == []
    assert _copy(U1, "office").eligibility == UNKNOWN, "no country was adopted from a quote about an office"
    assert out["geo_verify"]["llm_unsupported_quote"] == 1
    assert row.attempts == 1, "the call happened and was paid for; it counts"


def test_a_forty_character_prefix_is_not_a_verbatim_quote():
    text = "Candidates must reside in the United States and be able to travel occasionally."
    real_head = "Candidates must reside in the United States"          # 43 chars, verbatim
    assert gv.quote_is_verbatim(real_head, text)
    assert gv.quote_is_verbatim(real_head + "...", text), "a trimmed quote is still a substring"
    assert not gv.quote_is_verbatim(real_head + " and must hold Canadian citizenship", text), (
        "the first 40 characters are real; the tail is invented")
    geo, how = gv.interpret_llm_answer(json.dumps({
        "country": "Canada", "permitted_regions": ["Canada"],
        "evidence_quote": real_head + " and must hold Canadian citizenship"}), text)
    assert geo is None and how == "rejected_quote"


def test_each_claim_needs_its_own_support_and_the_unsupported_one_is_dropped():
    text = "Fully remote position. Candidates must be based in Germany for payroll reasons."
    geo, how = gv.interpret_llm_answer(json.dumps({
        "country": "United States",                       # not in the quote
        "permitted_regions": ["Germany", "Austria"],      # Austria not in the quote
        "work_mode": "remote",
        "evidence_quote": "Candidates must be based in Germany for payroll reasons"}), text)
    assert how == "llm" and geo.countries == ["germany"] and geo.remote_regions == ["germany"]
    assert decide(geo, US_HOME).status == INELIGIBLE
    # A demonym is support: the rules cannot read "German borders", a reader can.
    geo, how = gv.interpret_llm_answer(json.dumps({
        "country": "Germany", "permitted_regions": ["Germany"],
        "evidence_quote": "live and work from within German borders"}),
        "This position requires you to live and work from within German borders.")
    assert how == "llm" and geo.countries == ["germany"]
    # "Worldwide" is supported only by a worldwide phrase.
    geo, how = gv.interpret_llm_answer(json.dumps({
        "country": None, "permitted_regions": ["worldwide"],
        "evidence_quote": "We hire from anywhere in the world."}),
        "Remote first. We hire from anywhere in the world.")
    assert how == "llm" and geo.remote_regions == ["worldwide"]
    geo, how = gv.interpret_llm_answer(json.dumps({
        "country": None, "permitted_regions": ["worldwide"],
        "evidence_quote": "Remote first."}), "Remote first. Great benefits.")
    assert geo is None and how == "unsupported_quote"


# ═════════════════════════════════════════════════════════════════════════════
# 3. The legacy filters defer to the verdict
# ═════════════════════════════════════════════════════════════════════════════

def test_a_multi_site_posting_eligible_by_verdict_survives_every_legacy_filter():
    from app.matching import matcher as mm
    from app.matching.filters.rule_filter import RuleFilter
    from app.matching.reranker import Reranker
    _profile(U1, location="Cincinnati, OH")
    loc = "Remote · Tiranë, Albania · Austin, TX"
    raw = _raw("albania", source="ashby", location=loc, remote=True,
               url="https://jobs.ashbyhq.com/acme/1",
               geo=GeoEvidence(sites=["Remote", "Tiranë, Albania", "Austin, TX"],
                               sites_field="secondaryLocations", work_mode="remote"))
    assert _shared_then_user(raw, U1) == 1
    job = _copy(U1, "albania")
    assert job.eligibility == ELIGIBLE, "a US site exists; the verdict admits it"

    # The rule filter — Phase 1 of the matching pipeline, and the reranker's
    # pre-filter in the scoring and pulse lanes — used to reject it as Albania.
    res = RuleFilter(profile=_us_profile()).filter(job)
    assert res.passed, res.reason
    assert Reranker(profile=_us_profile())._pre_filter_job(job) is None
    # Retrieval's own copy of the string gate, on the projected row.
    with get_session() as s:
        cand = mm._Candidate(*s.exec(select(*mm._candidate_columns()).where(Job.id == job.id)).one())
    assert cand.eligibility == ELIGIBLE
    assert mm._passes_legacy_country_gate(cand, "united states")
    # The same string with NO verdict (a legacy row) is still Albania to the
    # string gate: the old behaviour is kept for the rows it was written for.
    legacy = mm._Candidate(id=0, title="ML Engineer", company="Acme", location=loc,
                           remote=False, eligibility=None, description="")
    assert not mm._passes_legacy_country_gate(legacy, "united states")
    job.eligibility = None
    job.remote = False
    assert "Location pre-filtered" in RuleFilter(profile=_us_profile()).filter(job).reason
    # ...and an INELIGIBLE verdict is honoured as a backstop, whatever the string says.
    job.eligibility, job.eligibility_reason, job.location = INELIGIBLE, "Located in Sweden", "Austin, TX"
    res = RuleFilter(profile=_us_profile()).filter(job)
    assert not res.passed and res.reason.startswith("Location filtered: Located in Sweden")
    assert not mm._passes_legacy_country_gate(
        mm._Candidate(id=0, title="t", company="c", location="Austin, TX", remote=False,
                      eligibility=INELIGIBLE, description=""), "united states")


def test_the_verdict_never_admits_what_it_did_not_decide():
    """Seniority, sponsorship and job type stay independent checks."""
    from app.matching.filters.rule_filter import RuleFilter
    job = Job(source=JobSource.GREENHOUSE, external_id=_P + "staff", company="Acme",
              title="Staff ML Engineer", location="Austin, TX", url="https://x/s",
              description="Lead the team.", eligibility=ELIGIBLE, eligibility_reason="Located in United States")
    assert "Title pre-filtered" in RuleFilter(profile=_us_profile()).filter(job).reason


# ═════════════════════════════════════════════════════════════════════════════
# 4. A structured-only change reaches the shared door
# ═════════════════════════════════════════════════════════════════════════════

def test_a_structured_only_country_change_re_decides_every_copy():
    from app.strategy import pulse_lane as pl
    _profile(U1, location="Cincinnati, OH")
    desc = "Remote-first team. Python and SQL. " * 3
    us = _lever_raw("moved", "US", desc)
    _shared_then_user(us, U1)
    assert _copy(U1, "moved").eligibility == ELIGIBLE
    assert json.loads(_geo_row("moved", "lever").countries_json) == ["united states"]
    with get_session() as s:
        shared = _shared("moved")
        assert shared.geo_hash == gv.evidence_hash(us)
        row = s.get(Job, shared.id)
        row.embedding_id = 4242
        s.add(row)
        s.commit()

    # Same text, same display string, same remote flag — only Lever's
    # structured `country` moved. Nothing the old comparison looked at changed.
    gb = _lever_raw("moved", "GB", desc)
    assert gb.location == us.location and gb.description == us.description
    assert pl._board_signature([gb]) != pl._board_signature([us]), "the board hash sees it too"
    assert pl._board_signature([us]) == pl._board_signature([_lever_raw("moved", "US", desc)])
    P._upsert([gb], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    row = _geo_row("moved", "lever")
    assert json.loads(row.countries_json) == ["united kingdom"]
    assert row.verifier_version == gv.VERIFIER_VERSION
    copy = _copy(U1, "moved")
    assert copy.eligibility == INELIGIBLE and copy.rerank_score == gv.INELIGIBLE_STAMP_SCORE
    assert "United Kingdom" in copy.eligibility_reason
    shared = _shared("moved")
    assert shared.geo_hash == gv.evidence_hash(gb) and shared.geo_hash != gv.evidence_hash(us)
    assert shared.embedding_id == 4242 and shared.description == desc, (
        "a geography-only change rewrites two small columns, never the text or the embedding")


def test_a_row_from_before_the_hash_adopts_a_baseline_without_re_deriving():
    """Shared rows written before Job.geo_hash existed are NULL. The first
    stale touch records the current evidence as the baseline — it is NOT a
    change, no geography row is created for a pre-rollout posting, and
    nothing is re-decided. The NEXT move is what becomes visible."""
    _profile(U1)
    desc = "Remote-first team. Python. " * 3
    raw = _lever_raw("legacy", "US", desc)
    now = datetime.utcnow()
    with get_session() as s:
        s.add(Job(user_id=P.SHARED_POOL_USER, source=JobSource.LEVER, external_id=_P + "legacy",
                  company=raw.company, title=raw.title, location="Remote", remote=True,
                  url=LEVER_URL, description=desc,
                  content_hash=hashlib.sha256(desc.encode()).hexdigest(),
                  posted_at=now, first_seen=now - timedelta(days=2),
                  discovered_at=now - timedelta(days=2), last_seen=now - timedelta(hours=7),
                  geo_hash=None))
        s.commit()
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    shared = _shared("legacy")
    assert shared.geo_hash == gv.evidence_hash(raw), "baseline adopted on the stale touch"
    assert shared.last_seen > now - timedelta(minutes=1)
    assert _geo_row("legacy", "lever") is None, "copying/touching an old posting never makes it new"
    # From here a real move IS seen.
    P._upsert([_lever_raw("legacy", "GB", desc)], user_id=P.SHARED_POOL_USER,
              user_keywords=["machine learning engineer"])
    assert _shared("legacy").geo_hash == gv.evidence_hash(_lever_raw("legacy", "GB", desc))


# ═════════════════════════════════════════════════════════════════════════════
# 5. Remote preference and sub-national restrictions
# ═════════════════════════════════════════════════════════════════════════════

def test_include_remote_roles_off_makes_a_remote_role_ineligible():
    remote_us = Geography(status="resolved", countries=["united states"], sites=["Remote"],
                          work_mode="remote", remote_regions=["united states"])
    assert decide(remote_us, US_HOME).status == ELIGIBLE
    d = decide(remote_us, US_ONSITE)
    assert d.status == INELIGIBLE and d.code == "remote_not_wanted"
    # An office in the user's own area is the office, not the remote track.
    with_office = Geography(status="resolved", countries=["united states"],
                            sites=["Remote", "Cincinnati, OH"], work_mode="remote")
    assert decide(with_office, US_ONSITE).code == "office_in_home_area"
    elsewhere = Geography(status="resolved", countries=["united states"],
                          sites=["Remote", "Austin, TX"], work_mode="remote")
    assert decide(elsewhere, US_ONSITE).code == "remote_not_wanted"
    # Hybrid and on-site are what the preference asks for.
    hybrid = Geography(status="resolved", countries=["united states"],
                       sites=["Cincinnati, OH"], work_mode="hybrid")
    assert decide(hybrid, US_ONSITE).status == ELIGIBLE
    # ...and at the door: the posting never enters the pool.
    _profile(U1, remote_ok=False, location="Cincinnati, OH")
    raw = _raw("rok-off", source="remotive", location="Remote (United States)", remote=True,
               url="https://remotive.com/remote-jobs/1",
               geo=GeoEvidence(sites=["United States"], work_mode="remote",
                               remote_regions="United States", remote_regions_field="location"))
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    assert P._upsert([raw], user_id=U1, preferred_country="United States",
                     user_keywords=["machine learning engineer"], geo_prefs=US_ONSITE) == 0
    assert _copy(U1, "rok-off") is None


def test_a_state_restriction_is_narrower_than_the_country():
    _profile(U1, location="Cincinnati, OH")
    _profile(U2, location="San Jose, CA")
    _profile(U3, location="")
    desc = "Fully remote role. Candidates must be based in California. Python and SQL."
    raw = _raw("cal", location="Remote", remote=True, desc=desc,
               geo=GeoEvidence(sites=["Remote"], work_mode="remote"))
    g = gv.derive(raw)
    assert g.status == "resolved" and g.countries == ["united states"]
    assert g.areas == ["united states/ca"], "the state is kept, not widened to the country"
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    for uid, prefs in ((U1, US_HOME), (U2, US_SJ), (U3, US_NOWHERE)):
        P._upsert([raw], user_id=uid, preferred_country="United States",
                  user_keywords=["machine learning engineer"], geo_prefs=prefs)
    assert _copy(U1, "cal") is None, "Cincinnati is not California: dropped at the door"
    assert decide(g, US_HOME).code == "area_restriction_excluded"
    sj = _copy(U2, "cal")
    assert sj.eligibility == ELIGIBLE and "California" in sj.eligibility_reason
    nowhere = _copy(U3, "cal")
    assert nowhere.eligibility == UNKNOWN and nowhere.rerank_score is None, (
        "no home state on file: held, never inferred nationwide")
    assert "California" in nowhere.eligibility_reason
    assert json.loads(_geo_row("cal").areas_json) == ["united states/ca"]
    # A German user is outside it for the plain country reason.
    assert decide(g, GeoPrefs(country="germany")).code == "remote_region_excluded"
    # A user who WILL relocate is not held to a residence rule for an office
    # they would move to — but a remote role's residence rule still binds.
    assert decide(g, US_MOVER).code == "area_restriction_excluded"
    office = Geography(status="resolved", countries=["united states"], sites=["Los Angeles, CA"],
                       work_mode="onsite", remote_regions=["united states"], areas=["united states/ca"])
    assert decide(office, US_MOVER).status == ELIGIBLE
    assert decide(office, US_HOME).status == INELIGIBLE


def test_a_city_restriction_binds_the_city():
    g = gv.derive(_raw("atx", location="Remote", remote=True,
                       desc="Remote within Texas only: candidates must be located in Austin, TX."))
    assert g.areas == ["united states/tx/austin"]
    assert decide(g, GeoPrefs(country="united states", home_location="Austin, TX")).status == ELIGIBLE
    assert decide(g, GeoPrefs(country="united states", home_location="Dallas, TX")).code == "area_restriction_excluded"
    assert decide(g, GeoPrefs(country="united states", home_location="TX")).code == "area_restriction_unresolved"
    # Spelled-out states on either side.
    g2 = gv.derive(_raw("ny", location="Remote", remote=True,
                        desc="Applicants must reside in New York State."))
    assert g2.areas == ["united states/ny"]
    assert decide(g2, GeoPrefs(country="united states", home_location="Brooklyn, New York")).status == ELIGIBLE
    assert decide(g2, GeoPrefs(country="united states", home_location="Newark, New Jersey")).status == INELIGIBLE


def test_the_model_can_establish_a_state_restriction_the_rules_could_not_read(monkeypatch):
    _profile(U1, location="Cincinnati, OH")
    _profile(U2, location="San Jose, CA")
    desc = "Fully remote position; we can only consider folks who call California home. Python."
    assert gv.derive(_raw("probe", desc=desc)).status == "unknown", "the rules must not already read this"
    raw = _raw("cal-llm", source="teamtailor", location="", desc=desc,
               url=f"https://acme.teamtailor.com/jobs/{_P}cal-llm")
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    for uid, prefs in ((U1, US_HOME), (U2, US_SJ)):
        P._upsert([raw], user_id=uid, preferred_country="United States",
                  user_keywords=["machine learning engineer"], geo_prefs=prefs)
    assert _copy(U1, "cal-llm").eligibility == UNKNOWN
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: (None, "", "ConnectTimeout"))
    monkeypatch.setattr(gv, "_cheapest_backend", lambda: ("openai", "gpt-4o-mini", object()))
    monkeypatch.setattr(gv, "_call_llm", lambda p, m, c, ex: (json.dumps({
        "country": "United States", "locations": [], "work_mode": "remote",
        "permitted_regions": ["California"],
        "evidence_quote": "we can only consider folks who call California home",
        "conflicts": []}), {"input": 400, "output": 50}, m))
    out = gv.verify_pending()
    assert out["resolved"] == 1
    row = _geo_row("cal-llm", "teamtailor")
    assert json.loads(row.areas_json) == ["united states/ca"]
    assert _copy(U1, "cal-llm").eligibility == INELIGIBLE
    assert _copy(U2, "cal-llm").eligibility == ELIGIBLE


# ═════════════════════════════════════════════════════════════════════════════
# 6. The daily cap is a platform-wide number
# ═════════════════════════════════════════════════════════════════════════════

def test_the_daily_cap_is_persisted_and_reserved_atomically(monkeypatch):
    name = _P + "cap"
    dc.reset(name)
    assert dc.count(name) is None
    assert dc.reserve(name, 2) and dc.reserve(name, 2)
    assert not dc.reserve(name, 2), "the third unit is refused"
    assert dc.count(name) == 2
    # A restart (or a second replica) shares the same number: nothing in
    # process memory to lose. The module holds no state to reset.
    import importlib
    importlib.reload(dc)
    assert not dc.reserve(name, 2) and dc.count(name) == 2
    assert dc.reserve(name, 0), "cap 0 = unlimited, but still counted"
    assert dc.count(name) == 3
    # The verifier's own counter goes through the same table.
    monkeypatch.setattr(settings, "geo_verify_llm_daily_cap", 1)
    assert gv._register_llm_call() is True
    assert gv._register_llm_call() is False
    assert gv._llm_calls_today() == 1
    with get_session() as s:
        row = s.exec(select(PlatformCounter).where(PlatformCounter.name == gv.LLM_CAP_COUNTER)).first()
    assert row is not None and row.count == 1 and row.day == datetime.utcnow().date()


def test_a_reservation_that_cannot_be_recorded_means_no_call(monkeypatch):
    """Money that cannot be accounted for is not spent — and the posting is
    deferred as a platform skip, not charged an attempt."""
    _profile(U1)
    raw = _raw("nores", source="teamtailor", location="",
               url=f"https://acme.teamtailor.com/jobs/{_P}nores",
               desc="Our office is in the city centre with a hybrid schedule. Python.")
    _shared_then_user(raw, U1)
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: (403, "", None))
    monkeypatch.setattr(gv, "_cheapest_backend", lambda: ("openai", "gpt-4o-mini", object()))
    monkeypatch.setattr(gv, "_call_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call without a reservation")))
    monkeypatch.setattr(dc, "reserve", lambda *a, **k: False)
    out = gv.verify_pending()
    assert out["still_unknown"] == 1
    row = _geo_row("nores", "teamtailor")
    assert row.last_error == "skipped:daily_cap" and row.attempts == 0
    assert row.next_attempt_at > datetime.utcnow() + timedelta(minutes=30)
