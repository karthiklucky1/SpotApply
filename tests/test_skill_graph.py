"""Skill-graph guard rails (CardRace v2 Layer 2, docs/CARDRACE_DESIGN.md §9.2).

Each test pins one of the rules that keep inference honest: multiply-along-path,
max-over-paths (never sum), depth cap, the 0.85 inferred-evidence cap, and
full-credit aliases.
"""
from __future__ import annotations

import pytest

from app.matching.skill_graph import (INFERRED_CAP, MAX_DEPTH, PHRASE_CAP, SkillGraph,
                                      _content_tokens, load_graph)


@pytest.fixture
def graph() -> SkillGraph:
    return SkillGraph(
        aliases={"k8s": "kubernetes"},
        edges={
            "pytorch": {"ml deployment": 0.7, "deep learning": 0.9},
            "deep learning": {"machine learning": 0.9},
            "machine learning": {"data science": 0.75},
            "vllm": {"inference optimization": 0.9},
            "kubernetes": {"devops": 0.8},
        },
        version=1,
    )


def test_exact_and_alias_full_credit(graph):
    assert graph.infer_strength("pytorch", "pytorch") == 1.0
    assert graph.infer_strength("k8s", "kubernetes") == 1.0
    cov, via = graph.coverage({"k8s": 0.9}, "kubernetes")
    assert cov == 0.9 and via == "k8s"          # aliases are spelling, not inference


def test_single_edge_strength(graph):
    assert graph.infer_strength("pytorch", "ml deployment") == pytest.approx(0.7)


def test_path_strengths_multiply(graph):
    # pytorch -> deep learning (0.9) -> machine learning (0.9) = 0.81
    assert graph.infer_strength("pytorch", "machine learning") == pytest.approx(0.81)


def test_depth_cap_blocks_long_chains(graph):
    # pytorch -> deep learning -> machine learning -> data science needs 3 hops
    assert MAX_DEPTH == 2
    assert graph.infer_strength("pytorch", "data science") == 0.0


def test_no_path_is_zero(graph):
    assert graph.infer_strength("vllm", "devops") == 0.0


def test_inferred_coverage_capped(graph):
    # Full evidence (1.0) via a 0.95-strength edge would give 0.95 — the cap
    # holds it at INFERRED_CAP: inference never fully satisfies a must-have.
    g = SkillGraph({}, {"vllm": {"inference optimization": 0.95}})
    cov, via = g.coverage({"vllm": 1.0}, "inference optimization")
    assert cov == pytest.approx(INFERRED_CAP)
    assert via == "vllm"


def test_direct_evidence_beats_weak_inference(graph):
    ev = {"ml deployment": 0.6, "pytorch": 1.0}
    cov, via = graph.coverage(ev, "ml deployment")
    # direct 0.6 vs inferred min(1.0*0.7, 0.85)=0.7 → inference wins here,
    # but with strong direct evidence direct wins:
    assert cov == pytest.approx(0.7) and via == "pytorch"
    ev["ml deployment"] = 0.9
    cov, via = graph.coverage(ev, "ml deployment")
    assert cov == pytest.approx(0.9) and via == "ml deployment"


def test_best_path_wins_not_sum(graph):
    # Two independent hints must NOT stack above the single best path.
    g = SkillGraph({}, {
        "a": {"target": 0.5},
        "b": {"target": 0.6},
    })
    cov, via = g.coverage({"a": 1.0, "b": 1.0}, "target")
    assert cov == pytest.approx(0.6)            # max, not 1.1
    assert via == "b"


def test_inference_off_means_direct_only(graph):
    cov, _ = graph.coverage({"pytorch": 1.0}, "ml deployment", use_inference=False)
    assert cov == 0.0


def test_seed_graph_file_loads_and_is_sane():
    g = load_graph("data/skill_graph.json")
    assert g.version >= 1
    # the PyTorch problem, on the real shipped graph:
    assert g.infer_strength("pytorch", "ml deployment") > 0.5
    assert g.infer_strength("vllm", "inference optimization") > 0.8
    # every edge strength is a valid probability-like weight
    for src, tos in g.edges.items():
        for dst, w in tos.items():
            assert 0.0 < w <= 1.0, f"bad strength {src}->{dst}={w}"


# ── phrase decomposition (the vocabulary mismatch) ───────────────────────────
#
# The JobCard mint is prompted for success-profile PHRASES ("LLM application
# development"); UserCard skills arrive as TOKENS ("langchain"). Whole-phrase
# equality can never bridge those, and when it was the only route the shadow
# ledger measured g() dropping 37.2% of the jobs Claude accepted. These pin the
# bridge — and the limits that keep it from becoming a rubber stamp.


def test_multiword_capability_resolves_through_its_head_term():
    """The class of bug: a phrase whose skill-bearing token IS in the graph."""
    g = load_graph("data/skill_graph.json")
    for want in ("LLM application development", "Production ML systems",
                 "Python backend engineering"):
        cov, via = g.coverage(
            {"langchain": 0.85, "python": 0.95, "fastapi": 0.85,
             "machine learning": 0.75}, want)
        assert cov > 0.0, f"{want!r} scored zero — phrase tier is not reaching the graph"
        assert via is not None


def test_phrase_credit_never_beats_a_stated_skill(graph):
    """A partial phrase match must stay below direct evidence, always."""
    ev = {"kubernetes": 1.0}
    direct, _ = graph.coverage(ev, "kubernetes")
    phrase, _ = graph.coverage(ev, "kubernetes platform engineering")
    assert phrase < direct
    assert phrase <= PHRASE_CAP


def test_phrase_tier_is_a_floor_not_a_discount(graph):
    """Adding the tier can only raise coverage — an exact hit is untouched."""
    assert graph.coverage({"pytorch": 0.8}, "pytorch")[0] == pytest.approx(0.8)
    assert graph.coverage({"pytorch": 0.8}, "ml deployment")[0] == pytest.approx(0.56)


def test_unmet_capability_still_scores_zero():
    """The fix must not turn every phrase into a pass — real gaps stay gaps."""
    g = load_graph("data/skill_graph.json")
    ev = {"python": 0.95, "fastapi": 0.85, "langchain": 0.85}
    for want in ("mlops", "model deployment", "kotlin android development"):
        cov, _ = g.coverage(ev, want)
        assert cov == 0.0, f"{want!r} scored {cov} on a candidate with no such evidence"


def test_filler_only_phrase_falls_back_to_raw_tokens():
    """_content_tokens must never return nothing — an all-filler phrase would
    otherwise divide by zero or silently score 0."""
    assert _content_tokens("best practices") == ["best", "practices"]
    assert _content_tokens("Production ML Systems") == ["ml"]
    assert _content_tokens("") == []
    g = load_graph("data/skill_graph.json")
    assert g.coverage({"python": 1.0}, "best practices")[0] == 0.0   # no crash


def test_token_inside_a_held_multiword_skill_counts(graph):
    """Reverse direction: the wanted token sits inside a skill the user stated."""
    cov, via = graph.coverage({"prometheus monitoring stack": 0.9},
                              "monitoring and alerting")
    assert cov > 0.0 and via == "prometheus monitoring stack"


def test_phrase_tier_respects_the_inference_switch():
    """direct vs expanded must still differ — that difference IS `spread`."""
    g = load_graph("data/skill_graph.json")
    ev = {"fastapi": 0.85}
    direct, _ = g.coverage(ev, "Python backend engineering", use_inference=False)
    expanded, _ = g.coverage(ev, "Python backend engineering", use_inference=True)
    assert expanded > direct, "phrase tier is ignoring use_inference=False"
