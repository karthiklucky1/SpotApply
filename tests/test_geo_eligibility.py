"""Location eligibility for NEW postings: one shared geography, one decision.

What these pin, in the order the brief listed them:

  * a blank stored location resolved from the posting's own page (Sweden,
    on-site) rejects a US user — and the verification happens ONCE for every
    user copy;
  * a Teamtailor title suffix that is a DEPARTMENT is never a city; the page
    says Paris and Paris is what is stored;
  * "Homeoffice" alone is UNKNOWN; "Germany residents only" rejects a US user;
  * a US site hidden past Ashby's display cap still counts;
  * an on-site San Francisco role is INELIGIBLE for a Cincinnati user who will
    not relocate, and ELIGIBLE for one who will;
  * explicit US-only vs Europe-only remote restrictions;
  * an ATS site that contradicts the description's restriction stays a
    CONFLICT — nobody invents a country to break the tie;
  * missing evidence stays UNKNOWN: the model is not asked when there is no
    text, and an answer whose quote is not in the text is discarded;
  * a provider outage or an exhausted budget defers, and never re-tries every
    tick; changed location evidence re-decides every copy;
  * copying a posting the pool held before this shipped does NOT make it new.

Provider calls are mocked (`_call_llm`, `_cheapest_backend`) and the fetch is
mocked (`_fetch`) — nothing here leaves the box. Rows carry the `geo-`
prefix and are deleted by it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.common.eligibility import (
    CONFLICT, ELIGIBLE, INELIGIBLE, UNKNOWN, GeoPrefs, decide,
)
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    Application, FunnelEvent, Job, JobGeography, JobHiringContext, JobSource, UserProfile,
)
from app.discovery import geo_verify as gv
from app.discovery import pipeline as P
from app.discovery.base import GeoEvidence, RawJob

_P = "geo-"
US_HOME = GeoPrefs(country="united states", remote_ok=True, open_to_relocation=False,
                   home_location="Cincinnati, OH")
US_MOVER = GeoPrefs(country="united states", remote_ok=True, open_to_relocation=True,
                    home_location="Cincinnati, OH")
DE = GeoPrefs(country="germany")
U1, U2 = _P + "u1", _P + "u2"


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
        s.commit()
    gv.reset_state()


def _raw(ext, *, location="", remote=False, desc="Build the platform with Python. " * 5,
         geo=None, source="greenhouse", url=None, title="Machine Learning Engineer",
         company="Acme"):
    return RawJob(source=source, external_id=_P + ext, company=company, title=title,
                  location=location, remote=remote,
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


def _geo_row(ext, source="greenhouse"):
    with get_session() as s:
        return s.exec(select(JobGeography).where(JobGeography.source == source,
                                                 JobGeography.external_id == _P + ext)).first()


def _shared_then_user(raw, uid, prefs=US_HOME):
    """The pulse lane's order: shared upsert, then the per-user route on the
    same RawJob objects."""
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    return P._upsert([raw], user_id=uid, preferred_country="United States",
                     user_keywords=["machine learning engineer"], geo_prefs=prefs)


def _page(html_body):
    return (200, html_body, None)


def _jsonld(**posting):
    item = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Engineer", **posting}
    return f"<html><head><script type=\"application/ld+json\">{json.dumps(item)}</script></head><body>Engineer</body></html>"


# ═════════════════════════════════════════════════════════════════════════════
# The decision itself (pure)
# ═════════════════════════════════════════════════════════════════════════════

def test_homeoffice_alone_is_unknown_and_germany_residents_only_rejects_a_us_user():
    home = gv.derive(_raw("h", location="Homeoffice", remote=True))
    assert home.status == "unknown" and not home.countries
    assert decide(home, US_HOME).status == UNKNOWN

    de = gv.derive(_raw("d", location="Remote", remote=True,
                        desc="Fully remote role. Germany residents only. Python required."))
    assert de.countries == ["germany"] and "germany" in de.remote_regions
    d = decide(de, US_HOME)
    assert d.status == INELIGIBLE and d.code == "remote_region_excluded"
    assert "Germany" in d.reason
    assert decide(de, DE).status == ELIGIBLE
    assert "Germany residents only" in de.evidence_quote


def test_explicit_us_only_remote_versus_europe_only_remote():
    us_only = gv.derive(_raw("us", location="Remote", remote=True,
                             geo=GeoEvidence(work_mode="remote", remote_regions="USA Only",
                                             remote_regions_field="candidate_required_location")))
    eu_only = gv.derive(_raw("eu", location="Remote (Europe)", remote=True,
                             geo=GeoEvidence(sites=["Remote (Europe)"], work_mode="remote",
                                             remote_regions="Europe", remote_regions_field="region")))
    assert decide(us_only, US_HOME).status == ELIGIBLE
    assert decide(us_only, DE).code == "remote_region_excluded"
    assert decide(eu_only, US_HOME).code == "remote_region_excluded"
    assert decide(eu_only, DE).status == ELIGIBLE


def test_san_francisco_onsite_applies_the_relocation_preference():
    sf = gv.derive(_raw("sf", location="San Francisco, CA",
                        geo=GeoEvidence(sites=["San Francisco, CA"], work_mode="onsite",
                                        work_mode_field="workplaceType")))
    stay = decide(sf, US_HOME)
    assert stay.status == INELIGIBLE and stay.code == "onsite_outside_home_area"
    assert "San Francisco, CA" in stay.reason and "relocation" in stay.reason
    assert decide(sf, US_MOVER).status == ELIGIBLE
    # Same city as home → fine even without relocation.
    cin = gv.derive(_raw("cin", geo=GeoEvidence(sites=["Cincinnati, OH"], work_mode="onsite")))
    assert decide(cin, US_HOME).code == "onsite_in_home_area"
    # Work mode NOT stated: the country check decides, the scorer sees the city.
    unk = gv.derive(_raw("unk", location="San Francisco, CA"))
    assert decide(unk, US_HOME).status == ELIGIBLE


def test_conflicting_ats_and_description_restrictions_stay_unresolved():
    g = gv.derive(_raw("c", location="Austin, TX",
                       desc="Great team. Candidates must be located in Germany. Python."))
    assert g.status == CONFLICT and g.conflicts
    d = decide(g, US_HOME)
    assert d.status == UNKNOWN and d.code == "conflicting_location_evidence"
    # ...and a compatible restriction is not a conflict.
    ok = gv.derive(_raw("ok", location="Austin, TX",
                        desc="Candidates must be located in the United States."))
    assert ok.status == "resolved" and decide(ok, US_HOME).status == ELIGIBLE


def test_missing_evidence_never_invents_a_country():
    g = gv.derive(_raw("m", location="", desc="We are a fun team. Python and SQL. Great pay in USD."))
    assert g.status == "unknown" and not g.countries and not g.remote_regions
    assert decide(g, US_HOME).status == UNKNOWN
    # A blank country preference means no gate at all — never an assumed country.
    assert decide(g, GeoPrefs(country="")).status == ELIGIBLE


def test_a_remote_role_anchored_abroad_is_ineligible_even_when_remote():
    g = gv.derive(_raw("pl", location="Remote - Poland", remote=True))
    d = decide(g, US_HOME)
    assert d.status == INELIGIBLE and d.code == "country_mismatch"


# ═════════════════════════════════════════════════════════════════════════════
# Adapters keep the source's evidence
# ═════════════════════════════════════════════════════════════════════════════

class _Resp:
    def __init__(self, payload=None, content=b""):
        self._payload, self.content, self.status_code = payload, content, 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_teamtailor_does_not_guess_location_from_the_title(monkeypatch):
    import httpx
    from app.discovery.teamtailor import TeamtailorScraper
    rss = f"""<?xml version="1.0"?><rss><channel>
      <item><title>Backend Engineer - POS Integrations</title>
        <link>https://acme.teamtailor.com/jobs/{_P}pos-backend-engineer</link>
        <description>&lt;p&gt;Build integrations.&lt;/p&gt;</description>
        <pubDate>Mon, 15 Sep 2026 08:00:00 GMT</pubDate></item>
      <item><title>Data Engineer</title>
        <link>https://acme.teamtailor.com/jobs/{_P}data-engineer</link>
        <location>Stockholm</location>
        <description>&lt;p&gt;Pipelines.&lt;/p&gt;</description></item>
    </channel></rss>"""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(content=rss.encode()))
    jobs = {j.external_id: j for j in TeamtailorScraper("acme").fetch()}
    pos = jobs[f"{_P}pos-backend-engineer"]
    assert pos.location == "", "a department suffix must never become a city"
    assert pos.title == "Backend Engineer - POS Integrations"
    assert pos.geo is None
    assert gv.derive(pos).status == "unknown"
    # An explicit feed element IS evidence.
    de = jobs[f"{_P}data-engineer"]
    assert de.location == "Stockholm" and de.geo.sites == ["Stockholm"]
    assert gv.derive(de).countries == ["sweden"]


def test_lever_keeps_every_location_the_workplace_type_and_the_country(monkeypatch):
    import httpx
    from app.discovery.lever import LeverScraper
    payload = [{
        "id": _P + "lev1", "text": "ML Engineer", "hostedUrl": "https://jobs.lever.co/acme/x",
        "createdAt": 1757894400000, "descriptionPlain": "Build models.",
        "categories": {"location": "Berlin", "allLocations": ["Berlin", "Austin, TX"],
                       "commitment": "Full-time", "team": "AI"},
        "workplaceType": "hybrid", "country": "US",
    }]
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(payload=payload))
    job = LeverScraper("acme").fetch()[0]
    assert job.geo.sites == ["Berlin", "Austin, TX"]
    assert job.geo.work_mode == "hybrid" and job.geo.country == "US"
    assert job.location == "Berlin · Austin, TX"
    assert job.remote is False, "commitment=Full-time is not a remote signal"
    g = gv.derive(job)
    assert "united states" in g.countries and "germany" in g.countries
    assert g.work_mode == "hybrid"


def test_ashby_site_beyond_the_display_cap_still_counts(monkeypatch):
    import httpx
    from app.discovery.ashby import AshbyScraper
    secondaries = [{"location": c} for c in ("Lisbon, Portugal", "Madrid", "Berlin", "Paris",
                                              "Rome", "Warsaw")]
    secondaries.append({"location": "Austin, TX",
                        "address": {"postalAddress": {"addressLocality": "Austin",
                                                      "addressRegion": "TX",
                                                      "addressCountry": "US"}}})
    payload = {"jobs": [{
        "id": _P + "ash1", "title": "ML Engineer", "location": "Tiranë, Albania",
        "address": {"postalAddress": {"addressLocality": "Tiranë", "addressCountry": "Albania"}},
        "secondaryLocations": secondaries, "isRemote": True, "isListed": True,
        "publishedAt": "2026-09-15T00:00:00Z", "jobUrl": "https://jobs.ashbyhq.com/acme/1",
        "descriptionHtml": "<p>Models.</p>",
    }]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(payload=payload))
    job = AshbyScraper("acme").fetch()[0]
    assert job.location.endswith("+3 more"), "the DISPLAY string is still capped"
    assert "Austin" not in job.location
    assert "Austin, TX" in job.geo.sites, "the gate's evidence is not"
    assert job.geo.country == "Albania"
    g = gv.derive(job)
    assert "albania" in g.countries and "united states" in g.countries
    assert decide(g, US_MOVER).status == ELIGIBLE
    assert decide(g, US_HOME).status == ELIGIBLE, "remote-friendly, a US site exists"


def test_remoteok_and_weworkremotely_keep_their_restriction_fields():
    # Both used to store a borderless "Remote"; the restriction text is now the
    # remote-region evidence. Exercised at the derive level with the exact
    # GeoEvidence the adapters now build.
    rok = _raw("rok", location="Remote (United States)", remote=True, source="remotive",
               geo=GeoEvidence(sites=["United States"], sites_field="location", work_mode="remote",
                               remote_regions="United States", remote_regions_field="location"))
    wwr = _raw("wwr", location="Remote (Anywhere in the World)", remote=True, source="weworkremotely",
               geo=GeoEvidence(sites=["Anywhere in the World"], work_mode="remote",
                               remote_regions="Anywhere in the World", remote_regions_field="region"))
    assert decide(gv.derive(rok), US_HOME).status == ELIGIBLE
    assert decide(gv.derive(rok), DE).status == INELIGIBLE
    assert decide(gv.derive(wwr), US_HOME).status == ELIGIBLE
    assert decide(gv.derive(wwr), DE).status == ELIGIBLE


# ═════════════════════════════════════════════════════════════════════════════
# Intake: one decision at the door, stamped on the row
# ═════════════════════════════════════════════════════════════════════════════

def test_intake_drops_ineligible_holds_unknown_and_stamps_eligible():
    _profile(U1, location="Cincinnati, OH")
    raws = [_raw("in-se", location="Stockholm, Sweden"),
            _raw("in-blank", location=""),
            _raw("in-us", location="Austin, TX")]
    P._upsert(raws, user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    n = P._upsert(raws, user_id=U1, preferred_country="United States",
                  user_keywords=["machine learning engineer"], geo_prefs=US_HOME)
    assert n == 2
    assert _copy(U1, "in-se") is None, "a Swedish on-site posting never enters a US pool"
    blank, us = _copy(U1, "in-blank"), _copy(U1, "in-us")
    assert blank.eligibility == UNKNOWN and blank.rerank_score is None
    assert "pending verification" in blank.eligibility_reason
    assert us.eligibility == ELIGIBLE and "United States" in us.eligibility_reason
    # ONE geography row per posting, and the shared copy carries no verdict.
    for ext in ("in-se", "in-blank", "in-us"):
        assert _geo_row(ext) is not None
    with get_session() as s:
        shared = s.exec(select(Job.eligibility).where(Job.user_id == P.SHARED_POOL_USER,
                                                      Job.external_id == _P + "in-us")).first()
    assert shared is None


def test_a_door_that_cannot_decide_never_inherits_the_previous_users_verdict(monkeypatch):
    """The lanes hand the SAME RawJob objects to every user's door in turn.
    The verdict rides on the object, so a door whose geography lookup fails
    used to leave the previous user's verdict in place — user B's row carried
    a decision computed from user A's saved country, and that string reached
    B's explorer and B's scoring prompt."""
    _profile(U1)
    _profile(U2, preferred_country="Germany")
    raw = _raw("inherit", location="Remote", remote=True,
               desc="Fully remote role. Germany residents only.")
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    # User B (Germany) decides first: ELIGIBLE, and the verdict is on the object.
    P._upsert([raw], user_id=U2, preferred_country="Germany",
              user_keywords=["machine learning engineer"], geo_prefs=DE)
    assert _copy(U2, "inherit").eligibility == ELIGIBLE
    # User A's door cannot read the geography at all.
    raw.geography = None
    monkeypatch.setattr(P, "_geography_for_user_door", lambda cands: (_ for _ in ()).throw(RuntimeError("db down")))
    P._upsert([raw], user_id=U1, preferred_country="United States",
              user_keywords=["machine learning engineer"], geo_prefs=US_HOME)
    a = _copy(U1, "inherit")
    assert a is not None, "the string gate keeps 'Remote' — legacy behaviour"
    assert a.eligibility is None and a.eligibility_reason is None, (
        "A's row must carry NO verdict, never B's 'Remote role in Germany'")


def test_redecide_skips_a_user_whose_profile_cannot_be_read(monkeypatch):
    _profile(U1)
    raw = _raw("noprefs", location="")
    _shared_then_user(raw, U1)
    assert _copy(U1, "noprefs").eligibility == UNKNOWN
    from app.common import tenant_prefs
    monkeypatch.setattr(tenant_prefs, "geo_prefs_for_user", lambda uid: None)
    from app.common.eligibility import Geography
    sweden = Geography(status="resolved", countries=["sweden"], sites=["Stockholm"], work_mode="onsite")
    assert gv.redecide_copies("greenhouse", _P + "noprefs", sweden) == 0
    copy = _copy(U1, "noprefs")
    assert copy.eligibility == UNKNOWN and copy.rerank_score is None, (
        "no destructive stamp from a country the user never chose")


def test_copying_an_older_shared_posting_does_not_make_it_new():
    """A shared row that predates the geography record has no row and gets
    none when adopted; the copy keeps the legacy string gate (NULL verdict)."""
    from app.strategy.adoption import adopt_shared_jobs
    _profile(U1)
    now = datetime.utcnow()
    with get_session() as s:
        s.add(Job(user_id=P.SHARED_POOL_USER, source=JobSource.GREENHOUSE, external_id=_P + "old",
                  company="Acme", title="Machine Learning Engineer", location="",
                  url="https://x/old", description="Old posting.", posted_at=now,
                  first_seen=now - timedelta(days=3), discovered_at=now - timedelta(days=3)))
        s.commit()
    assert adopt_shared_jobs(U1) == 1
    copy = _copy(U1, "old")
    assert copy is not None and copy.eligibility is None
    assert _geo_row("old") is None, "adoption must not create a geography row for an old posting"


def test_scoring_queue_holds_unknown_and_the_slate_refuses_both(monkeypatch):
    from app.strategy import slate
    from app.strategy.scoring_lane import _user_queue
    _profile(U1)
    now = datetime.utcnow()
    with get_session() as s:
        for ext, elig in (("q-unk", UNKNOWN), ("q-ok", ELIGIBLE), ("q-legacy", None), ("q-bad", INELIGIBLE)):
            s.add(Job(user_id=U1, source=JobSource.GREENHOUSE, external_id=_P + ext, company="Acme",
                      title="ML Engineer", url=f"https://x/{ext}", description="d", posted_at=now,
                      first_seen=now, eligibility=elig, eligibility_reason="r" if elig else None))
        s.commit()
    with get_session() as s:
        ids = {j.external_id: j.id for j in s.exec(select(Job).where(Job.user_id == U1)).all()}
    queued = set(_user_queue(U1, 50))
    assert ids[_P + "q-ok"] in queued and ids[_P + "q-legacy"] in queued
    assert ids[_P + "q-unk"] not in queued, "unverified copies take no scoring slot"
    monkeypatch.setattr(settings, "geo_hold_unresolved", False)
    assert ids[_P + "q-unk"] in set(_user_queue(U1, 50)), "the hold is a switch"
    monkeypatch.setattr(settings, "geo_hold_unresolved", True)

    with get_session() as s:
        bad = s.get(Job, ids[_P + "q-bad"])
        bad.rerank_score = 95.0
        res = slate.place(s, bad, 95.0, user_id=U1)
        assert not res.created and res.outcome == "ineligible", "a 95 never overrides eligibility"
        unk = s.get(Job, ids[_P + "q-unk"])
        unk.rerank_score = 95.0
        res = slate.place(s, unk, 95.0, user_id=U1)
        assert not res.created and res.outcome == "unverified_location"
        ok = s.get(Job, ids[_P + "q-ok"])
        ok.rerank_score = 95.0
        assert slate.place(s, ok, 95.0, user_id=U1).created
        s.commit()


def test_the_scoring_lane_stamps_an_ineligible_copy_without_a_call(monkeypatch):
    from app.strategy import scoring_lane as sl
    now = datetime.utcnow()
    with get_session() as s:
        j = Job(user_id=U1, source=JobSource.GREENHOUSE, external_id=_P + "stamp", company="Acme",
                title="ML Engineer", url="https://x/s", description="d" * 100, posted_at=now,
                first_seen=now, eligibility=INELIGIBLE, eligibility_reason="Located in Sweden")
        s.add(j)
        s.commit()
        jid = j.id

    class _Boom:
        def prescore(self, *a, **k):
            raise AssertionError("Tier-1 must not be called for an ineligible copy")

        def score_with_meta(self, *a, **k):
            raise AssertionError("Tier-2 must not be called for an ineligible copy")

    ctx = sl._Ctx("resume", _Boom(), True, 40)
    assert sl._score_job_owned(jid, ctx) == ("drained", jid, None, None)
    with get_session() as s:
        row = s.get(Job, jid)
    assert row.rerank_score == gv.INELIGIBLE_STAMP_SCORE and row.scored_at is not None
    assert row.rerank_reasoning.startswith("Location filtered: Located in Sweden")


# ═════════════════════════════════════════════════════════════════════════════
# Verification: page first, model only on interpretable text, once per posting
# ═════════════════════════════════════════════════════════════════════════════

def _held_posting(ext, users=(U1,), **raw_kw):
    """A NEW posting with no usable location, in the shared pool and held
    (eligibility=unknown) in each user's pool."""
    # A company per posting: same company + title + blank location is ONE
    # posting by cross-source slug, and the second would be deduplicated away.
    raw = _raw(ext, location="", source="teamtailor", company=f"Acme {ext}",
               url=f"https://acme.teamtailor.com/jobs/{_P}{ext}", **raw_kw)
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    for uid in users:
        P._upsert([raw], user_id=uid, preferred_country="United States",
                  user_keywords=["machine learning engineer"], geo_prefs=US_HOME)
    return raw


