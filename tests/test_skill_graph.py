"""Skill-graph guard rails (CardRace v2 Layer 2, docs/CARDRACE_DESIGN.md §9.2).

Each test pins one of the rules that keep inference honest: multiply-along-path,
max-over-paths (never sum), depth cap, the 0.85 inferred-evidence cap, and
full-credit aliases.
"""
from __future__ import annotations

import pytest

from app.matching import skill_graph as sg
from app.matching.skill_graph import (EMBED_CAP, EMBED_FLOOR, EMBED_FULL, INFERRED_CAP,
                                      MAX_DEPTH, PHRASE_CAP, SkillGraph,
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


# ── semantic route (the missing-information bug) ─────────────────────────────
#
# The three string routes above can only credit a want whose WORDS the candidate
# wrote down. On the 732-row clean subset of the shadow ledger that left g()
# scoring skills 20/100 against Claude's 65/100 and dropping 49% of the jobs
# Claude accepted. The fourth route compares meaning instead — but only against
# résumé CLAIMS, because similarity against the 27 skill TOKENS measured 54.5%,
# below the 57.1% majority-class floor (docs/CARDRACE_DESIGN.md §9.2.3).
#
# These pin that asymmetry, the caps, and — the expensive direction — that a
# fuzzy route cannot quietly become a rubber stamp.


class _FakeEncoder:
    """Deterministic stand-in for MiniLM, so these run without the ML stack (CI
    strips torch/sentence-transformers). Each text is placed on the unit circle
    at ``acos(declared similarity to the want)``, so the cosine the route
    computes is exactly the number the test asked for."""

    def __init__(self, sims: dict):
        self.sims = sims
        self.calls = 0
        self.encoded = []

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):
        import math
        import numpy as np
        self.calls += 1
        self.encoded += list(texts)
        rows = []
        for t in texts:
            # Unknown text -> orthogonal (similarity 0) to everything declared.
            sim = self.sims.get(sg._norm(t), 0.0)
            ang = math.acos(max(-1.0, min(1.0, sim)))
            rows.append([math.cos(ang), math.sin(ang)])
        return np.array(rows, dtype="float32")


@pytest.fixture
def fake_embed(monkeypatch):
    """Installs a controllable encoder and guarantees a clean cache per test —
    the cache is process-wide by design, so leaking it across tests would make
    the call-count assertions depend on file order (the suite runs reversed)."""
    def _install(sims: dict):
        enc = _FakeEncoder({sg._norm(k): v for k, v in sims.items()})
        sg._EMB_CACHE.clear()
        sg._EMB_STATE["unavailable"] = False
        monkeypatch.setattr(sg, "_encoder", lambda: enc)
        return enc
    yield _install
    sg._EMB_CACHE.clear()
    sg._EMB_STATE["unavailable"] = False


WANT = "container orchestration"
CLAIM = "managed kubernetes workloads and ci/cd pipelines on gcp"


def test_semantic_route_needs_claims_and_is_otherwise_inert(fake_embed):
    """No claims -> not one encode, and the answer is the pre-existing one.

    This is what lets every test above keep passing unchanged: `phrases=None`
    is byte-for-byte the old function."""
    enc = fake_embed({WANT: 1.0, CLAIM: 0.9})
    g = SkillGraph({}, {})
    assert g.coverage({"python": 0.9}, WANT)[0] == 0.0
    assert g.coverage({"python": 0.9}, WANT, phrases=None)[0] == 0.0
    assert enc.calls == 0, "semantic route encoded something with no claims"


def test_claim_covers_a_want_with_no_shared_token(fake_embed):
    """The bug, in one assertion: zero string overlap, real evidence."""
    fake_embed({WANT: 1.0, CLAIM: EMBED_FULL})
    g = SkillGraph({}, {})
    assert g.coverage({"python": 0.9}, WANT)[0] == 0.0          # string routes: nothing
    cov, via = g.coverage({"python": 0.9}, WANT, phrases={CLAIM: 0.9})
    assert cov == pytest.approx(0.9 * EMBED_CAP)                # ev x full ramp x cap
    assert via == CLAIM, "the explanation must name the claim that carried it"


def test_semantic_credit_is_ranked_below_the_written_routes():
    """A cosine is the weakest of the four claims, and the constants say so."""
    assert EMBED_CAP < PHRASE_CAP < INFERRED_CAP
    assert 0.0 < EMBED_FLOOR < EMBED_FULL <= 1.0


