"""US jobs that sat "Location pending" until they expired (production 2026-10-29).

One profile's held copies after a day: 83, of which 55 were Adzuna. Adzuna's
display name is "Atlanta, Fulton County" — a town and a county, no state, no
country — and its descriptions are short snippets that name no place, so
neither intake nor the verifier's model step had anything to read. The result
says its market twice (`location.area` starts with "US"; `redirect_url` is on
adzuna.com) and the adapter read neither. Two more were remote postings scoped
to "North America" / "AMER", regions the location rules did not know.

Each fix is pinned at the decision a user feels: eligible for a US user, still
ineligible for someone the posting excludes.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.common.eligibility import ELIGIBLE, INELIGIBLE, GeoPrefs, decide
from app.config import settings
from app.discovery.base import RawJob
from app.discovery.geo_verify import derive, page_evidence
from app.discovery.sources.adzuna import AdzunaSource, adzuna_location, market_from_url

US_MOVER = GeoPrefs(country="united states", remote_ok=True, open_to_relocation=True,
                    home_location="Cincinnati, OH")
US_HOME = GeoPrefs(country="united states", remote_ok=True, open_to_relocation=False,
                   home_location="Atlanta, GA")
UK = GeoPrefs(country="united kingdom", remote_ok=True, open_to_relocation=True)
DE = GeoPrefs(country="germany", remote_ok=True, open_to_relocation=True)


# ── Adzuna reads its own market ──────────────────────────────────────────────

@pytest.mark.parametrize("area,display,want_loc", [
    (["US", "Georgia", "Fulton County", "Atlanta"], "Atlanta, Fulton County", "Atlanta, GA"),
    (["US", "Massachusetts", "Suffolk County", "Boston"], "Boston, Suffolk County", "Boston, MA"),
    (["US", "California", "Los Angeles County"], "Los Angeles County", "California, US"),
    (["US", "Texas"], "Texas", "Texas, US"),
])
def test_a_us_result_becomes_city_and_state(area, display, want_loc):
    loc, geo = adzuna_location({"area": area, "display_name": display},
                               "https://www.adzuna.com/land/ad/1")
    assert loc == want_loc
    assert geo.country == "US" and geo.country_field == "location.area[0]"
    g = derive(RawJob(source="adzuna", external_id="x", company="c", title="t",
                      location=loc, remote=False, url="", description="Python role.", geo=geo))
    assert g.resolved and g.countries == ["united states"]
    assert decide(g, US_MOVER).status == ELIGIBLE
    assert decide(g, UK).status == INELIGIBLE


def test_georgia_under_a_us_market_is_the_state():
    loc, geo = adzuna_location({"area": ["US", "Georgia", "Chatham County", "Savannah"],
                                "display_name": "Savannah, Chatham County"}, "")
    g = derive(RawJob(source="adzuna", external_id="x", company="c", title="t", location=loc,
                      remote=False, url="", description="", geo=geo))
    assert loc == "Savannah, GA" and "georgia" not in g.countries


def test_a_uk_result_is_the_uk():
    loc, geo = adzuna_location({"area": ["UK", "London"], "display_name": "London, UK"},
                               "https://www.adzuna.co.uk/jobs/details/1")
    g = derive(RawJob(source="adzuna", external_id="x", company="c", title="t", location=loc,
                      remote=False, url="", description="", geo=geo))
    assert decide(g, UK).status == ELIGIBLE and decide(g, US_MOVER).status == INELIGIBLE


@pytest.mark.parametrize("url,market", [
    ("https://www.adzuna.com/land/ad/5945677854?se=x", "US"),
    ("https://www.adzuna.co.uk/jobs/details/5945815884", "GB"),
    ("https://www.adzuna.ca/details/1", "CA"),
    ("https://evil.example/adzuna.com", ""),
    ("", ""),
])
def test_the_link_names_the_market(url, market):
    assert market_from_url(url) == market


def test_the_adapter_attaches_the_evidence():
    payload = {"results": [{
        "id": 77, "title": "Python Developer", "company": {"display_name": "Acme"},
        "location": {"area": ["US", "Ohio", "Hamilton County", "Cincinnati"],
                     "display_name": "Cincinnati, Hamilton County"},
        "redirect_url": "https://www.adzuna.com/land/ad/77", "description": "Build APIs.",
        "created": "2026-10-29T10:00:00Z"}]}

    class _R:
        status_code = 200

        def json(self):
            return payload

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _R()

    with patch.object(settings, "adzuna_app_id", "id"), \
         patch.object(settings, "adzuna_app_key", "key"), \
         patch.object(settings, "adzuna_enabled", True, create=True), \
         patch("app.discovery.sources.adzuna.httpx.AsyncClient", lambda **k: _C()):
        jobs = asyncio.run(AdzunaSource(keywords=["python developer"]).fetch_jobs())
    assert len(jobs) == 1
    j = jobs[0]
    assert j.location == "Cincinnati, OH" and j.geo.country == "US"
    assert decide(derive(j), US_MOVER).status == ELIGIBLE
    # Someone staying in Atlanta: the COUNTRY is settled; what is still open is
    # whether a Cincinnati job is remote, which is the right question to hold on.
    assert decide(derive(j), US_HOME).code == "work_mode_unresolved"


def test_a_stored_row_is_settled_from_its_link_without_a_fetch(monkeypatch):
    """Rows stored before the adapter fix carry only "Uptown, Marion County"
    and a snippet; the verifier resolves them from the link's host."""
    import app.discovery.geo_verify as gv
    monkeypatch.setattr(gv, "_fetch", lambda *a, **k: pytest.fail("no network needed"))
    page = page_evidence("https://www.adzuna.com/land/ad/5945677854?se=x", "adzuna",
                         description="Great Python role with growth.")
    assert page.how == "source_market"
    assert page.geo is not None and page.geo.resolved
    assert decide(page.geo, US_MOVER).status == ELIGIBLE
    # a restriction in the posting still wins over the market
    page = page_evidence("https://www.adzuna.com/land/ad/1", "adzuna",
                         description="Candidates must be based in Germany.")
    assert page.geo is None or decide(page.geo, US_MOVER).status != ELIGIBLE