def test_blank_location_resolved_from_the_official_page_rejects_a_us_user(monkeypatch):
    _profile(U1, location="Cincinnati, OH")
    _held_posting("se")
    assert _copy(U1, "se").eligibility == UNKNOWN
    calls = {"fetch": 0, "llm": 0}

    def _fake_fetch(url, timeout):
        calls["fetch"] += 1
        return _page(_jsonld(jobLocation={"@type": "Place", "address": {
            "@type": "PostalAddress", "addressLocality": "Stockholm", "addressCountry": "SE"}}))

    def _no_llm(*a, **k):
        calls["llm"] += 1
        raise AssertionError("the page answered; no model call is needed")

    monkeypatch.setattr(gv, "_fetch", _fake_fetch)
    monkeypatch.setattr(gv, "_call_llm", _no_llm)
    out = gv.verify_pending()
    assert out["resolved"] == 1 and calls == {"fetch": 1, "llm": 0}
    row = _geo_row("se", "teamtailor")
    assert row.status == "resolved" and json.loads(row.countries_json) == ["sweden"]
    assert row.evidence_source == "page_jsonld" and row.attempts == 1
    copy = _copy(U1, "se")
    assert copy.eligibility == INELIGIBLE and copy.rerank_score == gv.INELIGIBLE_STAMP_SCORE
    assert "Sweden" in copy.eligibility_reason and "United States" in copy.eligibility_reason
    assert copy.rerank_reasoning.startswith("Location filtered:")


