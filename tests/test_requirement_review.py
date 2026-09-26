"""An experience requirement is a sentence, not a number.

"4-6 years", "three (3) years", "2+", "at least 18 months" and "one year" are
five different requirements. Rounding them to one integer loses the thing the
candidate needs: how far short they are, and whether the shortfall is the kind a
screening call survives. So wording is kept verbatim and read as written.

Two scoping rules are pinned here because both errors point the same way — at a
candidate being told a job is out of reach when the posting does not say so:

  * a preference word binds to its own CLAUSE. In "5 years of experience; Kafka
    preferred" the preference is about Kafka, and reading it as optional five
    years understates the requirement.
  * a "Preferred Qualifications:" HEADING makes the bullets under it preferred.
    Calling those required invents blocking gaps.

And the review itself: three buckets, no fourth. Requirements the résumé
supports, genuine gaps, and the questions only the candidate can answer. A
PREFERRED shortfall is not a genuine gap. A suggested project is not evidence,
and if one appears in the draft as something already done, that is a fabrication
the review names.

Nothing in this file asserts a hiring outcome. The last test asserts the
opposite: no output offers a score, a percentage or a chance of an interview.
"""
from __future__ import annotations

import pytest

from app.tailoring.inventory import build_inventory
from app.tailoring.requirements import (GAP, LISTED_ONLY, PROJECT_ONLY, SHORT,
                                        SUPPORTED, UNDATED, ExperienceRequirement,
                                        assess, parse_requirements, review,
                                        unconfirmed_project_claims)

# Synthetic throughout — no real candidate history appears in this repo.
MASTER = """# Alex Rivera
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
"""

JD = """Backend Engineer

## Minimum Qualifications
- 4-6 years of software development experience.
- Three (3) years of experience with Python.
- 2 years of experience with Kubernetes.
- Experience with Kafka event streaming at scale.

## Preferred Qualifications
- 2+ years of experience with Terraform.
"""


# ── reading the wording ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,verbatim,lo,hi,skill", [
    ("4-6 years of software development experience.", "4-6 years", 48, 72, ""),
    ("Minimum three (3) years of experience with Java.",
     "Minimum three (3) years", 36, None, "Java"),
    ("3+ years of professional experience in Python.", "3+ years", 36, None, "Python"),
    ("At least 18 months of hands-on experience with Kubernetes.",
     "At least 18 months", 18, None, "Kubernetes"),
    ("2 to 4 years working with React.", "2 to 4 years", 24, 48, "React"),
    ("One year of experience with SQL.", "One year", 12, None, "SQL"),
    ("5+ yrs of Terraform.", "5+ yrs", 60, None, "Terraform"),
])
def test_a_requirement_keeps_its_wording(text, verbatim, lo, hi, skill):
    reqs = parse_requirements(text)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.verbatim == verbatim
    assert (r.months_min, r.months_max, r.skill) == (lo, hi, skill)


def test_a_digit_in_parentheses_wins_over_the_word():
    """"three (3)" states one number twice. Both readings must agree."""
    assert parse_requirements("three (3) years")[0].months_min == 36


def test_a_plus_removes_the_upper_bound():
    r = parse_requirements("3-5+ years of Go")[0]
    assert r.months_min == 36 and r.months_max is None


def test_a_ceiling_is_not_a_requirement():
    """"up to 5 years" asks for nothing; reading it as a floor invents a gap."""
    assert parse_requirements("Up to 5 years of experience.") == []


def test_an_implausible_number_is_ignored():
    assert parse_requirements("Founded in 1998 years of heritage") == []
    assert parse_requirements("0 years of experience") == []


def test_a_number_word_is_not_mistaken_for_a_skill():
    """"Three (3) years" was yielding "three" as the skill, which then showed up
    as "three: not present anywhere on the résumé"."""
    assert parse_requirements("Three (3) years of experience.")[0].skill == ""


def test_a_domain_noun_is_not_a_skill_gap():
    """Telling someone "software development: not on your résumé" about a résumé
    headed Software Engineer buries the real gaps."""
    assert parse_requirements("4-6 years of software development experience.")[0].skill == ""


