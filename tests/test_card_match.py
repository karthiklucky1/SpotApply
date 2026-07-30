"""Deterministic matcher g() — CardRace v2 (docs/CARDRACE_DESIGN.md §2.2, §9).

Covers: the four factors, hard-blocker capping, the dual direct/expanded score
(disagreement detector), Layer-4 level-equivalence, low-confidence routing, and
the Priya/PyTorch end-to-end scenario from the design doc.
"""
from __future__ import annotations

import pytest

from app.matching.card_match import (BLOCKER_OVERALL_CAP, CardMatchResult,
                                     match_cards)
from app.matching.skill_graph import SkillGraph

GRAPH = SkillGraph(
    aliases={},
    edges={
        "pytorch": {"ml deployment": 0.7, "deep learning": 0.9},
        "vllm": {"inference optimization": 0.9, "llm serving": 0.95},
        "cuda": {"inference optimization": 0.7},
        "fastapi": {"rest api": 0.9},
    },
)
EMPTY_GRAPH = SkillGraph({}, {})


def user_card(**over) -> dict:
    base = {
        "skills": [{"name": "python", "evidence": 0.95, "basis": "recent-production"},
                   {"name": "fastapi", "evidence": 0.8, "basis": "production"}],
        "years_experience": 4,
        "effective_level": "mid",
        "effective_level_confidence": 0.8,
        "level_rationale": "owned a production API serving 1M users",
        "role_families": ["backend engineer"],
        "domains": ["backend"],
        "_profile": {
            "requires_sponsorship": False,
            "work_authorization": "US Citizen",
            "preferred_country": "united states",
            "remote_ok": True,
            "open_to_relocation": False,
            "location": "Austin, TX",
        },
    }
    base.update(over)
    return base


def job_card(**over) -> dict:
    base = {
        "role_family": "backend engineer",
        "seniority": "mid",
        "years_min": 3, "years_max": 6,
        "capabilities": [
            {"name": "backend development", "importance": 0.9,
             "evidence_needed": ["python", "rest api"]},
        ],
        "nice_to_have": ["docker"],
        "disqualifiers": [],
        "remote_policy": "remote", "remote_scope": "country",
        "country": "united states",
        "visa": "silent",
        "salary_min": None, "salary_max": None,
        "confidence": {"skills": 0.9, "experience": 0.9, "location": 0.9, "visa": 0.9},
    }
    base.update(over)
    return base


def test_deterministic_same_input_same_output():
    a = match_cards(user_card(), job_card(), graph=GRAPH)
    b = match_cards(user_card(), job_card(), graph=GRAPH)
    assert a == b


def test_good_fit_scores_high_everywhere():
    r = match_cards(user_card(), job_card(), graph=GRAPH)
    assert r.expanded >= 75
    assert not r.blockers and not r.low_confidence
    for f in ("skills", "experience", "location", "work_auth"):
        assert f in r.breakdown and "score" in r.breakdown[f] and r.breakdown[f]["note"]


# ── work_auth ────────────────────────────────────────────────────────────────

def test_sponsorship_refused_is_a_blocker_and_caps_overall():
    u = user_card()
    u["_profile"]["requires_sponsorship"] = True
    u["_profile"]["work_authorization"] = "F-1 OPT"
    r = match_cards(u, job_card(visa="no_sponsorship"), graph=GRAPH)
    assert r.breakdown["work_auth"]["score"] <= 15
    assert r.expanded <= BLOCKER_OVERALL_CAP
    assert any("work_auth" in b for b in r.blockers)


def test_sponsorship_silent_is_not_punished():
    u = user_card()
    u["_profile"]["requires_sponsorship"] = True
    r = match_cards(u, job_card(visa="silent"), graph=GRAPH)
    assert r.breakdown["work_auth"]["score"] >= 80
    assert not r.blockers


def test_citizens_only_blocks_non_citizens_even_without_sponsorship_need():
    u = user_card()
    u["_profile"]["work_authorization"] = "Green Card"
    r = match_cards(u, job_card(disqualifiers=["us citizens only"]), graph=GRAPH)
    assert r.breakdown["work_auth"]["score"] <= 15
    assert r.expanded <= BLOCKER_OVERALL_CAP


# "citizens only" and "citizens OR PERMANENT RESIDENTS only" are different legal
# bars, and this pair exists because collapsing them broke once in each
# direction: reading "Green Card" as citizenship admitted PR candidates to real
# ITAR/cleared roles, and reading it as non-citizenship rejected them from the
# far more common postings that explicitly accept permanent residents.
@pytest.mark.parametrize("work_auth", ["Green Card", "Permanent Resident",
                                       "lawful permanent resident"])