def test_department_suffix_page_says_paris(monkeypatch):
    _profile(U1)
    _held_posting("pos", title="Backend Engineer - POS Integrations")
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: _page(_jsonld(
        jobLocation=[{"@type": "Place", "address": {"addressLocality": "Paris", "addressCountry": "FR"}}])))
    gv.verify_pending()
    row = _geo_row("pos", "teamtailor")
    assert json.loads(row.countries_json) == ["france"]
    assert json.loads(row.sites_json) == ["Paris, FR"]
    assert _copy(U1, "pos").eligibility == INELIGIBLE


def test_verification_runs_once_and_serves_every_user(monkeypatch):
    _profile(U1)
    _profile(U2, preferred_country="Sweden")
    _held_posting("shared", users=(U1, U2))
    calls = {"fetch": 0}

    def _fake_fetch(url, timeout):
        calls["fetch"] += 1
        return _page(_jsonld(jobLocation={"address": {"addressLocality": "Stockholm",
                                                      "addressCountry": "Sweden"}}))

    monkeypatch.setattr(gv, "_fetch", _fake_fetch)
    out = gv.verify_pending()
    assert calls["fetch"] == 1 and out["copies_updated"] == 2
    assert _copy(U1, "shared").eligibility == INELIGIBLE
    assert _copy(U2, "shared").eligibility == ELIGIBLE, "same posting, Swedish user: eligible"
    # A later sighting of the same posting reuses the row — no re-derivation.
    import hashlib
    raw = _raw("shared", location="", source="teamtailor", company="Acme shared",
               url=f"https://acme.teamtailor.com/jobs/{_P}shared")
    key = ("teamtailor", _P + "shared")
    chash = hashlib.sha256(raw.description.encode()).hexdigest()
    before = gv.metrics_snapshot(reset=True)
    got = gv.capture([raw], new_keys={key}, changed_keys=set(), content_hashes={key: chash})
    assert got[key].countries == ["sweden"]
    assert gv.metrics_snapshot().get("cache_hit") == 1
    assert _geo_row("shared", "teamtailor").cache_hits == 1
    assert not before.get("new_postings")
    # An EDITED description that still states no location does not undo what
    # the page established; the hash simply advances.
    edited = _raw("shared", location="", source="teamtailor", company="Acme shared",
                  url=f"https://acme.teamtailor.com/jobs/{_P}shared", desc="New text, no place.")
    got = gv.capture([edited], new_keys=set(), changed_keys={key},
                     content_hashes={key: hashlib.sha256(b"New text, no place.").hexdigest()})
    assert got[key].countries == ["sweden"] and got[key].status == "resolved"
    assert _copy(U1, "shared").eligibility == INELIGIBLE