# ── required vs preferred, scoped correctly ──────────────────────────────────

def test_a_preference_binds_to_its_own_clause():
    """THE SCOPING DEFECT. The preference is about Kafka, not the five years."""
    reqs = parse_requirements("5 years of experience; Kafka preferred.")
    assert len(reqs) == 1 and reqs[0].required is True


def test_an_explicit_preference_in_the_same_clause_is_honoured():
    assert parse_requirements("One year of SQL is preferred.")[0].required is False


def test_a_preferred_heading_applies_to_the_bullets_under_it():
    reqs = {r.skill: r for r in parse_requirements(JD)}
    assert reqs["Terraform"].required is False
    assert reqs["Python"].required is True
    assert reqs[""].required is True          # the 4-6 years line


def test_a_required_word_in_the_clause_beats_a_preferred_heading():
    reqs = parse_requirements(
        "## Preferred Qualifications\n- 3 years of Java is required for this team.\n")
    assert reqs and reqs[0].required is True


def test_requirements_keep_their_stated_order_and_deduplicate():
    reqs = parse_requirements(
        "3+ years of Python required.\nWe want 3+ years of Python.\n2 years of Go.")
    assert [(r.months_min, r.skill) for r in reqs] == [(36, "Python"), (24, "Go")]


def test_a_bulleted_list_yields_one_requirement_per_bullet():
    """The enumerated-per-skill shape: each bullet is its own requirement."""
    reqs = parse_requirements(
        "- three (3) years of experience with Java\n"
        "- three (3) years of experience with Spring Boot\n")
    assert [r.skill for r in reqs] == ["Java", "Spring Boot"]


def test_html_in_a_description_is_stripped_first():
    assert parse_requirements("<li><b>3+ years</b> of Python</li>")[0].skill == "Python"


# ── assessing against the inventory ──────────────────────────────────────────

@pytest.fixture(scope="module")
def inv():
    return build_inventory(MASTER, extra_skills=[
        "Python", "Kubernetes", "Terraform", "FastAPI", "Kafka"])


def _req(months, skill="", required=True):
    return ExperienceRequirement(verbatim=f"{months // 12} years",
                                 months_min=months, skill=skill, required=required)


def test_a_skill_mentioned_in_a_role_is_not_the_length_of_the_role(inv):
    """AUDIT 2026-09-25 (finding 4). FastAPI appears in one bullet of a
    33-month job; the résumé never says how long it was used. That is an open
    question for the candidate, not 33 months of FastAPI."""
    a = assess(_req(24, "FastAPI"), inv)
    assert a.status == UNDATED and a.held_months == 0
    assert "does not say for how long" in a.line()
    assert not a.is_gap


def test_one_dated_month_inside_six_years_does_not_support_five_years():
    """The audit's reproduction: Python in Dec 2025 only, during a six-year job."""
    master = ("## Experience\n**Software Engineer** | Acme | Jan 2020 - Dec 2025\n"
              "- Wrote a Python script in Dec 2025 to migrate billing data.\n"
              "- Built Java services handling 40k requests per day.\n")
    inv2 = build_inventory(master, extra_skills=["Python"])
    assert inv2.skill("Python").employment_months == 1
    assert inv2.skill("Python").role_months == 72
    a = assess(_req(60, "Python"), inv2)
    assert a.status != SUPPORTED
    assert a.status == UNDATED and a.held_months == 1


def test_dated_time_with_a_skill_is_supported():
    master = ("## Experience\n**Python Engineer** | Acme | Jan 2020 - Dec 2022\n"
              "- Built services handling 40k requests per day.\n")
    a = assess(_req(24, "Python"), build_inventory(master, extra_skills=["Python"]))
    assert a.status == SUPPORTED and a.held_months == 36


def test_roles_too_short_for_the_requirement_are_short_even_undated():
    master = ("## Experience\n**Engineer** | Acme | Jan 2024 - Dec 2024\n"
              "- Built services in Go handling 40k requests per day.\n")
    a = assess(_req(36, "Go"), build_inventory(master, extra_skills=["Go"]))
    assert a.status == SHORT and a.held_months == 12
    assert "upper bound" in a.line()