def test_below_floor_earns_nothing_and_near_floor_earns_almost_nothing(fake_embed):
    """The rubber-stamp guard. A fuzzy route that credits weak similarity turns
    every posting into a match — which is worse than the gap it fixes."""
    fake_embed({WANT: 1.0, "unrelated claim": EMBED_FLOOR - 0.05,
                "near miss claim": 0.40})
    g = SkillGraph({}, {})
    assert g.coverage({}, WANT, phrases={"unrelated claim": 1.0})[0] == 0.0
    near = g.coverage({}, WANT, phrases={"near miss claim": 1.0})[0]
    assert 0.0 < near < 0.15, f"a 0.40 cosine paid {near}, that is proof-grade credit"


def test_semantic_route_can_only_raise_never_lower(fake_embed):
    """Same contract as the phrase tier: it is a max(), so a stated skill is
    never diluted by a weaker semantic match."""
    fake_embed({"kubernetes": 1.0, "vaguely related claim": 0.5})
    g = SkillGraph({}, {})
    ev = {"kubernetes": 0.95}
    direct = g.coverage(ev, "kubernetes")[0]
    with_claims = g.coverage(ev, "kubernetes", phrases={"vaguely related claim": 1.0})
    assert with_claims[0] == pytest.approx(direct) == pytest.approx(0.95)
    assert with_claims[1] == "kubernetes"


def test_semantic_route_respects_the_inference_switch(fake_embed):
    """A cosine hit is assumption by construction, so it must land in `spread`
    (expanded - direct), never in the direct score."""
    fake_embed({WANT: 1.0, CLAIM: EMBED_FULL})
    g = SkillGraph({}, {})
    ph = {CLAIM: 0.9}
    assert g.coverage({}, WANT, use_inference=False, phrases=ph)[0] == 0.0
    assert g.coverage({}, WANT, use_inference=True, phrases=ph)[0] > 0.0


def test_strength_scales_the_credit(fake_embed):
    """A claim from a side project must not count like production ownership."""
    fake_embed({WANT: 1.0, CLAIM: EMBED_FULL})
    g = SkillGraph({}, {})
    weak = g.coverage({}, WANT, phrases={CLAIM: 0.4})[0]
    strong = g.coverage({}, WANT, phrases={CLAIM: 1.0})[0]
    assert weak == pytest.approx(0.4 * EMBED_CAP)
    assert strong == pytest.approx(EMBED_CAP)
    assert weak < strong


def test_prewarm_is_one_batch_and_coverage_then_costs_nothing(fake_embed):
    """coverage() runs per want per capability per pair; an encode inside that
    loop is the difference between one forward pass and dozens."""
    enc = fake_embed({WANT: 1.0, CLAIM: 0.8, "another want": 0.2})
    sg.embed_prewarm([CLAIM, WANT, "another want", CLAIM])
    assert enc.calls == 1
    assert sorted(enc.encoded) == sorted({sg._norm(CLAIM), sg._norm(WANT),
                                          sg._norm("another want")})
    g = SkillGraph({}, {})
    g.coverage({}, WANT, phrases={CLAIM: 0.9})
    g.coverage({}, "another want", phrases={CLAIM: 0.9})
    assert enc.calls == 1, "coverage() re-encoded a string prewarm already had"


def test_absent_model_degrades_to_the_string_routes(monkeypatch):
    """CI has no torch/sentence-transformers, and prod must survive a model that
    fails to load — neither may raise on the scoring path."""
    sg._EMB_CACHE.clear()
    sg._EMB_STATE["unavailable"] = False
    monkeypatch.setattr(sg, "_encoder", lambda: None)
    g = SkillGraph({}, {})
    assert g.coverage({"kubernetes": 0.9}, "kubernetes", phrases={CLAIM: 0.9})[0] == 0.9
    assert g.coverage({}, WANT, phrases={CLAIM: 0.9})[0] == 0.0
    sg._EMB_STATE["unavailable"] = False


def test_setting_switches_the_route_off(fake_embed, monkeypatch):
    enc = fake_embed({WANT: 1.0, CLAIM: EMBED_FULL})
    monkeypatch.setattr(sg.settings, "card_embed_enabled", False)
    g = SkillGraph({}, {})
    assert g.coverage({}, WANT, phrases={CLAIM: 0.9})[0] == 0.0
    assert enc.calls == 0
