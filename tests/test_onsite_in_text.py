"""A posting that says it is on-site only in its text is read as on-site, and a
posting that says nothing is not handed to someone who left on-site work out.

Audit of delivered jobs, 2026-11-20: "Process Admin", Fountain Valley CA, was
delivered at 78 to a profile whose only work style was remote. The scorer wrote
"On-site only; candidate prefers remote work" — and scored it 78 anyway,
because only the eligibility decision may rule a posting out, and that decision
had nothing to go on:

  * the source had no work-mode field, and the three on-site patterns for prose
    matched none of the posting's three sentences ("Based on-site at …",
    "Ability to work on-site at …", "Working on-site at …");
  * the work-style rule only binds a STATED mode, so a posting stating none
    passed for a remote-only profile exactly as it does for anyone.

Reading the same patterns the other way found "This is not a remote position"
stored as REMOTE: the phrase "remote position" matched.

Pinned here:
  * the description reader knows the on-site wordings, fills only a mode
    nothing else states, and never overturns a structured "remote";
  * a visit "as needed", "quarterly" or "at client sites" is not on-site work;
  * a negated remote statement is not remote;
  * a posting with a place and NO stated mode is HELD (`work_style_unresolved`)
    for a user whose work styles leave out on-site, and for nobody else.
"""
from __future__ import annotations

import pytest

from app.common.eligibility import (
    ELIGIBLE, INELIGIBLE, RULES_VERSION, UNKNOWN, Geography, GeoPrefs, decide,
)
from app.discovery.base import GeoEvidence, RawJob
from app.discovery.geo_verify import attendance_from_description, derive, work_mode_from_description

#: The production posting's three sentences, as written.
PROCESS_ADMIN = (
    "Based on-site at HMA headquarters in Fountain Valley, California, this role supports "
    "established workflows across the claims team.\n"
    "Requirements\n"
    "• Ability to work on-site at HMA headquarters in Fountain Valley, California\n"
    "• Two years of administrative experience\n"
    "Working on-site at HMA headquarters provides exposure to business processes and "
    "cross-functional teams, with room to grow.")


@pytest.mark.parametrize("text", [
    PROCESS_ADMIN,
    "Based on-site at HMA headquarters in Fountain Valley, California, this role supports workflows.",
    "• Ability to work on-site at HMA headquarters in Fountain Valley, California",
    "Working on-site at HMA headquarters provides exposure to business processes.",
    "You must be able to work in the office, Monday to Friday.",
    "Candidates need to work on-site in our Columbus warehouse.",
    "This is an on-site, full-time position at our Dallas clinic.",
    "This is an in-person role.",
    "Work Location: On-site",
    "Workplace type: Onsite\nDepartment: Finance",
    "Location type - In-Office",
    "The position is 100% on-site.",
    "This is not a remote position.",
    "Please note that this is not a remote role; you will join the team in Tampa.",
    "Remote work is not available for this role.",
    "There is no remote option for this position.",
    "This position is not eligible for remote work.",
    "We do not offer remote work for this team.",
    "This is a non-remote role in our Akron plant.",
    "This is not currently a fully remote position.",
    "We run a hybrid cloud platform. This is not a remote job.",
])
def test_on_site_as_a_posting_says_it(text):
    assert work_mode_from_description(text) == "onsite"


@pytest.mark.parametrize("text,mode", [
    # A visit is not where the work is done.
    ("Consultants work on-site at client locations as needed.", ""),
    ("Ability to work on-site at our Austin office as needed for planning weeks.", ""),
    ("Must be able to work on-site quarterly for company events.", ""),
    ("Willingness to work in person occasionally at the Denver hub.", ""),
    ("You will sometimes be working on-site at customer facilities.", ""),
    ("Able to work on-site for team offsites twice a year.", ""),
    # Both offered.
    ("Work Location: On-site or Remote", ""),
    ("Location: On-site / Hybrid", ""),
    # Not about this job's working arrangement.
    ("We provide on-site childcare and an on-site gym.", ""),
    ("Experience managing on-site construction crews.", ""),
    ("Our customers run hybrid cloud and remote monitoring tools.", ""),
    ("", ""),
    # What the reader already knew, unchanged.
    ("This is a fully remote role.", "remote"),
    ("Remote-first company; work from home anywhere in the US.", "remote"),
    ("This role is hybrid, three days in the office.", "hybrid"),
    ("We work 3 days per week in the office.", "hybrid"),
    ("You are expected to work in the office.", "onsite"),
    # A remote statement is kept when it is not the negated one.
    ("We are not a remote-first company, but this role is fully remote.", "remote"),
    # A "no" about something else negates nothing (read as it was before).
    ("There is no dress code; work from home on Fridays.", "remote"),
    ("No travel required. This is a remote position.", "remote"),
    ("Not sure yet? This is a fully remote role, so apply from anywhere.", "remote"),
    # Not remote, and the posting calls the week hybrid.
    ("This role is not fully remote: our team is hybrid.", "hybrid"),
])
def test_what_is_not_an_on_site_statement(text, mode):
    assert work_mode_from_description(text) == mode


