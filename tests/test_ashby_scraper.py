"""Ashby parsing: the fields the posting API actually emits.

The scraper read `locationName`, a key the Ashby posting API does not return,
so every Ashby posting was stored with a BLANK location and `remote=isRemote`.
In a 31-card audit sample (2026-09-17) 11 cards were Ashby rows with no
location while every one of them named a city on the page; 7 of 15 opened
postings were in the wrong country for the user (Snowflake Warsaw hybrid,
"Remote, Tiranë, Albania +27 more", Lendable/Magentic London hybrid, Nord
Security Vilnius/Kaunas, Addepto "Remote - Poland" — all for a US user;
Paddle Philippines for a UK user, scored 90). Salary was on five of those
postings and on none of the cards.

Payloads here are shaped like the real response: id, title, location (string),
secondaryLocations [{location, address}], address {postalAddress {...}},
isRemote, isListed, publishedAt, jobUrl, descriptionHtml/Plain and
compensation {compensationTierSummary, scrapeableCompensationSalarySummary}.
`--disable-socket` is on: httpx.get is monkeypatched, nothing leaves the box.

DB rows created here carry the `ashby-sal-` user prefix and are deleted by it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from sqlmodel import delete, select

from app.common.geo import detect_country, location_allowed
from app.db.init_db import get_session
from app.db.models import Application, FunnelEvent, Job, JobSource, UserProfile
from app.discovery import pipeline as P
from app.discovery.ashby import AshbyScraper
from app.discovery.base import RawJob

US = "United States"
UK = "United Kingdom"
_POOL = "ashby-sal-pool"
_FACET_POOL = "ashby-sal-facet"
_ADOPT_USER = "ashby-sal-adopt"
_SHARED_PREFIX = "ashby-sal-shared-"


def _serve(monkeypatch, jobs):
    """Canned Ashby response — the scraper's parse runs with no network."""

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"apiVersion": "1", "jobs": jobs}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())


def _postal(locality, region, country):
    return {"postalAddress": {"addressLocality": locality,
                              "addressRegion": region,
                              "addressCountry": country}}


def _posting(pid, **over):
    base = {
        "id": pid, "title": "Software Engineer", "department": "Engineering",
        "team": "Platform", "employmentType": "FullTime", "location": "",
        "secondaryLocations": [], "isRemote": False, "isListed": True,
        "publishedAt": "2026-09-10T12:00:00.000Z",
        "jobUrl": f"https://jobs.ashbyhq.com/acme/{pid}",
        "applyUrl": f"https://jobs.ashbyhq.com/acme/{pid}/application",
        "descriptionHtml": "<p>Build the platform.</p>",
        "descriptionPlain": "Build the platform.",
    }
    base.update(over)
    return base


WARSAW = _posting("warsaw", location="Warsaw", isRemote=False,
                  address=_postal("Warsaw", "Masovian Voivodeship", "Poland"),
                  compensation={
                      "compensationTierSummary": "$160K – $200K • Offers Equity",
                      "scrapeableCompensationSalarySummary": "$160,000 - $200,000 per year",
                  })
SF = _posting("sf", location="San Francisco", isRemote=True,
              address=_postal("San Francisco", "California", "United States"),
              compensation={
                  "scrapeableCompensationSalarySummary": "$200,000 - $250,000 per year",
              })
SAN_MATEO = _posting("sanmateo", location="", isRemote=True,
                     address=_postal("San Mateo", "CA", "US"))
MULTI = _posting("multi", location="Remote", isRemote=True, secondaryLocations=[
    {"location": "Tiranë, Albania", "address": _postal("Tiranë", "", "Albania")},
    {"location": "Lisbon, Portugal"},
    {"location": "Remote"},                     # duplicate of the primary
    {"location": "Madrid"}, {"location": "Berlin"},
    {"location": "Paris"}, {"location": "Rome"},
])
UNLISTED = _posting("hidden", location="New York", isListed=False)


def _fetch(monkeypatch, *jobs):
    _serve(monkeypatch, list(jobs))
    return {j.external_id: j for j in AshbyScraper("acme").fetch()}


# ── (1) a city posting stays a city posting ─────────────────────────────────

def test_warsaw_posting_keeps_its_city_and_is_not_remote(monkeypatch):
    job = _fetch(monkeypatch, WARSAW)["warsaw"]
    assert job.location == "Warsaw"
    assert job.remote is False
    # ...and the country gate can now see it, which is the whole point.
    assert detect_country(job.location) == "poland"
    assert not location_allowed(job.location, job.remote, US, True)


# ── (2) isRemote never blanks the location ──────────────────────────────────