# ── North America and the Americas are regions ───────────────────────────────

@pytest.mark.parametrize("site,us,de", [
    ("Remote: North America - East", ELIGIBLE, INELIGIBLE),
    ("Remote in AMER", ELIGIBLE, INELIGIBLE),
    ("Remote (Americas)", ELIGIBLE, INELIGIBLE),
    ("Remote (EMEA)", INELIGIBLE, ELIGIBLE),
    ("Remote (LATAM)", INELIGIBLE, INELIGIBLE),
])
def test_remote_regions_decide(site, us, de):
    g = derive(RawJob(source="greenhouse", external_id="x", company="c", title="t",
                      location=site, remote=True, url="", description="Remote role."))
    assert decide(g, US_MOVER).status == us
    assert decide(g, DE).status == de


def test_american_in_a_name_is_not_a_region():
    from app.common.geo import detect_region
    assert detect_region("American Express, New York, NY") == ""
    assert detect_region("Bank of America Plaza, Charlotte, NC") == ""


def test_the_market_settles_the_country_not_the_city():
    """Review 2026-10-29: the market probe carried no sites, so a stored
    "Atlanta, GA" row was re-decided as eligible for someone staying in Boston,
    and a paid final followed. The stored sites go into the probe."""
    boston = GeoPrefs(country="united states", remote_ok=True, open_to_relocation=False,
                      home_location="Boston, MA")
    page = page_evidence("https://www.adzuna.com/land/ad/9", "adzuna",
                         description="Backend role.", sites=["Atlanta, GA"])
    assert page.geo is not None and page.geo.sites == ["Atlanta, GA"]
    assert decide(page.geo, boston).status != ELIGIBLE
    assert decide(page.geo, US_MOVER).status == ELIGIBLE


def test_the_waiting_read_stops_at_the_deadline_and_rotates(monkeypatch):
    import time as _time

    import app.discovery.geo_verify as gv
    from app.db.init_db import get_session
    with get_session() as s:
        assert gv._waiting_rows(s, 5, gv.datetime.utcnow(), 3, owners=["a", "b", "c"],
                                deadline=_time.monotonic() - 1) == []
    assert gv._WAITING_RESUME == ["b"], "the next sweep starts one owner later"
    with get_session() as s:
        gv._waiting_rows(s, 5, gv.datetime.utcnow(), 3, owners=["a", "b", "c"])
    assert gv._WAITING_RESUME == ["c"]