def test_no_evidence_means_no_model_call_and_a_bad_quote_is_discarded(monkeypatch):
    _profile(U1)
    _held_posting("silent", desc="We ship rockets. " * 20)          # no location words at all
    _held_posting("liar", desc="Our team is distributed. The office is nice. " * 3)
    calls = []

    def _page_text(url, timeout):
        return _page("<html><body>Engineer wanted. Great benefits.</body></html>")

    def _fake_llm(provider, model, client, excerpts):
        calls.append(excerpts)
        # Confident, and NOT supported by the supplied text.
        return (json.dumps({"country": "Sweden", "locations": [], "work_mode": "onsite",
                            "permitted_regions": [], "evidence_quote": "Stockholm office required",
                            "conflicts": []}), {"input": 300, "output": 40}, model)

    monkeypatch.setattr(gv, "_fetch", _page_text)
    monkeypatch.setattr(gv, "_cheapest_backend", lambda: ("openai", "gpt-4o-mini", object()))
    monkeypatch.setattr(gv, "_call_llm", _fake_llm)
    out = gv.verify_pending()
    assert out["resolved"] == 0 and out["still_unknown"] == 2
    assert len(calls) == 1, "the posting with no location text never reaches the model"
    assert "office" in calls[0].lower()
    assert "rockets" not in calls[0]
    liar = _geo_row("liar", "teamtailor")
    assert liar.status == "unknown" and liar.last_error == "rejected_quote"
    assert liar.provider == "openai" and liar.input_tokens == 300
    assert _copy(U1, "liar").eligibility == UNKNOWN, "no invented country"
    # The sweep drains its counters into the cycle stats (one log line per
    # cycle, aggregate keys only — never a job id or URL).
    snap = out["geo_verify"]
    assert snap["llm_rejected_quote"] == 1 and snap["llm_skipped_no_evidence"] == 1
    assert snap["llm_cost_usd"] > 0, "the call that happened is still paid for and recorded"


