"""Ground once per unique claim — not once per bullet, per attempt, forever.

The old check verified every extracted bullet on every attempt, and each
verification re-sent the entire master résumé to answer a five-token question.
A measured audit put that at 64% of a tailored résumé's total cost: 29 calls and
41,987 input tokens for one application, most of it the same résumé over and
over.

Nothing about that was making the résumé safer. A bullet the master already
contains verbatim has not been generated, so there is nothing to fact-check; a
verdict already computed against the same evidence, the same text and the same
verifier cannot have changed. These tests pin the four properties that follow
from that, in the order the work is skipped:

  1. unchanged content         → no calls at all      (L0 / L1)
  2. changed but well-supported → no calls            (paraphrase of its source)
  3. changed and doubtful      → ONE batched call     (L2 / L3)
  4. asked again               → cache, no call

and the safety property that outranks all of them: an unanswered fact-check is
never a pass.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.tailoring import verify_cache
from app.tailoring.grounding import GroundingChecker, _adds_unbacked_metric

MASTER = """# Alex Tenant

## PROFESSIONAL EXPERIENCE
- Built REST APIs with FastAPI serving 2,500 requests per minute.
- Cut p99 latency 45% by tuning PostgreSQL query plans and connection pooling.
- Automated the release pipeline with GitHub Actions and Docker images.
- Instrumented services with structured logging and Prometheus metrics.
"""


@pytest.fixture
def checker(monkeypatch):
    """A checker with no MiniLM — the deterministic matcher, as CI runs it."""
    c = GroundingChecker.__new__(GroundingChecker)
    c.model = None
    verify_cache.clear_local()
    monkeypatch.setattr(settings, "grounding_cache_enabled", False, raising=False)
    return c


def _count_calls(checker, monkeypatch, verdicts=True):
    """Replace the batched verifier with a recorder. Returns the call log."""
    calls: list[list] = []

    def fake_batch(patches, source_md):
        calls.append(list(patches))
        if callable(verdicts):
            return [verdicts(claim) for claim, _src in patches]
        return [verdicts] * len(patches)

    monkeypatch.setattr(checker, "verify_batch", fake_batch)
    return calls


# ── 1. unchanged content costs nothing ───────────────────────────────────────

def test_an_identical_resume_makes_no_llm_call(checker, monkeypatch):
    calls = _count_calls(checker, monkeypatch)
    result = checker.check(MASTER, MASTER)
    assert result.passed is True
    assert result.tier == "L0"
    assert result.llm_calls == 0
    assert calls == [], "verified a résumé that says exactly what the master says"


def test_reordering_and_dropping_bullets_makes_no_llm_call(checker, monkeypatch):
    """L1. Selection and ordering change which claims appear, never what they say."""
    reordered = """# Alex Tenant

## PROFESSIONAL EXPERIENCE
- Instrumented services with structured logging and Prometheus metrics.
- Built REST APIs with FastAPI serving 2,500 requests per minute.
"""
    calls = _count_calls(checker, monkeypatch)
    result = checker.check(MASTER, reordered)
    assert result.passed is True
    assert result.tier == "L1"
    assert result.llm_calls == 0
    assert result.spans_changed == 0
    assert calls == []


def test_emphasis_alone_is_not_a_change(checker, monkeypatch):
    """Bolding a technology changes no words and no facts."""
    bolded = MASTER.replace("FastAPI", "**FastAPI**").replace("Docker", "**Docker**")
    calls = _count_calls(checker, monkeypatch)
    result = checker.check(MASTER, bolded)
    assert result.llm_calls == 0
    assert calls == []


# ── 2 & 3. only changed, doubtful spans are verified — in ONE call ───────────

def test_only_the_changed_spans_reach_the_verifier(checker, monkeypatch):
    """The headline property. Three untouched bullets, one invention: the
    verifier sees the invention and nothing else."""
    tailored = MASTER.replace(
        "- Automated the release pipeline with GitHub Actions and Docker images.",
        "- Led the migration of a 40-node Kubernetes fleet across three regions.",
    )
    calls = _count_calls(checker, monkeypatch, verdicts=False)
    result = checker.check(MASTER, tailored)

    assert len(calls) == 1, "changed spans must be batched into a single request"
    assert len(calls[0]) == 1
    assert "Kubernetes fleet" in calls[0][0][0]
    assert result.spans_changed == 1
    assert result.spans_verified == 1
    assert result.tier == "L2"
    assert result.passed is False


def test_several_changed_spans_are_one_request_not_several(checker, monkeypatch):
    """One call carrying N (claim, source) pairs, not N calls each carrying the
    whole master résumé. This is the 64%."""
    tailored = """# Alex Tenant

