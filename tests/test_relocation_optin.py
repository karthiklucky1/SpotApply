"""The relocation line is opt-in, U.S.-only, and never moves the candidate.

The failure this guards against is a document that misstates where someone
lives. "Open to relocation to Chicago, IL" is a statement of willingness; a
header that says Chicago for a person who lives in Cincinnati is a false fact on
a résumé an employer relies on, and no amount of relocation willingness makes it
true.

FOUR SEPARATE FACTS, and the tests keep them apart:

    where they live now      UserProfile.location             never rewritten
    will they move at all    UserProfile.open_to_relocation   a SEARCH filter
    may a résumé say so      UserProfile.relocation_resume_optin
    where, and by when       relocation_targets / relocation_timeline

The third does not follow from the second. Consent to be SHOWN an out-of-town
job is not consent to make a claim on a document, so the new flag defaults off
for everyone — including users already open to relocation — and nothing
backfills it.
"""
from __future__ import annotations

import pytest

from app.common.eligibility import Geography
from app.tailoring.relocation import (NATIONWIDE, offer_for, parse_targets,
                                      prompt_block)


class _Profile:
    """Only the fields the offer reads."""
    def __init__(self, optin=True, open_to_relocation=True, targets="nationwide",
                 timeline="", location="Cincinnati, OH"):
        self.relocation_resume_optin = optin
        self.open_to_relocation = open_to_relocation
        self.relocation_targets = targets
        self.relocation_timeline = timeline
        self.location = location


def _geo(sites, countries=("united states",), work_mode=""):
    return Geography(status="resolved", countries=list(countries), sites=list(sites),
                     work_mode=work_mode, remote_regions=[], areas=[], conflicts=[])


# ── consent ──────────────────────────────────────────────────────────────────

def test_the_line_is_off_unless_the_user_opted_in():
    off = offer_for(_Profile(optin=False), _geo(["Chicago, IL"]))
    assert not off and off.reason == "optin_off"


def test_opting_in_does_not_follow_from_the_search_preference():
    """THE MIGRATION RULE. A user who is already `open_to_relocation` has told
    us which jobs to show them. That is not permission to print a claim, so the
    new flag stays off until they set it."""
    p = _Profile(optin=False, open_to_relocation=True)
    assert not offer_for(p, _geo(["Chicago, IL"]))


def test_a_user_who_will_not_move_gets_no_line_even_if_opted_in():
    """Otherwise the document would contradict the profile."""
    off = offer_for(_Profile(open_to_relocation=False), _geo(["Chicago, IL"]))
    assert not off and off.reason == "not_open_to_relocation"


def test_the_model_default_is_off():
    from app.db.models import UserProfile
    assert UserProfile().relocation_resume_optin is False
    assert UserProfile().relocation_targets == ""
    assert UserProfile().relocation_timeline == ""


# ── specificity comes from the posting ───────────────────────────────────────

def test_a_city_posting_yields_a_city_line():
    off = offer_for(_Profile(), _geo(["Chicago, IL"]))
    assert off.line == "Open to relocation to Chicago, IL"
    assert off.destination == "Chicago, IL"


def test_a_state_only_posting_yields_a_state_line():
    """We never promote a state to a city — there is no city to name."""
    off = offer_for(_Profile(), _geo(["Kentucky"]))
    assert off.line == "Open to relocation to Kentucky"
    assert "," not in off.destination


def test_the_timeline_is_shown_as_written_when_set():
    off = offer_for(_Profile(timeline="within 4 weeks"), _geo(["Austin, TX"]))
    assert off.line == "Open to relocation to Austin, TX (within 4 weeks)"


# ── which destinations are approved ──────────────────────────────────────────

def test_nationwide_approves_any_us_destination():
    for site in ("Chicago, IL", "Austin, TX", "Kentucky", "New York, NY"):
        assert offer_for(_Profile(targets="nationwide"), _geo([site]))


def test_a_state_target_approves_cities_in_that_state():
    p = _Profile(targets="Illinois, Texas")
    assert offer_for(p, _geo(["Chicago, IL"])).destination == "Chicago, IL"
    assert offer_for(p, _geo(["Austin, TX"])).destination == "Austin, TX"