def test_permanent_residents_are_eligible_when_the_posting_accepts_them(work_auth):
    u = user_card()
    u["_profile"]["work_authorization"] = work_auth
    r = match_cards(u, job_card(disqualifiers=["us citizens or permanent residents only"]),
                    graph=GRAPH)
    assert r.breakdown["work_auth"]["score"] >= 80, r.breakdown["work_auth"]["note"]
    assert not any("work_auth" in b for b in r.blockers)


@pytest.mark.parametrize("work_auth", ["Green Card", "Permanent Resident"])
def test_permanent_residents_are_still_blocked_by_a_strict_citizens_only_posting(work_auth):
    u = user_card()
    u["_profile"]["work_authorization"] = work_auth
    r = match_cards(u, job_card(disqualifiers=["must be a us citizen"]), graph=GRAPH)
    assert r.breakdown["work_auth"]["score"] <= 15
    assert "citizen" in r.breakdown["work_auth"]["note"].lower()


@pytest.mark.parametrize("work_auth", ["F-1 OPT", "H-1B", "not a us citizen"])
def test_non_citizens_blocked_by_either_phrasing(work_auth):
    """A negated status ("not a us citizen") must never read as citizenship."""
    u = user_card()
    u["_profile"]["work_authorization"] = work_auth
    for disq in (["us citizens only"], ["us citizens or permanent residents only"]):
        r = match_cards(u, job_card(disqualifiers=disq), graph=GRAPH)
        assert r.breakdown["work_auth"]["score"] <= 15, (work_auth, disq)


def test_citizens_only_does_not_punish_an_actual_citizen():
    u = user_card()
    u["_profile"]["work_authorization"] = "US Citizen"
    for disq in (["us citizens only"], ["us citizens or permanent residents only"]):
        r = match_cards(u, job_card(disqualifiers=disq), graph=GRAPH)
        assert r.breakdown["work_auth"]["score"] >= 80, (disq, r.breakdown["work_auth"])


def test_clearance_requirement_blocks():
    r = match_cards(user_card(), job_card(disqualifiers=["security clearance required"]),
                    graph=GRAPH)
    assert r.breakdown["work_auth"]["score"] <= 15


def test_clearance_holder_is_not_blocked_by_a_clearance_requirement():
    u = user_card()
    u["_profile"]["work_authorization"] = "US Citizen"
    u["_profile"]["visa_status"] = "Active Secret clearance"
    r = match_cards(u, job_card(disqualifiers=["security clearance required"]), graph=GRAPH)
    assert r.breakdown["work_auth"]["score"] >= 80, r.breakdown["work_auth"]["note"]


# ── location ─────────────────────────────────────────────────────────────────

def test_other_country_anchored_role_is_a_blocker():
    r = match_cards(user_card(), job_card(country="germany", remote_policy="remote",
                                          remote_scope="country"), graph=GRAPH)
    assert r.breakdown["location"]["score"] <= 15
    assert r.expanded <= BLOCKER_OVERALL_CAP


def test_global_remote_from_another_country_is_fine():
    r = match_cards(user_card(), job_card(country="germany", remote_policy="remote",
                                          remote_scope="global"), graph=GRAPH)
    assert r.breakdown["location"]["score"] >= 80


def test_no_country_preference_means_no_country_gate():
    u = user_card()
    u["_profile"]["preferred_country"] = ""
    r = match_cards(u, job_card(country="germany"), graph=GRAPH)
    assert r.breakdown["location"]["score"] >= 60   # never a silent-US blocker


def test_onsite_in_country_is_a_stretch_not_a_blocker_for_remote_fans():
    r = match_cards(user_card(), job_card(remote_policy="onsite"), graph=GRAPH)
    assert 40 <= r.breakdown["location"]["score"] < 70
    assert not r.blockers


# ── experience (incl. Layer-4 level equivalence) ─────────────────────────────

def test_meeting_years_scores_high():
    r = match_cards(user_card(), job_card(years_min=3), graph=GRAPH)
    assert r.breakdown["experience"]["score"] >= 85


def test_huge_years_gap_is_a_blocker():
    u = user_card(effective_level="mid")          # no senior-equivalent evidence
    r = match_cards(u, job_card(years_min=10, seniority="staff+"), graph=GRAPH)
    assert r.breakdown["experience"]["score"] <= 15
    assert r.expanded <= BLOCKER_OVERALL_CAP
    assert any("experience" in b for b in r.blockers)