def _raw(description, *, mode="", location="Fountain Valley, CA, United States", remote=False):
    return RawJob(source="smartrecruiters", external_id="osit-1", company="MSX International",
                  title="Process Admin", location=location, remote=remote,
                  url="https://jobs.smartrecruiters.com/MSX/1", description=description,
                  geo=GeoEvidence(sites=[location], sites_field="location",
                                  country="US", country_field="location.country",
                                  work_mode=mode, work_mode_field="location.remote" if mode else ""))


def test_the_production_posting_is_read_as_on_site_and_ruled_out_for_remote_only():
    geo = derive(_raw(PROCESS_ADMIN))
    assert geo.work_mode == "onsite"
    remote_only = GeoPrefs(country="united states", home_location="Irvine, CA",
                           open_to_relocation=True, work_modes=("remote",))
    d = decide(geo, remote_only)
    assert d.status == INELIGIBLE and d.code == "work_mode_not_wanted"
    # …and is the on-site job it always was for someone who takes any style.
    anyone = GeoPrefs(country="united states", home_location="Irvine, CA", open_to_relocation=True)
    assert decide(geo, anyone).status == ELIGIBLE


def test_these_wordings_never_overturn_a_structured_remote():
    """A remote role that asks for some time on site says some of these. They
    fill a mode nothing states; the attendance rule that CAN overturn a
    structured remote keeps its own, stricter list."""
    text = ("This role is listed as remote. Ability to work on-site at our Reno office "
            "is a plus. Working on-site at Reno headquarters is welcome any time.")
    assert attendance_from_description(text)[0] == ""
    assert derive(_raw(text, mode="remote", location="Remote, United States", remote=True)
                  ).work_mode == "remote"


# ── a posting that states no mode at all ─────────────────────────────────────

def _unstated(site="Fountain Valley, CA, United States"):
    return Geography(status="resolved", countries=["united states"], sites=[site], work_mode="")


@pytest.mark.parametrize("modes", [("remote",), ("hybrid",), ("remote", "hybrid")])
def test_no_stated_mode_is_held_for_someone_who_left_on_site_out(modes):
    prefs = GeoPrefs(country="united states", home_location="Irvine, CA",
                     open_to_relocation=True, work_modes=modes)
    d = decide(_unstated(), prefs)
    assert d.status == UNKNOWN and d.code == "work_style_unresolved"
    assert "Fountain Valley" in d.reason and "does not say whether it is on-site or remote" in d.reason
    for m in modes:
        assert m in d.reason


@pytest.mark.parametrize("modes", [(), ("onsite",), ("hybrid", "onsite"), ("remote", "onsite")])
def test_no_stated_mode_costs_nothing_to_someone_who_takes_on_site(modes):
    prefs = GeoPrefs(country="united states", home_location="Irvine, CA",
                     open_to_relocation=True, work_modes=modes)
    assert decide(_unstated(), prefs).status == ELIGIBLE


def test_a_stated_remote_role_is_not_held_by_it():
    prefs = GeoPrefs(country="united states", home_location="Irvine, CA", work_modes=("remote",))
    remote = Geography(status="resolved", countries=["united states"],
                       sites=["Remote", "Houston, Texas, United States"], work_mode="remote")
    assert decide(remote, prefs).status == ELIGIBLE


def test_a_posting_with_no_place_in_it_is_not_held_by_it():
    """"Anywhere" is a way of working, not an office."""
    prefs = GeoPrefs(country="united states", home_location="Irvine, CA", work_modes=("remote",))
    anywhere = Geography(status="resolved", countries=["united states"],
                         sites=["Anywhere"], work_mode="", remote_regions=["united states"])
    assert decide(anywhere, prefs).code != "work_style_unresolved"


def test_the_rules_version_moved_with_the_rule():
    assert RULES_VERSION == 3