def test_is_remote_is_a_flag_beside_the_location_not_instead_of_it(monkeypatch):
    job = _fetch(monkeypatch, SF)["sf"]
    assert job.remote is True
    # "San Francisco" alone names no country the gate knows, so the postal
    # region and country are appended; the string leads with what the page shows.
    assert job.location == "San Francisco, California, United States"
    assert detect_country(job.location) == "united states"
    assert location_allowed(job.location, job.remote, US, True)
    assert not location_allowed(job.location, job.remote, UK, True)


# ── (3) postal address stands in for a blank location string ────────────────

def test_postal_address_is_the_location_when_the_string_is_blank(monkeypatch):
    job = _fetch(monkeypatch, SAN_MATEO)["sanmateo"]
    assert job.location == "San Mateo, CA, US"
    assert job.remote is True
    assert detect_country(job.location) == "united states"


# ── (4) every site of a multi-site posting is named ─────────────────────────

def test_secondary_locations_are_appended_distinct_and_capped(monkeypatch):
    job = _fetch(monkeypatch, MULTI)["multi"]
    parts = job.location.split(" · ")
    assert parts[0] == "Remote"
    assert parts[1] == "Tiranë, Albania"
    assert parts[2] == "Lisbon, Portugal"
    # the duplicate "Remote" is dropped; the cap leaves 1 + 4 sites and says
    # how many were cut, the way Ashby's own page does ("+27 more")
    assert len(parts) == 5
    assert parts[-1].endswith("+2 more")
    assert job.remote is True
    # A US user no longer receives it: the string names a foreign country.
    assert not location_allowed(job.location, job.remote, US, True)


# ── (5) compensation summary → salary_text ───────────────────────────────────

def test_compensation_summary_becomes_salary_text(monkeypatch):
    jobs = _fetch(monkeypatch, WARSAW, SF, SAN_MATEO)
    # the human summary wins when both are present
    assert jobs["warsaw"].salary_text == "$160K – $200K • Offers Equity"
    # the scrapeable form is the fallback
    assert jobs["sf"].salary_text == "$200,000 - $250,000 per year"
    # no compensation block → None, never "" (None means "source says nothing")
    assert jobs["sanmateo"].salary_text is None


def test_salary_text_is_trimmed_and_capped(monkeypatch):
    long = "$1 " * 100
    blank = _posting("blank", compensation={"compensationTierSummary": "   "})
    huge = _posting("huge", compensation={"compensationTierSummary": long})
    jobs = _fetch(monkeypatch, blank, huge)
    assert jobs["blank"].salary_text is None
    assert len(jobs["huge"].salary_text) <= 120
    assert "  " not in jobs["huge"].salary_text


# ── (6) unlisted postings are not public ────────────────────────────────────

def test_unlisted_postings_are_skipped(monkeypatch):
    jobs = _fetch(monkeypatch, UNLISTED, WARSAW)
    assert "hidden" not in jobs
    assert "warsaw" in jobs


def test_missing_is_listed_means_listed(monkeypatch):
    p = _posting("old-tenant", location="Austin, TX")
    del p["isListed"]
    assert "old-tenant" in _fetch(monkeypatch, p)


# ── the rest of the row ─────────────────────────────────────────────────────

def test_posted_at_department_and_team_are_unchanged(monkeypatch):
    job = _fetch(monkeypatch, WARSAW)["warsaw"]
    assert job.posted_at == datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    assert job.context["department"].value == "Engineering"
    assert job.context["team"].value == "Platform"
    assert job.context["ats"].value == "ashby"
    assert job.url == "https://jobs.ashbyhq.com/acme/warsaw"
    assert job.description == "Build the platform."


def test_description_plain_is_the_fallback_when_html_is_missing(monkeypatch):
    p = _posting("plain", descriptionHtml=None, descriptionPlain="Only plain text.")
    assert _fetch(monkeypatch, p)["plain"].description == "Only plain text."


def test_remote_in_the_text_still_counts(monkeypatch):
    p = _posting("txt", location="Remote - US", isRemote=False)
    job = _fetch(monkeypatch, p)["txt"]
    assert job.remote is True
    assert job.location == "Remote - US"


# ── the seven audit postings, as the gate now sees them ─────────────────────

@pytest.mark.parametrize("location, remote, user", [
    ("Warsaw", False, US),                                   # Snowflake hybrid
    ("Remote · Tiranë, Albania · Lisbon, Portugal", True, US),
    ("London", False, US),                                    # Lendable / Magentic
    ("Vilnius, Lithuania · Kaunas, Lithuania", False, US),   # Nord Security
    ("Remote - Poland", True, US),                            # Addepto
    ("Philippines", True, UK),                                # Paddle, scored 90
])
def test_the_audit_s_wrong_country_ashby_postings_are_gated(location, remote, user):
    assert not location_allowed(location, remote, user, True), location


# ── salary_text reaches the row, and the regex facet never blanks it ────────