## PROFESSIONAL EXPERIENCE
- Invented a novel quantum error-correction scheme for trapped-ion qubits.
- Negotiated a nine-figure vendor contract with a global cloud provider.
- Performed cardiothoracic surgery on a rotating hospital roster.
- Trained a 400-billion-parameter foundation model from scratch.
"""
    calls = _count_calls(checker, monkeypatch, verdicts=False)
    result = checker.check(MASTER, tailored)

    assert len(calls) == 1
    assert len(calls[0]) == 4
    assert result.tier == "L3"
    assert result.llm_calls == 1
    assert len(result.flagged_bullets) == 4


def test_each_claim_carries_its_own_source_not_the_whole_resume(checker, monkeypatch):
    """A patch is judged against the ONE line it was derived from."""
    tailored = MASTER.replace(
        "- Cut p99 latency 45% by tuning PostgreSQL query plans and connection pooling.",
        "- Cut p99 latency 45% by tuning PostgreSQL query plans, lifting conversion 5%.",
    )
    calls = _count_calls(checker, monkeypatch)
    checker.check(MASTER, tailored)
    assert len(calls) == 1
    claim, source = calls[0][0]
    assert "conversion 5%" in claim
    assert "PostgreSQL query plans" in source, (
        "the claim must arrive paired with the source line it was rewritten from"
    )


def test_a_supported_paraphrase_is_not_charged_a_verdict(checker, monkeypatch):
    """A close rewrite of a real bullet, adding no metric, needs no opinion."""
    tailored = MASTER.replace(
        "- Instrumented services with structured logging and Prometheus metrics.",
        "- Instrumented services with structured logging and Prometheus metrics for observability.",
    )
    calls = _count_calls(checker, monkeypatch)
    result = checker.check(MASTER, tailored)
    assert result.spans_changed == 1
    assert result.spans_verified == 0
    assert result.llm_calls == 0
    assert calls == []
    assert result.passed is True


# ── 4. the cache ─────────────────────────────────────────────────────────────

# Cached verdicts are content-addressed and PERSIST across tests in the shared
# dev database, so each cache test works on its own master résumé (a different
# name is a different evidence_id) and deletes only its own rows afterwards.
# Sharing one fixture would make these tests pass or fail on execution order.

@pytest.fixture
def own_evidence(monkeypatch):
    """A per-test master résumé plus a tailored draft with one invention."""
    monkeypatch.setattr(settings, "grounding_cache_enabled", True, raising=False)
    verify_cache.clear_local()
    made: list[str] = []

    def make(tag: str):
        master = MASTER.replace("Alex Tenant", f"Alex Tenant {tag}")
        tailored = master.replace(
            "- Automated the release pipeline with GitHub Actions and Docker images.",
            f"- Led the migration of a 40-node Kubernetes fleet across three regions ({tag}).",
        )
        from app.tailoring.evidence import build_evidence
        eid = build_evidence(master).evidence_id
        made.append(eid)
        verify_cache.invalidate_evidence(eid)
        return master, tailored

    yield make
    for eid in made:
        verify_cache.invalidate_evidence(eid)


def _recording_checker(calls: list, verdict: bool = True):
    c = GroundingChecker.__new__(GroundingChecker)
    c.model = None
    c.verify_batch = lambda patches, src: (calls.append(len(patches))
                                           or [verdict] * len(patches))
    return c


def test_the_same_question_is_not_bought_twice(own_evidence):
    """Same evidence, same generated text, same verifier → the stored verdict.

    This is what makes a rebuild cheap: attempt 2 re-checks the bullets attempt 1
    already cleared, and there is nothing new to learn about them.
    """
    master, tailored = own_evidence("reuse")
    calls: list[int] = []

    r1 = _recording_checker(calls).check(master, tailored)
    assert r1.llm_calls == 1 and r1.cache_hits == 0

    r2 = _recording_checker(calls).check(master, tailored)
    assert r2.llm_calls == 0, "re-asked a question whose answer cannot have changed"
    assert r2.cache_hits == 1
    assert r2.passed is True
    assert calls == [1], "the verifier ran exactly once across both checks"


def test_a_changed_master_resume_invalidates_the_verdict(own_evidence):
    """The key is content, so a new résumé simply has no entries — there is no
    explicit invalidation step anyone can forget to run."""
    master, tailored = own_evidence("invalidate")
    edited_master = master.replace("## PROFESSIONAL EXPERIENCE",
                                   "## PROFESSIONAL EXPERIENCE\n- Ran the on-call rotation.")
    calls: list[int] = []

    _recording_checker(calls).check(master, tailored)
    r2 = _recording_checker(calls).check(edited_master, tailored)
    assert r2.cache_hits == 0, "a verdict from a DIFFERENT résumé was reused"
    assert calls == [1, 1]


def test_manual_recheck_ignores_the_cache(own_evidence):
    """The user's escape hatch: ask again and we actually ask again."""
    master, tailored = own_evidence("recheck")
    calls: list[int] = []

    _recording_checker(calls).check(master, tailored)
    again = _recording_checker(calls).check(master, tailored, use_cache=False)
    assert again.llm_calls == 1
    assert again.cache_hits == 0
    assert calls == [1, 1]


