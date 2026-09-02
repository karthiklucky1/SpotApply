"""The deterministic half of the anti-fabrication promise.

Grounding is a model asking a model whether a sentence is supported. It is good
at semantics and it is the reason "GPU clusters" got caught — but it is a
judgement, it costs money, and it only looks at lines it recognises as
achievement bullets. A fabricated employer sitting in an experience header, or a
degree appended to the Education section, is not a bullet and never reaches it.

This layer needs no model and cannot itself hallucinate: extract the facts from
the input, extract the facts from the output, and anything in the output that is
not in the input was invented. Set difference in the ADDITION direction only —
a tailored résumé that DROPS a fact has made an editing choice; one that GAINS
a fact has made something up.

Each test below names a category a real résumé gets rejected (or an offer
rescinded) for asserting falsely.
"""
from __future__ import annotations

import pytest

from app.tailoring.evidence import (
    build_evidence,
    extract_facts,
    fabrication_violations,
    normalize_text,
    patch_hash,
)

MASTER = """# Alex Tenant
Cincinnati, OH | alex@example.com

## PROFESSIONAL SUMMARY
- Backend engineer with 3 years building Python services.

## PROFESSIONAL EXPERIENCE
**Backend Engineer** | Acme Corp | May 2022 - Aug 2024 | Remote
- Built REST APIs with FastAPI serving 2,500 requests per minute.
- Cut p99 latency 45% by tuning PostgreSQL query plans.

## EDUCATION
**Master of Science** | Aug 2024
Ohio State University, Columbus, OH
"""


def _kinds(violations):
    return {kind for kind, _value in violations}


def test_an_untouched_resume_has_nothing_added():
    assert fabrication_violations(MASTER, MASTER) == []


def test_reordering_and_rewording_is_not_a_fabrication():
    """The tailor's actual job must not trip the guard.

    Emphasis, ordering and phrasing all change during a normal tailoring pass.
    A guard that fires on those is a guard that gets switched off.
    """
    reworded = MASTER.replace(
        "Built REST APIs with FastAPI serving 2,500 requests per minute.",
        "Designed and shipped **FastAPI** REST services handling 2,500 requests per minute.",
    )
    assert fabrication_violations(MASTER, reworded) == []


def test_dropping_a_bullet_is_an_editing_choice_not_a_fabrication():
    trimmed = MASTER.replace("- Cut p99 latency 45% by tuning PostgreSQL query plans.\n", "")
    assert fabrication_violations(MASTER, trimmed) == []


# ── the seven categories ─────────────────────────────────────────────────────

def test_an_invented_employer_is_caught():
    bad = MASTER.replace("| Acme Corp |", "| Stripe |")
    assert "employer" in _kinds(fabrication_violations(MASTER, bad))


def test_an_inflated_job_title_is_caught():
    """The quiet one. Nobody checks a résumé's adjectives; everybody checks the
    title against the reference."""
    bad = MASTER.replace("**Backend Engineer**", "**Principal Backend Engineer**")
    assert "job title" in _kinds(fabrication_violations(MASTER, bad))


def test_a_stretched_employment_date_is_caught():
    bad = MASTER.replace("May 2022 - Aug 2024", "May 2020 - Aug 2024")
    assert "employment date" in _kinds(fabrication_violations(MASTER, bad))


def test_an_invented_degree_and_institution_are_caught():
    bad = MASTER + "\n**Doctor of Philosophy** | May 2021\nStanford University\n"
    kinds = _kinds(fabrication_violations(MASTER, bad))
    assert "degree" in kinds
    assert "institution" in kinds


def test_an_invented_certification_is_caught():
    bad = MASTER + "\n## CERTIFICATIONS\n- AWS Certified Solutions Architect\n"
    assert "certification" in _kinds(fabrication_violations(MASTER, bad))


def test_an_invented_number_is_caught():
    bad = MASTER.replace(
        "serving 2,500 requests per minute",
        "serving 12,000 requests per minute",
    )
    assert "number" in _kinds(fabrication_violations(MASTER, bad))


def test_a_smaller_number_hiding_inside_a_real_one_is_caught():
    """THE bug this replaces.

    Comparison used to be `token not in source_text`, so '5%' read as present
    because the source said '45%'. Every containing number hid a smaller
    invented one — '2%' inside '12%', '1.5x' inside '11.5x' — and the fabricated
    figure was never even escalated to a fact-check.
    """
    bad = MASTER.replace(
        "Cut p99 latency 45% by tuning PostgreSQL query plans.",
        "Cut p99 latency 45% by tuning PostgreSQL query plans, lifting conversion 5%.",
    )
    violations = fabrication_violations(MASTER, bad)
    assert ("number", "5") in violations, violations


def test_reformatting_a_number_is_not_a_new_number():
    """'2,500' and '2500' are one fact. A guard that cannot tell formatting from
    fabrication blocks honest résumés, which is worse than useless."""
    same = MASTER.replace("2,500 requests", "2500 requests")
    assert fabrication_violations(MASTER, same) == []


# ── evidence identity: what makes the verdict cache safe ─────────────────────

def test_evidence_id_is_stable_under_formatting_and_changes_with_content():
    a = build_evidence(MASTER).evidence_id
    b = build_evidence(MASTER.replace("FastAPI", "**FastAPI**")).evidence_id
    c = build_evidence(MASTER.replace("Acme Corp", "Globex")).evidence_id
    assert a == b, "emphasis is not evidence — the same facts must key the same"
    assert a != c, "a different employer MUST NOT reuse the old résumé's verdicts"


def test_patch_hash_binds_the_claim_to_the_evidence_it_was_judged_against():
    """A verdict is a statement about a PAIR. The same sentence can be supported
    by one source line and fabricated against another, so the source has to be
    in the key or the cache will serve the wrong answer."""
    claim = "Cut p99 latency 45% by tuning PostgreSQL query plans."
    assert patch_hash(claim, "span-a") != patch_hash(claim, "span-b")
    assert patch_hash(claim, "span-a") == patch_hash(claim, "span-a")
    # …and formatting-only differences are the same claim.
    assert patch_hash(claim, "s") == patch_hash(f"**{claim}**", "s")


def test_evidence_spans_exclude_structure():
    """Headers, employer lines and date ranges carry facts, not claims. Sending
    them to a fact-checker only manufactures failures — a date range cannot be
    'supported' by a bullet about building an API."""
    spans = [s.text for s in build_evidence(MASTER).spans]
    joined = " | ".join(spans)
    assert "Built REST APIs" in joined
    assert "Backend Engineer" not in joined
    assert "Ohio State University" not in joined


@pytest.mark.parametrize("text,expected", [
    ("**FastAPI**  services", "fastapi services"),
    ("  Mixed   CASE\t", "mixed case"),
])
def test_normalization_is_formatting_insensitive(text, expected):
    assert normalize_text(text) == expected


def test_facts_survive_a_resume_with_no_recognizable_structure():
    """A plain-text résumé (PDF extraction) must not produce phantom facts.

    Over-extraction here is worse than under-extraction: a phantom 'employer'
    in the output that the master's parse missed would block a truthful résumé.
    """
    facts = extract_facts("just some free text with no headings at all")
    assert facts.employers == frozenset()
    assert facts.dates == frozenset()
    assert facts.degrees == frozenset()