def test_a_valid_quote_resolves_the_posting_and_records_the_cost(monkeypatch):
    _profile(U1)
    # Phrased so the RULES cannot read it (no "located/based/reside in X"
    # pattern) but a reader can: exactly the case that earns a model call.
    desc = ("Join us. This position requires you to live and work from within German "
            "borders (Wohnsitz in Deutschland). Python and SQL.")
    _held_posting("llm-ok", desc=desc)
    assert gv.derive(_raw("probe", desc=desc)).status == "unknown", "the rules must not already read this"
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: (None, "", "ConnectTimeout"))
    monkeypatch.setattr(gv, "_cheapest_backend", lambda: ("openai", "gpt-4o-mini", object()))
    monkeypatch.setattr(gv, "_call_llm", lambda p, m, c, ex: (json.dumps({
        "country": "Germany", "locations": [], "work_mode": "remote",
        "permitted_regions": ["Germany"],
        "evidence_quote": "This position requires you to live and work from within German borders",
        "conflicts": []}), {"input": 500, "output": 60}, "gpt-4o-mini-2024-07-18"))
    out = gv.verify_pending()
    assert out["resolved"] == 1
    row = _geo_row("llm-ok", "teamtailor")
    assert row.evidence_source == "llm" and json.loads(row.countries_json) == ["germany"]
    assert row.model == "gpt-4o-mini-2024-07-18" and row.est_cost_usd > 0
    assert _copy(U1, "llm-ok").eligibility == INELIGIBLE
    from app.analytics.spend import pending_spend
    assert any(k == "geo_verify" or (isinstance(k, tuple) and "geo_verify" in k)
               for k in _flatten_keys(pending_spend())), "spend lands under kind=geo_verify"