def test_not_enough_time_is_short_and_says_how_much_is_held(inv):
    a = assess(_req(60), inv)
    assert a.status == SHORT and a.held_months == 33
    assert "2 years 9 months" in a.line()


def test_a_project_skill_is_never_reported_as_years_of_employment(inv):
    """THE CLAIM THAT ENDS A SCREENING CALL."""
    a = assess(_req(24, "Kubernetes"), inv)
    assert a.status == PROJECT_ONLY
    assert a.held_months == 0
    assert "not in paid employment" in a.line()
    assert "is not years of professional experience" in a.line()


def test_an_internship_skill_is_measured_against_internship_time(inv):
    """Real work at a real employer — reported as an internship, not a project,
    and not folded into professional experience."""
    a = assess(_req(36, "Python"), inv)
    assert a.status == SHORT
    assert a.held_months == 3
    assert "internship" in a.line().lower()
    assert "personal project" not in a.line().lower()


def test_a_listed_only_skill_says_so_plainly(inv):
    a = assess(_req(24, "Terraform"), inv)
    assert a.status == LISTED_ONLY
    assert "skills list" in a.line()


def test_a_skill_absent_from_the_resume_is_a_gap(inv):
    a = assess(_req(24, "Kafka"), inv)
    assert a.status == GAP and a.held_months == 0


def test_a_requirement_naming_no_skill_uses_overall_employment_time(inv):
    assert assess(_req(24), inv).held_months == inv.employment_months


# ── the pre-download review ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rep():
    return review(MASTER, MASTER, JD, suggested_projects=["Kafka"])


def test_the_review_reports_the_employment_total_and_keeps_internships_apart(rep):
    assert "2 years 9 months of paid employment" in rep.employment_summary
    assert "3 months of internships" in rep.employment_summary


def test_a_genuine_gap_is_listed_as_one(rep):
    joined = " ".join(rep.gaps)
    assert "4-6 years" in joined
    assert "Kubernetes" in joined


def test_a_preferred_shortfall_is_not_a_genuine_gap(rep):
    """Filing it as one invents a blocker and talks someone out of a job the
    posting says they can have without it."""
    assert not any("Terraform" in g for g in rep.gaps)
    terraform = [q for q in rep.questions if "Terraform" in q]
    assert terraform and "not a blocker" in terraform[0]


def test_the_open_questions_are_ones_only_the_candidate_can_answer(rep):
    blob = " ".join(rep.questions).lower()
    assert "internship experience is" in blob
    assert "if you used it in a role" in blob


def test_a_suggested_project_stays_in_the_improvement_plan(rep):
    assert rep.improvement_plan
    assert "Kafka" in rep.improvement_plan[0]
    assert "until you confirm" in rep.improvement_plan[0]
    assert rep.unconfirmed_claims == ()
    assert rep.ok is True


def test_a_suggested_project_written_into_the_draft_is_caught():
    """Skill-gap advice is a plan. If "build something with Kafka" becomes a
    Kafka bullet, the document asserts work the candidate has not confirmed."""
    leaky = MASTER.replace("- Wrote Python scripts",
                           "- Built a Kafka streaming pipeline\n- Wrote Python scripts")
    r = review(MASTER, leaky, JD, suggested_projects=["Kafka"])
    assert r.unconfirmed_claims == ("Kafka",)
    assert r.ok is False


def test_the_improvement_plan_is_derived_when_no_advice_is_passed():
    """THE DEAD WIRING. Nothing in production supplied `suggested_projects`, so
    the plan was always empty and the fabrication check below never ran — a draft
    that started claiming a skill the résumé has never evidenced passed silently.
    `evidence.fabrication_violations` does not catch this class: a skill is not an
    employer, a date or a number."""
    master = ("## Experience\n**Engineer** | Acme | Jan 2024 - Present\n"
              "- Built a FastAPI service with Postgres.\n## Skills\nPython, FastAPI\n")
    jd = "## Minimum Qualifications\n- 3 years of experience with Kafka.\n"
    r = review(master, master, jd)
    assert any(p.startswith("Kafka") for p in r.improvement_plan)
    assert "does not belong on the résumé until then" in r.improvement_plan[0]


