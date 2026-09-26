"""Tenure is a union of intervals — not a span, and not a sum.

PRODUCTION, 2026-09-25. `resume_basic_extract._years_experience` measured
`min(start) → max(end)`: the SPAN of a career rather than the time inside it. A
2016 summer internship plus a job started in 2024 reported **10 years of
experience** for a résumé holding under three. That number is not cosmetic — it
reaches `reranker`'s seniority rules ("candidate has ~{yoe} years"),
`RuleFilter.cand_years` and the UserCard, so the inflation spends a day's finals
budget on Staff and Principal postings the user will not be screened for.

Summing the periods instead is the mirror error: two roles held at once would
count their overlap twice. Both are the same mistake — there is exactly one
correct measure, the union, and `inventory.merged_months` is the only
implementation of it.

The second thing this file pins is that a résumé's "experience" is not one
substance. A completed project is real evidence that someone can do the thing;
it is not years of professional employment, and an internship is neither a side
project nor a staff job. Telling someone their Kubernetes is three years of
production experience because they built an operator over a weekend is what ends
a screening call.

No test here asserts a hiring outcome, a score or a percentage — the last test
in the file asserts the OPPOSITE: that no output ever offers one.
"""
from __future__ import annotations

import pytest

from app.tailoring.inventory import (ACRONYMS, EMPLOYMENT_KINDS, INTERNSHIP,
                                     PERSONAL, PROFESSIONAL, acronym_pairs,
                                     build_inventory, expand_acronym,
                                     humanize_months, merged_months)

# A synthetic résumé. Every fact in it is invented for the test — no real
# candidate's history, contact details or immigration status appears in this repo.
MASTER = """# Alex Rivera
Cincinnati, OH | alex@example.invalid

## Professional Experience
**Software Engineer** | Northwind Labs | Jan 2024 - Present | Remote
- Built a FastAPI service handling 40k requests per day with Postgres and Redis.
- Automated the CI/CD pipeline with GitHub Actions, cutting release time 30%.

**Software Engineering Intern** | Contoso | Jun 2016 - Aug 2016 | Columbus, OH
- Wrote Python scripts to reconcile nightly batch reports.

## Projects
**Volta** | Personal | Mar 2023 - Jun 2023
- Built a Kubernetes operator in Go to autoscale GPU workers on a home cluster.

## Skills
Languages: Python, Go, SQL
Tools: Docker, Kubernetes, Terraform, FastAPI

## Education
**M.S. Computer Science** | Ohio State University | 2021 - 2023
"""


@pytest.fixture(scope="module")
def inv():
    return build_inventory(MASTER, extra_skills=[
        "Kubernetes", "FastAPI", "Java", "Terraform", "Python", "CI/CD"])


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_a_career_gap_is_not_employment():
    """THE DEFECT. 2016 internship + a 2024 job was reported as ten years."""
    from app.intelligence.resume_basic_extract import _years_experience
    years = _years_experience([
        {"start": "Jun 2016", "end": "Aug 2016"},
        {"start": "Jan 2024", "end": "Sep 2026"},
    ])
    assert years == 3, "the eight-year gap must not be counted as worked time"


def test_two_concurrent_roles_are_not_counted_twice():
    """The mirror error. Summing the periods would report four years."""
    from app.intelligence.resume_basic_extract import _years_experience
    assert _years_experience([
        {"start": "Jan 2023", "end": "Jan 2025"},
        {"start": "Jan 2023", "end": "Jan 2025"},
    ]) == 2


@pytest.mark.parametrize("intervals,expected", [
    ([], 0),
    ([(0, 0)], 1),                                  # one month is one month
    ([(0, 11)], 12),
    ([(0, 11), (12, 23)], 24),                      # contiguous: no phantom gap
    ([(0, 11), (13, 24)], 24),                      # a real one-month gap
    ([(0, 23), (12, 35)], 36),                      # overlap counted once
    ([(0, 23), (0, 23)], 24),                       # identical
    ([(0, 35), (12, 23)], 36),                      # fully contained
    ([(12, 23), (0, 11)], 24),                      # order does not matter
    ([(10, 5)], 0),                                 # end before start: ignored
])
def test_merged_months(intervals, expected):
    assert merged_months(intervals) == expected


def test_a_none_bound_is_dropped_not_guessed():
    assert merged_months([(None, 10), (0, 11)]) == 12
    assert merged_months([(0, None)]) == 0


@pytest.mark.parametrize("months,text", [
    (0, "none on the résumé"), (1, "1 month"), (11, "11 months"),
    (12, "1 year"), (13, "1 year 1 month"), (33, "2 years 9 months"),
    (35, "2 years 11 months"),               # never rounded up to "3 years"
])
def test_humanize_months(months, text):
    assert humanize_months(months) == text