def _flatten_keys(d, acc=None):
    acc = [] if acc is None else acc
    if isinstance(d, dict):
        for k, v in d.items():
            acc.append(k)
            _flatten_keys(v, acc)
    elif isinstance(d, (list, tuple)):
        for v in d:
            _flatten_keys(v, acc)
    return acc


def test_provider_down_or_budget_exhausted_defers_without_a_retry_storm(monkeypatch):
    from app.matching import reranker as rr
    _profile(U1)
    _held_posting("down", desc="Our office is in the city centre. Hybrid schedule.")
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: (403, "", None))
    calls = {"n": 0}

    def _count(*a, **k):
        calls["n"] += 1
        raise AssertionError("no provider call while the breaker is open")

    monkeypatch.setattr(gv, "_call_llm", _count)
    rr._mark_provider_down("openai", error="credit balance too low")
    rr._mark_provider_down("anthropic", error="credit balance too low")
    out = gv.verify_pending()
    assert out["still_unknown"] == 1 and calls["n"] == 0
    row = _geo_row("down", "teamtailor")
    # A platform-level skip is not the posting's fault: it is deferred one
    # interval but spends NONE of its attempts, or a capped day would retire
    # every posting it could not reach for good.
    assert row.attempts == 0 and row.next_attempt_at > datetime.utcnow() + timedelta(minutes=30)
    assert row.last_error == "skipped:no_provider"
    # The very next cycle does NOT examine it again: it is deferred, not polled.
    out2 = gv.verify_pending()
    assert out2["pending_examined"] == 0
    # Budget: the daily cap is a hard stop too.
    rr._provider_down_until.clear()
    monkeypatch.setattr(settings, "geo_verify_llm_daily_cap", 1)
    for _ in range(1):
        gv._register_llm_call()
    assert gv.llm_extract("Applicants must be located in the kingdom of Narnia, "
                          "near the wardrobe office.").how == "skipped:daily_cap"
    # After max attempts the row is a cached "could not tell" — never re-asked.
    with get_session() as s:
        r = s.get(JobGeography, row.id)
        r.attempts, r.next_attempt_at = settings.geo_verify_max_attempts, datetime.utcnow() - timedelta(hours=1)
        s.add(r)
        s.commit()
    assert gv.verify_pending()["pending_examined"] == 0