def test_a_draft_that_starts_claiming_an_unevidenced_skill_is_caught():
    master = ("## Experience\n**Engineer** | Acme | Jan 2024 - Present\n"
              "- Built a FastAPI service with Postgres.\n## Skills\nPython, FastAPI\n")
    jd = "## Minimum Qualifications\n- 3 years of experience with Kafka.\n"
    leaky = master.replace("- Built a FastAPI",
                           "- Built Kafka consumers at scale.\n- Built a FastAPI")
    r = review(master, leaky, jd)
    assert r.unconfirmed_claims == ("Kafka",)
    assert r.ok is False


def test_explicit_advice_overrides_the_derived_plan():
    master = "## Skills\nPython\n"
    r = review(master, master, "3 years of Kafka.", suggested_projects=["Redis"])
    assert [p.split(" — ")[0] for p in r.improvement_plan] == ["Redis"]
    assert "until you confirm you have done it" in r.improvement_plan[0]


@pytest.mark.parametrize("word", [
    "minimum", "qualifications", "requirements", "responsibilities",
    "understanding", "familiarity", "expertise", "ability", "knowledge",
])
def test_a_jd_structure_word_is_never_a_skill_to_go_and_learn(word):
    """"Minimum Qualifications" is a heading. Listing "minimum" as a gap buries
    the real ones, and it reached the improvement plan as something to learn."""
    jd = f"## {word.title()} Qualifications\n- 3 years of experience.\n"
    r = review("## Skills\nPython\n", "## Skills\nPython\n", jd)
    assert not any(word in g.lower().split(":")[0] for g in r.gaps)
    assert not any(word in p.lower().split(" — ")[0] for p in r.improvement_plan)


def test_unconfirmed_claims_are_matched_on_word_boundaries():
    assert unconfirmed_project_claims("Used Kafka Streams daily.", ["Kafka"]) == ["Kafka"]
    assert unconfirmed_project_claims("Worked on Kafkaesque forms.", ["Kafka"]) == []
    assert unconfirmed_project_claims("Nothing here.", []) == []


def test_a_clean_draft_reviews_without_a_master_resume_gap_sweep_explosion(rep):
    """The gap list is a requirements review, not a keyword dump."""
    assert len(rep.gaps) <= 10


def test_year_only_dates_are_admitted_as_approximate():
    master = ("## Experience\n**Engineer** | Acme | 2019 - 2024\n"
              "- Built and operated the billing service end to end.\n")
    r = review(master, master, "5+ years of experience required.")
    assert any("year only" in q for q in r.questions)


def test_the_review_serialises_to_the_three_buckets(rep):
    d = rep.as_dict()
    for key in ("requirements_supported", "genuine_gaps", "open_questions",
                "improvement_plan", "unconfirmed_claims", "employment_summary"):
        assert key in d
    assert isinstance(d["genuine_gaps"], list)


def test_an_empty_posting_reviews_without_raising():
    r = review(MASTER, MASTER, "")
    assert r.gaps == () and r.supported == ()
    assert "2 years 9 months" in r.employment_summary


def test_a_missing_master_resume_does_not_raise():
    r = review("", "", JD)
    assert r.employment_summary.startswith("none on the résumé")


# ── the route is owner-scoped and read-only ──────────────────────────────────

def test_the_review_route_requires_ownership_and_writes_nothing():
    import inspect

    from app.api import server
    src = inspect.getsource(server.application_pre_download_review)
    assert "_require_owned_application" in src, "an id-bearing route must check ownership"
    for mutation in ("session.add(", "session.commit(", "session.delete("):
        assert mutation not in src, "the review must not modify the application"


# ── nothing here predicts an outcome ─────────────────────────────────────────