def _clean(pools):
    """Only OUR rows: the user pools above, our shared-pool rows by external_id
    prefix, and our profile. A wholesale delete(Job) would take out fixtures
    other files built."""
    with get_session() as s:
        ids = [j.id for j in s.exec(select(Job).where(
            Job.user_id.in_(pools)
            | ((Job.user_id == P.SHARED_POOL_USER)
               & Job.external_id.like(f"{_SHARED_PREFIX}%")))).all()]
        if ids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(ids)))
            s.exec(delete(Application).where(Application.job_id.in_(ids)))
            s.exec(delete(Job).where(Job.id.in_(ids)))
        s.exec(delete(UserProfile).where(UserProfile.user_id == _ADOPT_USER))
        s.commit()


@pytest.fixture
def _db_isolate():
    _clean([_POOL, _FACET_POOL, _ADOPT_USER])
    yield
    _clean([_POOL, _FACET_POOL, _ADOPT_USER])


def _raw(ext, salary_text):
    return RawJob(
        # Distinct titles: two raws with the same company+title+location are
        # one posting by cross_source_slug, and the second is deduped away.
        source="ashby", external_id=ext, company="Acme", title=f"Backend Engineer ({ext})",
        location="Austin, TX", remote=False,
        url=f"https://jobs.ashbyhq.com/acme/{ext}",
        # The regex facet WOULD find a (different) number here — the ATS's own
        # statement must still win.
        description="Backend engineer. Python and SQL. $150,000 - $200,000 a year. " * 4,
        posted_at=datetime.utcnow(), salary_text=salary_text,
    )


def test_scraper_salary_wins_over_the_regex_facet_and_regex_is_the_fallback(_db_isolate):
    n = P._upsert([_raw("ats-says", "$160K – $200K • Offers Equity"),
                   _raw("ats-silent", None)],
                  user_id=_POOL, user_keywords=["backend engineer"])
    assert n == 2
    with get_session() as s:
        rows = {j.external_id: j for j in
                s.exec(select(Job).where(Job.user_id == _POOL)).all()}
    assert rows["ats-says"].salary_text == "$160K – $200K • Offers Equity"
    # No structured field → the description regex, exactly as before.
    assert rows["ats-silent"].salary_text
    assert rows["ats-silent"].salary_text.startswith("$150,000")


def test_facet_backfill_and_restamp_keep_a_salary_the_regex_cannot_see(_db_isolate):
    from app.strategy.job_facets import backfill, restamp_facets
    with get_session() as s:
        s.add(Job(source=JobSource.ASHBY, external_id="ashby-sal-kept", company="Acme",
                  title="Backend Engineer", location="Austin, TX",
                  url="https://jobs.ashbyhq.com/acme/kept",
                  description="No pay stated in the body.",   # regex finds nothing
                  user_id=_FACET_POOL, salary_text="$160K – $200K • Offers Equity"))
        s.commit()

    def _stored():
        with get_session() as s:
            return s.exec(select(Job.salary_text)
                          .where(Job.user_id == _FACET_POOL)).first()

    assert backfill(_FACET_POOL, pause=0) == 1
    assert _stored() == "$160K – $200K • Offers Equity", "backfill blanked the ATS salary"
    restamp_facets(pause=0)
    assert _stored() == "$160K – $200K • Offers Equity", "restamp blanked the ATS salary"


def test_adoption_copies_the_ats_salary_into_the_user_s_pool(_db_isolate):
    """Adoption rebuilds a RawJob from the shared row, so a field it does not
    carry is re-derived by _build_job — for salary that meant the regex, which
    finds nothing in a posting whose pay lives only in Ashby's compensation
    block. Same class as first_seen: a copy must not lose what the original knew."""
    from app.strategy.adoption import adopt_shared_jobs
    now = datetime.utcnow()
    with get_session() as s:
        s.add(UserProfile(user_id=_ADOPT_USER, target_roles="Backend Engineer"))
        s.add(Job(user_id=P.SHARED_POOL_USER, source=JobSource.ASHBY,
                  external_id=f"{_SHARED_PREFIX}1", company="Acme",
                  title="Senior Backend Engineer", location="Austin, TX", remote=False,
                  url="https://jobs.ashbyhq.com/acme/shared-1",
                  description="Pay is not stated in the body.",
                  posted_at=now, first_seen=now, discovered_at=now,
                  salary_text="$160K – $200K • Offers Equity"))
        s.commit()

    assert adopt_shared_jobs(_ADOPT_USER) == 1
    with get_session() as s:
        copy = s.exec(select(Job).where(Job.user_id == _ADOPT_USER,
                                        Job.external_id == f"{_SHARED_PREFIX}1")).one()
    assert copy.salary_text == "$160K – $200K • Offers Equity"