def test_a_failed_outcome_write_does_not_refetch_every_cycle(monkeypatch):
    """The row is claimed BEFORE any network work, so a database failure while
    recording the outcome (the statement-timeout class) leaves it deferred —
    not re-fetched every 90 s at zero progress."""
    _profile(U1)
    _held_posting("claim")
    fetches = {"n": 0}

    def _fake_fetch(url, timeout):
        fetches["n"] += 1
        return _page(_jsonld(jobLocation={"address": {"addressLocality": "Austin",
                                                      "addressRegion": "TX", "addressCountry": "US"}}))

    monkeypatch.setattr(gv, "_fetch", _fake_fetch)
    monkeypatch.setattr(gv, "_record_attempt",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("statement timeout")))
    assert gv.verify_pending()["pending_examined"] == 1
    assert fetches["n"] == 1
    assert gv.verify_pending()["pending_examined"] == 0, "claimed: not due again this cycle"
    assert fetches["n"] == 1
    row = _geo_row("claim", "teamtailor")
    assert row.status == "unknown" and row.next_attempt_at > datetime.utcnow() + timedelta(minutes=10)


def test_a_location_only_change_does_not_rewrite_the_text_or_the_embedding():
    """The first poll after a deploy that changes how an adapter spells a
    location touches every posting from that adapter. That must be two small
    columns per row — never a description rewrite, never a cleared embedding
    (which forces a from-scratch FAISS rebuild per user per matching tick)."""
    raw = _raw("locmove", location="Austin, TX")
    P._upsert([raw], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    with get_session() as s:
        job = s.exec(select(Job).where(Job.user_id == P.SHARED_POOL_USER,
                                       Job.external_id == _P + "locmove")).one()
        job.embedding_id = 4242
        s.add(job)
        s.commit()
    moved = _raw("locmove", location="Austin, TX · Berlin")     # same description
    P._upsert([moved], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    with get_session() as s:
        job = s.exec(select(Job).where(Job.user_id == P.SHARED_POOL_USER,
                                       Job.external_id == _P + "locmove")).one()
    assert job.location == "Austin, TX · Berlin"
    assert job.embedding_id == 4242, "a location-only change must not force a re-embed"
    # ...whereas an edited description still does.
    edited = _raw("locmove", location="Austin, TX · Berlin", desc="Entirely new text about models.")
    P._upsert([edited], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    with get_session() as s:
        job = s.exec(select(Job).where(Job.user_id == P.SHARED_POOL_USER,
                                       Job.external_id == _P + "locmove")).one()
    assert job.embedding_id is None and job.description == "Entirely new text about models."


def test_the_sweep_is_bounded_by_its_budget(monkeypatch):
    _profile(U1)
    for i in range(3):
        _held_posting(f"budget{i}")
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: _page(_jsonld(
        jobLocation={"address": {"addressLocality": "Austin", "addressRegion": "TX",
                                 "addressCountry": "US"}})))
    # Other test files' held fixtures may be due too (their postings now get
    # geography rows), so this pins the BOUND and the outcome, not an exact
    # count: no sweep ever exceeds its item budget, every sweep with work left
    # does some, and three postings cannot fit one sweep of two.
    sweeps = 0
    while not all(_copy(U1, f"budget{i}").eligibility == ELIGIBLE for i in range(3)):
        out = gv.verify_pending(budget=gv.Budget(seconds=60, items=2))
        sweeps += 1
        assert 0 < out["pending_examined"] <= 2, out
        assert sweeps <= 40, "held postings never resolved"
    assert sweeps >= 2
    for i in range(3):
        row = _geo_row(f"budget{i}", "teamtailor")
        assert row.status == "resolved" and row.attempts == 1, "each posting was fetched exactly once"


def test_a_due_row_nobody_is_waiting_on_cannot_starve_a_held_posting(monkeypatch):
    """Production shape: the shared lane records many UNKNOWN postings no user
    has adopted (or whose copies have since scored or expired). Those rows are
    'due' forever and used to fill the sweep's window ahead of the postings a
    user is actually waiting on."""
    _profile(U1)
    now = datetime.utcnow()
    with get_session() as s:
        for i in range(12):
            s.add(JobGeography(source="teamtailor", external_id=f"{_P}orphan{i}", status="unknown",
                               next_attempt_at=now - timedelta(minutes=30 + i), location_hash="h",
                               created_at=now - timedelta(hours=1)))
        s.commit()
    monkeypatch.setattr(gv, "_PENDING_PAGE", 4)     # a small window, so the orphans would fill it
    _held_posting("waiting")
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: _page(_jsonld(
        jobLocation={"address": {"addressLocality": "Austin", "addressRegion": "TX",
                                 "addressCountry": "US"}})))
    out = gv.verify_pending(budget=gv.Budget(seconds=60, items=3))
    assert out["pending_examined"] == 1 and out["resolved"] == 1
    assert _copy(U1, "waiting").eligibility == ELIGIBLE
    assert out["geo_verify"]["deferred_no_held_copy"] == 12
    with get_session() as s:
        orphan = s.exec(select(JobGeography).where(JobGeography.external_id == _P + "orphan0")).one()
    assert orphan.next_attempt_at > now + timedelta(hours=1), "pushed one retry interval out"
    assert orphan.attempts == 0, "not an attempt: nothing was fetched or asked"
    # ...and the moment a user copy of it is admitted as unknown, it is due again.
    raw = _raw("orphan0", location="", source="teamtailor", company="Acme orphan",
               url=f"https://acme.teamtailor.com/jobs/{_P}orphan0")
    raw.first_seen = now - timedelta(hours=1)          # a copy of a known posting, not a new one
    P._upsert([raw], user_id=U1, preferred_country="United States",
              user_keywords=["machine learning engineer"], geo_prefs=US_HOME)
    with get_session() as s:
        orphan = s.exec(select(JobGeography).where(JobGeography.external_id == _P + "orphan0")).one()
    assert orphan.next_attempt_at <= datetime.utcnow()
    assert gv.verify_pending(budget=gv.Budget(seconds=60, items=3))["pending_examined"] == 1


def test_changed_location_evidence_invalidates_the_cached_decision():
    _profile(U1)
    raw = _raw("chg", location="Austin, TX")
    _shared_then_user(raw, U1)
    assert _copy(U1, "chg").eligibility == ELIGIBLE
    first = _geo_row("chg")
    # The same posting re-seen with a different site; the description did not
    # change, so only the location moved.
    moved = _raw("chg", location="Berlin, Germany")
    P._upsert([moved], user_id=P.SHARED_POOL_USER, user_keywords=["machine learning engineer"])
    row = _geo_row("chg")
    assert row.location_hash != first.location_hash
    assert json.loads(row.countries_json) == ["germany"]
    copy = _copy(U1, "chg")
    assert copy.eligibility == INELIGIBLE and copy.rerank_score == gv.INELIGIBLE_STAMP_SCORE
    with get_session() as s:
        shared = s.exec(select(Job.location).where(Job.user_id == P.SHARED_POOL_USER,
                                                   Job.external_id == _P + "chg")).first()
    assert shared == "Berlin, Germany", "the row renders what the gate decided on"


def test_greenhouse_detail_endpoint_is_the_page_cross_check(monkeypatch):
    seen = []

    def _fake_fetch(url, timeout):
        seen.append(url)
        if "boards-api.greenhouse.io" in url:
            return (200, json.dumps({"location": {"name": "Remote"},
                                     "offices": [{"name": "Chicago", "location": "Chicago, IL"}],
                                     "metadata": [{"name": "Workplace Type", "value": "Hybrid"}]}), None)
        raise AssertionError("the official endpoint answers; the HTML page is not fetched")

    monkeypatch.setattr(gv, "_fetch", _fake_fetch)
    ev = gv.page_evidence("https://boards.greenhouse.io/acme/jobs/12345", "greenhouse")
    assert ev.how == "ats_detail" and ev.geo is not None
    assert ev.geo.countries == ["united states"] and ev.geo.work_mode == "hybrid"
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/12345"]


def test_hands_off_hosts_and_aggregators_are_never_fetched(monkeypatch):
    monkeypatch.setattr(gv, "_fetch", lambda url, timeout: (_ for _ in ()).throw(AssertionError(url)))
    assert gv.page_evidence("https://www.linkedin.com/jobs/view/1", "linkedin").how == "skipped:source"
    assert gv.page_evidence("https://indeed.com/rc/clk?x", "indeed").how == "skipped:source"
    assert gv.page_evidence("", "greenhouse").how == "skipped:source"


def test_the_cheapest_configured_model_is_chosen(monkeypatch):
    from app.common import llm as llm_mod
    from app.matching import reranker as rr
    monkeypatch.setattr(llm_mod, "shared_openai", lambda **k: object())
    monkeypatch.setattr(llm_mod, "shared_anthropic", lambda **k: object())
    monkeypatch.setattr(rr, "provider_available", lambda name: True)
    provider, model, _client = gv._cheapest_backend()
    assert (provider, model) == ("openai", settings.geo_verify_model_openai)
    monkeypatch.setattr(rr, "provider_available", lambda name: name == "anthropic")
    assert gv._cheapest_backend()[0] == "anthropic", "a provider in cooldown is skipped"
    monkeypatch.setattr(rr, "provider_available", lambda name: False)
    assert gv._cheapest_backend() is None


def test_quote_validation_is_verbatim_but_tolerant_of_trimming():
    text = "Candidates must reside in the United States and be able to travel occasionally."
    assert gv.quote_is_verbatim("candidates must reside in the united states", text)
    assert gv.quote_is_verbatim("Candidates must reside in the United States and be able to", text)
    assert not gv.quote_is_verbatim("United", text), "too short to be evidence"
    assert not gv.quote_is_verbatim("Candidates must reside in Canada", text)


def test_geography_holds_no_user_data():
    cols = set(JobGeography.__table__.columns.keys())
    assert "user_id" not in cols and not any("resume" in c for c in cols)