def test_no_line_offers_a_score_or_a_chance_of_an_interview(rep):
    """A keyword match is not a hiring probability. Every line is a fact about
    the résumé and the posting, or an admission that we do not know."""
    blob = rep.as_text().lower()
    for banned in ("chance", "likelihood", "probability", "odds", "%",
                   "you will get", "guaranteed", "score", "rank", "out of 100"):
        assert banned not in blob, f"the review must never say {banned!r}"


def test_the_review_never_tells_the_user_they_will_be_interviewed(rep):
    blob = rep.as_text().lower()
    for phrase in ("will be interviewed", "will get an interview",
                   "expect an interview", "you are a strong match"):
        assert phrase not in blob


def test_requirements_module_needs_no_llm_or_network():
    import inspect

    from app.tailoring import requirements
    src = inspect.getsource(requirements)
    for forbidden in ("requests.", "httpx.", "anthropic", "openai", "urlopen",
                      "socket.", "get_session"):
        assert forbidden not in src


# ── the wiring, not just the helpers ─────────────────────────────────────────

def test_the_tailor_is_told_what_is_backed_and_what_is_only_a_project():
    """The block has to reach the prompt, and it has to carry the distinction —
    a helper nothing calls protects no one."""
    import inspect

    from app.tailoring.tailor import Tailor
    src = inspect.getsource(Tailor.tailor_resume)
    assert "{evidence_block}" in src, "the block must be interpolated into the prompt"
    assert "build_inventory" in src
    assert "BACKED BY PAID WORK" in src
    assert "DEMONSTRATED BUT NOT EMPLOYMENT" in src
    assert "is NOT years of professional" in src
    # The posting's own wording, and an explicit ban on inflating it.
    assert "STATED EXPERIENCE REQUIREMENTS" in src
    assert "inflate" in src
    # Acronyms are expanded only from the table.
    assert "Do not \\nexpand anything not on this list" in src.replace("\n", "\\n") \
        or "expand anything not on this list" in src


def test_an_inventory_failure_never_fails_a_tailor_run():
    """Best-effort: a résumé this parser cannot read must still be tailored."""
    import inspect

    from app.tailoring.tailor import Tailor
    src = inspect.getsource(Tailor.tailor_resume)
    block = src[src.index("evidence_block = \"\""):src.index("highlights_block = \"\"")]
    assert "except Exception" in block
    assert "continuing without it" in block


def test_the_dashboard_shows_the_review_before_the_download():
    """It exists so the person clicking Download has read it. A route nothing
    calls is a report nobody sees."""
    from pathlib import Path
    dash = Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html"
    html = dash.read_text()
    assert "loadPreDownloadReview" in html
    assert 'id="predownload-review"' in html
    assert "/review?_=" in html, "the review must be fetched, not just declared"
    # Cleared on open: a slow /details must not leave the PREVIOUS application's
    # gaps on screen under this application's title.
    assert "_revReset" in html
    # And it must not RENDER a number the product cannot stand behind. Comments
    # are stripped first: the panel's own comment explains why there is no
    # interview likelihood in it, and matching that would be the test failing on
    # the documentation of the thing it wants.
    import re as _re
    panel = html[html.index("loadPreDownloadReview"):]
    panel = panel[:panel.index("async function verifyFormFromModal")]
    panel = _re.sub(r"//[^\n]*", "", panel)
    for banned in ("chance of", "likelihood", "probability", "% match",
                   "interview chance", "out of 100"):
        assert banned not in panel.lower(), f"the panel must never render {banned!r}"


def test_the_review_classes_are_in_the_compiled_dashboard_css():
    """Both stylesheets are compiled AND committed; node_modules is absent in
    CI, so a new class that was never built is an invisible style in production."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    css = (root / "app" / "static" / "tailwind.css").read_text()
    for cls in ("mb-5", "space-y-1.5", "rounded-2xl", "text-red-400",
                "tracking-wide", "leading-relaxed"):
        esc = cls
        for ch in "/[]().:":
            esc = esc.replace(ch, "\\" + ch)
        assert f".{esc}" in css, f"{cls} missing from tailwind.css — run `npm run build`"