# ── the five kinds of work ───────────────────────────────────────────────────

def test_the_totals_separate_employment_from_internships(inv):
    assert inv.employment_months == 33               # Jan 2024 → Sep 2026
    assert inv.internship_months == 3                # Jun–Aug 2016
    assert "internship" not in humanize_months(inv.employment_months)


def test_an_intern_title_overrides_its_section(inv):
    """"Software Engineering Intern" under a plain "Experience" heading is an
    internship. Reading the section alone made it professional employment."""
    intern = [e for e in inv.engagements if e.kind == INTERNSHIP]
    assert len(intern) == 1
    assert "Intern" in intern[0].title
    assert intern[0].is_employment is False


def test_a_project_is_not_employment(inv):
    project = [e for e in inv.engagements if e.kind == PERSONAL]
    assert len(project) == 1 and project[0].months == 4
    assert project[0].is_employment is False
    # …and its months are in no total.
    assert inv.employment_months == 33


def test_education_is_academic_and_counts_toward_nothing(inv):
    academic = [e for e in inv.engagements if e.kind == "academic"]
    assert academic and "Computer Science" in academic[0].title
    assert all(not e.is_employment for e in academic)


def test_the_employer_is_read_from_the_pipe_field_not_the_city(inv):
    """"| Contoso | Jun 2016 - Aug 2016 | Columbus, OH" named Columbus as the
    employer, because a comma split made the city look like a field."""
    intern = inv.by_kind(INTERNSHIP)[0]
    assert intern.org == "Contoso"


# ── attribution: which work backs which skill ────────────────────────────────

def test_a_skill_used_in_paid_work_is_paid_work_of_unstated_duration(inv):
    """AUDIT 2026-09-25 (finding 4). A skill named in one bullet of a 33-month
    job was credited with all 33 months. The role's length is kept as context
    (`role_months`, an upper bound); the skill's own time is what the résumé
    dates, and here it dates none."""
    fastapi = inv.skill("FastAPI")
    assert fastapi.kinds == frozenset({PROFESSIONAL})
    assert fastapi.paid_work is True
    assert fastapi.employment_months == 0
    assert fastapi.role_months == 33
    assert fastapi.duration_unknown is True
    assert fastapi.project_only is False


def test_a_title_naming_the_skill_dates_the_whole_role():
    inv2 = build_inventory("## Experience\n**Python Developer** | Beta | Jan 2018 - Dec 2018\n"
                           "- Maintained internal tooling for the data team.\n",
                           extra_skills=["Python"])
    assert inv2.skill("Python").employment_months == 12


def test_a_dated_bullet_dates_only_its_own_span():
    inv2 = build_inventory(
        "## Experience\n**Engineer** | Acme | Jan 2020 - Dec 2025\n"
        "- Led the Go rewrite (Mar 2022 - Aug 2022) of the ingest service.\n"
        "- Planned the market 2024 roadmap with the Go team.\n",
        extra_skills=["Go"])
    # Mar–Aug 2022 only; "market 2024" is not March 2024.
    assert inv2.skill("Go").employment_months == 6


def test_a_skill_shown_only_in_a_project_carries_no_employment_time(inv):
    """THE CLAIM THAT ENDS A SCREENING CALL. The operator is real; it is not
    two years of production Kubernetes."""
    k8s = inv.skill("Kubernetes")
    assert k8s.project_only is True
    assert k8s.employment_months == 0
    assert not (k8s.kinds & EMPLOYMENT_KINDS)


def test_an_internship_skill_is_not_called_a_side_project(inv):
    """Python was used at an employer. Reporting it as "a personal project" is
    both wrong and insulting, so it gets its own case."""
    py = inv.skill("Python")
    assert py.internship_only is True
    assert py.project_only is False, "an internship is paid work in a real team"
    assert py.employment_months == 0


def test_a_skill_only_in_the_skills_list_is_marked_as_such(inv):
    """Terraform is listed; no role or project describes using it."""
    tf = inv.skill("Terraform")
    assert tf is not None, "a listed skill belongs in the inventory"
    assert tf.listed_only is True
    assert tf.employment_months == 0


def test_the_whole_skills_block_is_read_not_just_its_first_line(inv):
    """The collector stopped after "Languages:", dropping every tool below it."""
    for name in ("Docker", "Terraform", "Go", "SQL"):
        assert inv.skill(name) is not None, f"{name} was in the skills block"


def test_a_skill_absent_from_the_resume_is_absent_from_the_inventory(inv):
    assert inv.skill("Java") is None