def test_verifier_version_is_part_of_the_key():
    """Change the prompt or the model and every stored answer must stop being
    served — otherwise the cache serves verdicts the current verifier never gave."""
    from app.tailoring import grounding
    assert grounding.VERIFIER_VERSION
    assert settings.scoring_model in grounding._verifier_version()


# ── safety: silence is never a pass ──────────────────────────────────────────

def test_an_unreadable_verdict_is_a_failure_not_a_pass():
    parsed = GroundingChecker._parse_batch_answer("I'm not sure about these.", 2)
    assert parsed == [False, False]


def test_a_partial_answer_only_passes_what_it_actually_answered():
    parsed = GroundingChecker._parse_batch_answer("1: SUPPORTED", 3)
    assert parsed == [True, False, False]


def test_verdicts_are_mapped_by_number_not_by_position():
    parsed = GroundingChecker._parse_batch_answer(
        "2: FABRICATED\n1: SUPPORTED\n3: SUPPORTED", 3)
    assert parsed == [True, False, True]


def test_a_verifier_failure_flags_everything(monkeypatch):
    """No backend, no answer, no pass."""
    c = GroundingChecker.__new__(GroundingChecker)
    c.model = None
    monkeypatch.setattr(settings, "grounding_cache_enabled", False, raising=False)
    monkeypatch.setattr(c, "verify_batch", lambda patches, src: [False] * len(patches))
    tailored = MASTER.replace(
        "- Automated the release pipeline with GitHub Actions and Docker images.",
        "- Invented a novel quantum error-correction scheme for trapped-ion qubits.",
    )
    result = c.check(MASTER, tailored)
    assert result.passed is False


# ── the metric gate, on set membership rather than substring ─────────────────

def test_a_smaller_metric_inside_a_larger_one_still_escalates():
    """'5%' used to read as present because the source said '45%'."""
    src = "Cut p99 latency 45% by tuning query plans."
    bad = "Cut p99 latency 45% by tuning query plans, lifting conversion 5%."
    assert _adds_unbacked_metric(bad, src) is True


def test_reformatting_a_metric_does_not_escalate():
    src = "Served 2,500 requests per minute."
    same = "Served 2500 requests per minute at peak."
    assert _adds_unbacked_metric(same, src) is False
