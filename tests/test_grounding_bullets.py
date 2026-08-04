"""Grounding must check achievement claims, not résumé structure.

Found in production on 2026-07-30. A live tailor run failed grounding on two
"bullets":

    "Home Depot Cincinnati, OH"     — an employer/location header
    "May 2022 – Aug 2024"           — a date range

Both sit inside the EXPERIENCE section, and the extractor's only filter was
``len(cleaned) >= 12 and " " in cleaned`` — whose comment claimed it "skips
dates/company headers/one-word lines" while doing nothing of the sort. So they
were compared against source bullets, could not be semantically supported by any
of them, were escalated to LLM verification, and failed the whole résumé to
ERROR.

That is a false positive that BLOCKS a real user's application, which is itself a
quality failure. Excluding non-bullets makes the check more accurate, not more
permissive: a date range is not a claim that can be hallucinated, and the real
credential facts (degree, school, employment dates) are protected structurally by
tailoring/lock.py, which restores them verbatim from the master before grounding
runs at all.

These tests exist so the filter can never be loosened back into flagging
structure, nor tightened into dropping genuine bullets.
"""
from __future__ import annotations

import pytest

pytest.importorskip("sentence_transformers", reason="grounding imports it at module scope")

from app.tailoring.grounding import _is_not_a_bullet  # noqa: E402


# ── structure: must never reach the grounding check ──────────────────────────

@pytest.mark.parametrize("line", [
    # The two that actually failed in production.
    "Home Depot Cincinnati, OH",
    "May 2022 – Aug 2024",
    # Employer / location headers.
    "Acme Corp, San Francisco, CA",
    "Globex, Remote",
    "Initech, Hybrid",
    "Contoso Ltd, Austin, TX",
    # Date ranges, in the shapes résumés actually use.
    "2020 - Present",
    "Jan 2020 — Dec 2021",
    "June 2018 – July 2020",
    "06/2019 - 08/2021",
    "March 2021 to Present",
    "Sept 2017 through Aug 2019",
    # Standalone job titles.
    "Software Engineer",
    "Senior Data Analyst",
    "Machine Learning Intern",
])
def test_structure_lines_are_not_treated_as_bullets(line):
    assert _is_not_a_bullet(line) is True, (
        f"{line!r} would be grounded as an achievement claim; it cannot be "
        f"supported by any source bullet, so it fails the résumé to ERROR")


# ── real bullets: must always reach the grounding check ──────────────────────

@pytest.mark.parametrize("line", [
    "Built internal tooling that cut manual triage time",
    "Conducted interviews with teams to identify manual processes",
    "Led migration of the billing service to Postgres",
    "Reduced p99 latency by 43% across the ingestion pipeline",
    "Designed and shipped a vLLM inference service serving 2,500 req/min",
    "Owned the on-call rotation for four production services",
    "Mentored three junior engineers through their first launches",
    "Scaled Kafka consumers to 12M events per day",
    "Automated ATS ingestion, saving 20 hours weekly",
    "Improved model accuracy 22% using contrastive fine-tuning",
    # A bullet that legitimately ends in a sentence and mentions a location.
    "Coordinated the launch across three offices in Austin, TX.",
])
def test_real_bullets_still_get_checked(line):
    assert _is_not_a_bullet(line) is False, (
        f"{line!r} is a genuine achievement claim — excluding it silently "
        f"weakens the anti-hallucination check")


# ── end to end through the extractor ─────────────────────────────────────────

_RESUME = """## EXPERIENCE

Home Depot Cincinnati, OH
Software Engineer
May 2022 – Aug 2024
- Built internal tooling that cut manual triage time
- Conducted interviews with teams to identify manual processes

Acme Corp, San Francisco, CA
2020 - Present
- Led migration of the billing service to Postgres
"""


def _extract(md: str):
    from app.tailoring.grounding import GroundingChecker
    # __init__ loads the shared MiniLM; the extractor itself is pure Python.
    return GroundingChecker.__new__(GroundingChecker)._extract_bullets(md)


def test_the_extractor_returns_only_the_three_real_bullets():
    assert _extract(_RESUME) == [
        "Built internal tooling that cut manual triage time",
        "Conducted interviews with teams to identify manual processes",
        "Led migration of the billing service to Postgres",
    ]


def test_the_extractor_still_finds_bullets_at_all():
    """The filter must not be so aggressive that grounding silently no-ops —
    an empty bullet list makes check() pass everything."""
    assert len(_extract(_RESUME)) == 3


def test_the_messy_pdf_fallback_filters_structure_too():
    """The no-recognizable-headers path is a separate branch, and it would
    otherwise reintroduce the same bug for PDF-extracted résumés."""
    messy = (
        "Home Depot Cincinnati, OH\n"
        "May 2022 – Aug 2024\n"
        "Built internal tooling that cut manual triage time for the whole team\n"
        "Led the migration of the billing service onto managed Postgres\n"
    )
    out = _extract(messy)
    assert out, "fallback found nothing — grounding would pass blindly"
    assert all(not _is_not_a_bullet(b) for b in out)
    assert "Home Depot Cincinnati, OH" not in out
    assert "May 2022 – Aug 2024" not in out


# ── skills-section labels and bullet-prefixed headings ───────────────────────
# The nine-deep ERROR backlog (2026-08-01) contained 26 flagged lines. Only one
# was a date range; the pattern that actually blocks applications 1358, 1359 and
# 1284 is skills-section labels the section detector missed — not ALL-CAPS, no
# known section keyword — so they were graded as experience bullets.

@pytest.mark.parametrize("line", [
    "Familiar / Actively Adopting:",          # real, from application 1358
    "Systems & Infrastructure:",              # real
    "Generative AI & LLM Engineering:",       # real
    "Core Competencies:",
    "Tools:",
])
def test_list_labels_are_not_bullets(line):
    """A label introducing a list is not a claim. A real achievement bullet is a
    sentence, and does not end in a colon."""
    assert _is_not_a_bullet(line) is True


@pytest.mark.parametrize("line", [
    "- ## PROFESSIONAL EXPERIENCE",           # real, from application 1358
    "## PROFESSIONAL EXPERIENCE",
    "* # Skills",
])
def test_a_bullet_prefixed_markdown_heading_is_not_a_bullet(line):
    """_is_header checks for '#' BEFORE stripping the bullet glyph, so a heading
    that carried one reached the bullet list."""
    assert _is_not_a_bullet(line) is True


def test_a_colon_inside_a_bullet_does_not_disqualify_it():
    """Only a TRAILING colon marks a label — mid-sentence colons are ordinary."""
    assert _is_not_a_bullet(
        "Shipped the pricing rewrite: revenue per user rose 18%") is False