@pytest.mark.parametrize("written", ["CI/CD", "ci/cd", "CI CD", "ci-cd"])
def test_punctuated_skills_still_match(written):
    """`\\b` breaks on ci/cd, node.js, c++ and .net."""
    inv2 = build_inventory(
        "## Experience\n**Engineer** | Acme | Jan 2024 - Dec 2024\n"
        "- Ran the CI/CD pipeline and the Node.js build.\n",
        extra_skills=[written, "Node.js", "nodejs"])
    assert inv2.skill(written) is not None
    assert inv2.skill("Node.js").role_months == 12


# ── selection order ──────────────────────────────────────────────────────────

def test_the_strongest_evidence_is_offered_first(inv):
    """An employer reads the top of a résumé. The best-backed claim goes there."""
    order = [e.display for e in inv.ranked_skills(
        ["Terraform", "Kubernetes", "Python", "FastAPI"])]
    assert order[0] == "FastAPI", "paid work outranks an internship and a project"
    assert order.index("Python") < order.index("Kubernetes"), \
        "an internship outranks a weekend project"
    assert order[-1] == "Terraform", "a bare list entry is the weakest evidence"


def test_a_skill_named_twice_is_offered_once(inv):
    """Callers concatenate the JD's phrases with the requirements' skills, so the
    same skill arrives under two spellings."""
    assert [e.display for e in inv.ranked_skills(
        ["python", "Python", "FastAPI", "fastapi"])] == ["FastAPI", "Python"]


def test_an_unevidenced_name_is_simply_not_offered(inv):
    assert inv.ranked_skills(["Java", "Rust", "COBOL"]) == []


# ── approximate dates are admitted, not smoothed over ────────────────────────

def test_a_year_only_employment_date_marks_the_total_approximate():
    inv2 = build_inventory("## Experience\n**Engineer** | Acme | 2021 - 2023\n"
                           "- Built and shipped the billing service.\n")
    assert inv2.approximate is True


def test_an_undated_education_section_does_not_make_employment_approximate(inv):
    """"2021 - 2023" under Education says nothing about employment precision."""
    assert inv.approximate is False
    assert any(e.approximate for e in inv.engagements if e.kind == "academic")


# ── acronyms: expanded when known, never invented ────────────────────────────

@pytest.mark.parametrize("term,expected", [
    ("CI/CD", "continuous integration and continuous delivery"),
    ("etl", "extract, transform, load"),
    ("k8s", "Kubernetes"),
    ("RAG", "retrieval-augmented generation"),
])
def test_known_acronyms_expand(term, expected):
    assert expand_acronym(term) == expected


@pytest.mark.parametrize("term", ["ZZQ", "Northwind", "", "Volta", "framework"])
def test_an_unknown_acronym_is_never_guessed(term):
    assert expand_acronym(term) is None


def test_acronym_pairs_are_deduplicated_and_skip_the_unknown():
    assert acronym_pairs(["CI/CD", "ci/cd", "ZZQ", "ETL"]) == [
        ("CI/CD", "continuous integration and continuous delivery"),
        ("ETL", "extract, transform, load"),
    ]


def test_every_expansion_is_a_real_expansion():
    """A table entry that restates the acronym teaches a reader nothing."""
    for short, long in ACRONYMS.items():
        assert long and long.lower() != short.lower()
        assert len(long) > len(short)


# ── identity is shared with the grounding evidence ───────────────────────────

def test_span_ids_agree_with_the_grounding_evidence_module():
    """Two modules address the same line, so they must address it the SAME way —
    pinned here rather than trusted to a comment."""
    from app.tailoring.evidence import extract_spans
    from app.tailoring.inventory import _span_id
    for span in extract_spans(MASTER):
        assert _span_id(span.text) == span.span_id


def test_the_inventory_carries_the_evidence_id(inv):
    from app.tailoring.evidence import build_evidence
    assert inv.evidence_id == build_evidence(MASTER).evidence_id


def test_the_inventory_is_read_only_and_needs_no_network():
    """No LLM, no DB, no HTTP: it must be safe to build on any path."""
    import inspect

    from app.tailoring import inventory
    src = inspect.getsource(inventory)
    for forbidden in ("requests.", "httpx.", "anthropic", "openai",
                      "get_session", "urlopen", "socket."):
        assert forbidden not in src, f"inventory must not reach for {forbidden}"


# ── nothing here predicts an outcome ─────────────────────────────────────────

def test_no_output_offers_a_score_or_a_chance_of_an_interview(inv):
    """A keyword match is not a hiring probability. Every user-visible string in
    this module states a fact about the résumé or admits what we do not know."""
    strings = [humanize_months(inv.employment_months),
               humanize_months(inv.internship_months)]
    for ev in inv.ranked_skills(["FastAPI", "Kubernetes", "Python", "Terraform"]):
        strings.append(ev.display)
    blob = " ".join(strings).lower()
    for banned in ("%", "chance", "likelihood", "probability", "odds",
                   "you will get", "guaranteed", "score"):
        assert banned not in blob