def test_a_destination_outside_the_approved_list_gets_no_line():
    off = offer_for(_Profile(targets="Illinois"), _geo(["Austin, TX"]))
    assert not off and off.reason == "destination_not_approved"


def test_a_city_target_does_not_approve_the_whole_state():
    p = _Profile(targets="Chicago, IL")
    assert offer_for(p, _geo(["Chicago, IL"]))
    assert not offer_for(p, _geo(["Springfield, IL"]))


def test_no_approved_destinations_set_means_no_line():
    off = offer_for(_Profile(targets=""), _geo(["Chicago, IL"]))
    assert not off and off.reason == "no_approved_destinations_set"


@pytest.mark.parametrize("raw,expected", [
    ("nationwide", [NATIONWIDE]),
    ("Anywhere", [NATIONWIDE]),
    # "City, ST" stays ONE target. Splitting on commas alone would turn a
    # request for one city into a target for its whole state, because a bare
    # state target approves every city in it.
    ("Chicago, IL\nAustin, TX", ["chicago, il", "austin, tx"]),
    ("Chicago, IL, Austin, TX", ["chicago, il", "austin, tx"]),
    ("Illinois, Chicago, IL", ["illinois", "chicago, il"]),
    (["Illinois", "Texas"], ["illinois", "texas"]),
    ("", []),
    # Nationwide anywhere in the list short-circuits the rest.
    ("Illinois, nationwide, Texas", [NATIONWIDE]),
])
def test_targets_parse(raw, expected):
    assert parse_targets(raw) == expected


# ── the cases that must produce nothing ──────────────────────────────────────

def test_a_non_us_posting_never_gets_a_line():
    """The wording, the destination list and the timeline are all framed around
    a domestic move, and a work-authorisation question sits behind an
    international one."""
    off = offer_for(_Profile(), _geo(["Berlin"], countries=("germany",)))
    assert not off and off.reason == "not_a_us_posting"


def test_a_remote_posting_with_no_office_gets_no_line():
    """There is nothing to relocate TO."""
    off = offer_for(_Profile(), _geo(["Remote"], work_mode="remote"))
    assert not off and off.reason == "no_us_destination_in_posting"
    assert not offer_for(_Profile(), _geo(["Remote, USA"], work_mode="remote"))


def test_a_posting_with_no_location_gets_no_line():
    assert not offer_for(_Profile(), _geo([]))
    assert not offer_for(_Profile(), None)


def test_a_multi_site_posting_uses_the_first_approved_site():
    off = offer_for(_Profile(targets="Texas"),
                    _geo(["Chicago, IL", "Austin, TX", "Remote"]))
    assert off.destination == "Austin, TX"


def test_a_posting_whose_country_is_ambiguous_gets_no_line():
    """Two countries on one posting is not a verified U.S. destination."""
    assert not offer_for(_Profile(),
                         _geo(["Toronto", "Austin, TX"],
                              countries=("canada", "united states")))


# ── the instruction handed to the generator ──────────────────────────────────

def test_the_prompt_block_forbids_moving_the_candidate():
    block = prompt_block(offer_for(_Profile(), _geo(["Chicago, IL"])))
    assert "Open to relocation to Chicago, IL" in block
    # The instruction has to say this outright; it is the whole risk.
    assert "do NOT change the candidate's current city" in block
    assert "still live where the master" in block
    assert "Do not reword" in block


def test_no_offer_means_no_prompt_text_at_all():
    assert prompt_block(offer_for(_Profile(optin=False), _geo(["Chicago, IL"]))) == ""
    assert prompt_block(None) == ""


def test_the_tailor_accepts_and_forwards_the_block():
    """Guards the wiring, not just the helper: the block has to reach the
    prompt, and only the prompt."""
    import inspect

    from app.tailoring.tailor import Tailor, tailor_for_application
    assert "relocation_block" in inspect.signature(Tailor.tailor_resume).parameters
    src = inspect.getsource(Tailor.tailor_resume)
    assert "{relocation_block}" in src, "the block must be interpolated into the prompt"
    caller = inspect.getsource(tailor_for_application)
    assert "relocation_block=relocation_block" in caller
    assert "relocation_resume_optin" in caller, \
        "the opt-in must gate the work, not just the output"