def test_level_equivalence_rescues_huge_gap_into_band_territory():
    """Layer 4: the unusual candidate (2y, built a 1M-user system, judged
    staff-equivalent) must NOT be silently arithmetic-rejected — the score lifts
    out of blocker range so the pair can reach Claude via the band."""
    u = user_card(effective_level="staff+", effective_level_confidence=0.9)
    r = match_cards(u, job_card(years_min=10, seniority="staff+"), graph=GRAPH)
    assert 25 <= r.breakdown["experience"]["score"] <= 45   # weak, not blocked
    assert not any("experience" in b for b in r.blockers)


def test_level_equivalence_softens_small_gap():
    # 4y candidate vs 6y ask (gap 2). Without level evidence: visible stretch.
    weak = user_card(effective_level="junior", effective_level_confidence=0.9)
    strong = user_card(effective_level="senior", effective_level_confidence=0.9)
    j = job_card(years_min=6, seniority="senior")
    r_weak = match_cards(weak, j, graph=GRAPH)
    r_strong = match_cards(strong, j, graph=GRAPH)
    assert r_strong.breakdown["experience"]["score"] > r_weak.breakdown["experience"]["score"]
    # but never past the honest ceiling for a formal gap
    assert r_strong.breakdown["experience"]["score"] <= 85


def test_seniority_ladder_when_years_absent():
    j = job_card(years_min=None, seniority="senior")
    r_mid = match_cards(user_card(effective_level="mid"), j, graph=GRAPH)
    r_sen = match_cards(user_card(effective_level="senior"), j, graph=GRAPH)
    assert r_sen.breakdown["experience"]["score"] > r_mid.breakdown["experience"]["score"]


# ── skills + the disagreement detector ───────────────────────────────────────

def test_priya_pytorch_scenario_direct_vs_expanded():
    """The design doc's worked example: JD wants inference optimization; the
    candidate never says those words but ships vLLM/CUDA. Direct scoring misses
    it; graph inference finds it; the spread flags it for Claude."""
    priya = user_card(skills=[
        {"name": "python", "evidence": 0.95},
        {"name": "pytorch", "evidence": 0.9},
        {"name": "cuda", "evidence": 0.85},
        {"name": "vllm", "evidence": 0.9},
    ])
    j = job_card(capabilities=[
        {"name": "python", "importance": 0.9, "evidence_needed": []},
        {"name": "inference optimization", "importance": 0.9,
         "evidence_needed": ["ml deployment"]},
    ])
    r = match_cards(priya, j, graph=GRAPH)
    assert r.expanded > r.direct                    # inference found hidden ability
    assert r.spread == pytest.approx(r.expanded - r.direct, abs=0.11)
    assert r.spread > 5                             # enough to matter for routing
    # explanation surface: the trace names the carrying skill
    assert "vllm" in r.breakdown["skills"]["note"] or "cuda" in r.breakdown["skills"]["note"]


def test_plain_backend_candidate_has_tiny_spread():
    r = match_cards(user_card(), job_card(), graph=GRAPH)
    assert abs(r.spread) <= 5                       # nothing assumed → auto-safe


def test_no_skills_on_card_scores_low_but_judged():
    r = match_cards(user_card(skills=[]), job_card(), graph=GRAPH)
    assert r.breakdown["skills"]["score"] <= 25
    assert "skills" not in r.low_confidence


def test_empty_capabilities_marks_skills_low_confidence():
    r = match_cards(user_card(), job_card(capabilities=[]), graph=GRAPH)
    assert "skills" in r.low_confidence


def test_low_confidence_job_fields_are_routed():
    j = job_card(confidence={"skills": 0.9, "experience": 0.9,
                             "location": 0.3, "visa": 0.9})
    r = match_cards(user_card(), j, graph=GRAPH)
    assert "location" in r.low_confidence


def test_nice_to_have_bonus_is_bounded():
    have_all = user_card(skills=[
        {"name": "python", "evidence": 0.95}, {"name": "rest api", "evidence": 0.9},
        {"name": "docker", "evidence": 0.9}])
    j = job_card(nice_to_have=["docker"])
    with_bonus = match_cards(have_all, j, graph=GRAPH)
    without = match_cards(have_all, job_card(nice_to_have=[]), graph=GRAPH)
    diff = with_bonus.breakdown["skills"]["score"] - without.breakdown["skills"]["score"]
    assert 0 <= diff <= 9.01


def test_result_shape_matches_reranker_contract():
    """rerank_breakdown consumers expect the exact four keys with score+note."""
    r = match_cards(user_card(), job_card(), graph=GRAPH)
    assert isinstance(r, CardMatchResult)
    assert set(r.breakdown) == {"skills", "experience", "location", "work_auth"}
    for v in r.breakdown.values():
        assert set(v) == {"score", "note"}
        assert 0 <= v["score"] <= 100
