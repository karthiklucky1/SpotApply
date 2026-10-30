"""Hard blockers never reach the board; the scorer knows where the user lives
(founder report, 2026-10-28: "the jobs which are perfect match — I don't think
they are perfect match").

An F-1 OPT profile created that day got 36 jobs in two hours. Two were jobs it
could never hold, both scored 72 and shortlisted:

  * "U.S. Citizenship with eligibility for DoD Secret clearance." — the model
    itself wrote work_auth 10, "requires U.S. Citizenship", and still answered
    overall 72; the contract's "a factor <=15 from a blocker caps overall at 25"
    was never enforced in code.
  * "RSM does not intend to hire entry level candidates who will require
    sponsorship now OR in the future (i.e. F-1 visa holders)."

The refusal detector missed both, and most real phrasings besides: it split
sentences on every period, so "Must be a U.S. citizen" became "Must be a U" /
"S" / "citizen", and its "eligible for sponsorship" canceller vetoed "NOT
eligible for visa sponsorship". On 400 recent postings that mention
sponsorship, citizenship or a clearance it found 32 refusals; it now finds 110,
and none it found before is lost. Every sentence below is from a real posting.

The scorer was also never told the candidate's city or that they would
relocate ("candidate location unclear", "candidate seeks remote" on on-site US
roles), nor today's date ("recent roles dated future" for a 2026 end date).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.common.sponsorship_text import find_refusal
from app.db.models import Job, JobSource, UserProfile
from app.matching import reranker


def _opt_profile(**kw):
    base = dict(user_id="mqb-user", work_auth_status="OPT", work_authorization="F-1 OPT",
                preferred_country="United States", location="Cincinnati, OH",
                open_to_relocation=True, remote_ok=True, years_experience=4,
                current_title="AI Full Stack Developer", key_skills="Python, FastAPI, LangChain",
                target_roles="Backend Engineer, AI Engineer")
    base.update(kw)
    return UserProfile(**base)


# ── the detector ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "U.S. Citizenship with eligibility for DoD Secret clearance.",
    "RSM does not intend to hire entry level candidates who will require sponsorship now OR "
    "in the future (i.e. F-1 visa holders).",
    "Must be a U.S. citizen.",
    "Due to DoD requirements, candidates must be US citizens.",
    "US Citizenship is strictly required.",
    "This position is not eligible for visa sponsorship.",
    "Currently, this position is not eligible for visa sponsorship or STEM OPT extensions.",
    "Please note that visa sponsorship is not available for this position.",
    "We are unable to offer sponsorship, including STEM-OPT and H-1B.",
    "We will not consider candidates who require current or future sponsorship for this position.",
    "If you require sponsorship now, or in the future, your application will not be considered.",
    "Applicants must be currently authorized to work in the United States without the need for "
    "visa sponsorship now or in the future.",
    "Active Secret clearance or higher is required.",
    "**ACTIVE TS/SCI SECURITY CLEARANCE REQUIRED**",
    "Must have the ability to obtain and maintain a security clearance.",
    "Ability to obtain and maintain a DoD Secret Security Clearance.",
    # Qualifiers between the level and "clearance" (CACI, delivered at 82, 2026-10-29).
    "Required: Security Clearance: Willing to be submitted for and able to obtain a TS/SCI "
    "U.S. Government Security Clearance.",
    "Must be able to obtain a Secret level clearance.",
    "Active Top Secret (TS) clearance is required.",
    # Real postings in one F-1 user's pool, 2026-10-25..30 (all missed before).
    "Ability to obtain and maintain US Government TS/SCI Security Clearance.",
    "Willingness and ability to apply for and maintain a TS/SCI security clearance.",
    "Ability to gain and maintain Top Secret security clearance.",
    "Ability to obtain/maintain a DoD Secret security clearance.",
    "Ability to obtain a Top-Secret Security Clearance.",
    "Active Secret U.S. Security Clearance.",
])
def test_real_refusals_and_requirements_are_caught(text):
    found = find_refusal(text)
    assert found is not None, f"missed: {text!r}"
    assert found.sentence


@pytest.mark.parametrize("text", [
    # EEO boilerplate, E-Verify, waivers, preferences, hedges, other people's roles.
    "We do not discriminate on the basis of race, national origin, citizenship status, or veteran status.",
    "U.S. Citizenship and Immigration Services (USCIS) runs E-Verify.",
    "Active Secret clearance preferred.",
    "Nice to have: active TS/SCI clearance.",
    "No security clearance required.",
    "US citizenship is not required.",
    "WSU employs only US citizens and lawfully authorized non-US citizens.",
    "Voyager's careers page states that applicable roles may require US citizenship, lawful "
    "permanent residency, protected-person status, or authorization to obtain the required approvals.",
    "US Citizenship is required for all positions with a government clearance and certain other "
    "restricted positions.",
    "Visa sponsorship is available for eligible candidates.",
    "Candidates are eligible for visa sponsorship.",
    "Must be authorized to work in the U.S. We offer great benefits.",
    "You will be able to obtain a security badge and clearance to the lab floor on day one.",
    "Nice to have: active TS/SCI U.S. Government Security Clearance.",
])
def test_mentions_waivers_and_hedges_are_not_refusals(text):
    assert find_refusal(text) is None, f"false refusal: {text!r}"


# ── the cap, from the posting's words only ──────────────────────────────────

_CREDENCE = ("Experience with AI coding tools.\nU.S. Citizenship with eligibility for DoD Secret "
             "clearance.\nPreferred\nCurious about AI.")
_MODEL_SAID = (72.0, "Strong AI/ML and Python skills; clearance requirement blocks consideration.",
               ["Requires U.S. Citizenship with DoD Secret clearance; candidate is F-1 OPT"],
               {"skills": {"score": 78, "note": "strong"}, "experience": {"score": 75, "note": "ok"},
                "location": {"score": 85, "note": "US"},
                "work_auth": {"score": 10, "note": "F-1 OPT; role requires U.S. Citizenship"}})


def test_a_posting_that_rules_the_user_out_is_capped_at_25():
    score, _reason, concerns, _bd = reranker.reconcile_with_profile(_MODEL_SAID, _opt_profile(), _CREDENCE)
    assert score == reranker.BLOCKER_OVERALL_CAP == 25.0
    assert concerns[0].startswith("Posting rules this out:") and "Citizenship" in concerns[0]


def test_a_citizen_is_not_capped_by_a_sponsorship_rule():
    citizen = _opt_profile(work_auth_status="Citizen", work_authorization="US Citizen")
    assert reranker.reconcile_with_profile(_MODEL_SAID, citizen, _CREDENCE)[0] == 72.0


def test_the_models_own_low_work_auth_never_caps_a_silent_posting():
    silent = "We build AI tools. Python and FastAPI required. Remote in the US."
    assert reranker.reconcile_with_profile(_MODEL_SAID, _opt_profile(), silent)[0] == 72.0


def test_the_rule_filter_drops_it_before_any_paid_call():
    from app.matching.filters.rule_filter import RuleFilter
    job = Job(source=JobSource.WORKABLE, external_id="mqb-credence", company="Credence",
              title="AI Software Engineer", location="Tysons, Virginia, United States",
              url="https://x.co/c", description=_CREDENCE)
    res = RuleFilter(profile=_opt_profile()).filter(job)
    assert res.passed is False and "Sponsorship" in res.reason


# ── what the scorer is told ─────────────────────────────────────────────────

def test_the_scorer_knows_home_relocation_and_today():
    prompt = reranker._profile_system_prompt(_opt_profile())
    assert "Lives in Cincinnati, OH." in prompt
    assert "Open to relocating anywhere in United States" in prompt
    assert f"Today's date is {date.today().isoformat()}" in prompt
    assert "Location check' line in the job is the verified decision" in prompt
    stay = reranker._profile_system_prompt(_opt_profile(open_to_relocation=False))
    assert "Not open to relocating" in stay
    chicago = reranker._profile_system_prompt(_opt_profile(relocation_targets="Chicago, IL"))
    assert "Open to relocating to Chicago, IL" in chicago
