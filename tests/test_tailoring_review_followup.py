"""Review follow-up to the Tailoring Studio fixes (2026-10-09).

The first round cleaned "Not covered" for the Horizon3 posting but broke it for
every other field: field words sat on the company-boilerplate list ("cell
culture", "public policy" and "employee relations" lost their phrase and left
"cell", "public", "relations" behind), a phrase written once had to carry a
capitalised word, so bulleted lists and lower-case tools (pandas, dbt, pytest)
vanished, a pipe-separated summary tagline was never fact-checked, "No-code"
lost its "No-", and two keyword paths never learned the company's name. Each
test here fails on the code before this change.
"""
from __future__ import annotations

import re

import pytest

from app.config import settings
from app.tailoring import verify_cache
from app.tailoring.ats_keywords import (
    _company_terms,
    _is_noise_phrase,
    analyze,
    extract_jd_phrases,
)
from app.tailoring.evidence import remove_summary_sentences, summary_sentences
from app.tailoring.grounding import GroundingChecker

MASTER = ("# Pat Lee\npat@example.com\n\n## Experience\n**Analyst** | Foo | Jan 2020 - Present\n"
          "- Built Excel models for 3 regions.\n")


# ── 1. field words are not boilerplate; a dropped phrase leaves no orphan ────

NON_TECH = {
    "policy": (
        ("Policy Analyst. Conduct Public Policy research. Public policy degree required. "
         "Policy Analysis experience. Policy analysis memos."),
        {"public policy", "policy analysis"}, {"public", "analysis"}),
    "dental": (
        ("Dental Hygienist. Provide Dental Hygiene services. Dental hygiene license required. "
         "Dental hygiene education."),
        {"dental hygiene"}, {"hygiene", "hygienist"}),
    "biotech": (
        ("Research Associate. Maintain Cell Culture for our assays. Cell culture experience "
         "required. Gene Expression profiling with qPCR. Gene expression analysis."),
        {"cell culture", "gene expression"}, {"cell", "gene"}),
    "hr": (
        ("Our HR team needs an Employee Relations Partner. You will own employee relations "
         "cases. Employee relations experience is required. Benefits Administration and "
         "Workday knowledge preferred. Benefits administration for 2,000 employees."),
        {"employee relations", "benefits administration", "workday"},
        {"relations", "administration"}),
    "infosec": (
        ("Information Security Engineer. Protect our systems with information security "
         "controls. 5 years of information security experience. Identity Management and SSO."),
        {"information security", "identity management"}, {"management"}),
    "manufacturing": (
        ("Process Engineer. Drive Process Improvement on the line. Process improvement and "
         "Lean Six Sigma. Process improvement projects."),
        {"process improvement", "lean six sigma"}, {"improvement", "drive process"}),
    "marketing": (
        ("Growth Marketing Manager. Own Growth Marketing experiments. Growth marketing "
         "experience. Paid acquisition and growth marketing."),
        {"growth marketing"}, {"marketing"}),
    "medicine": (
        ("Family Nurse Practitioner. Family Medicine clinic. Family medicine experience. "
         "Board certified in family medicine."),
        {"family medicine"}, {"medicine"}),
    "people-ops": (
        ("People Operations Generalist. Support People Operations. People operations "
         "experience. HRIS and people operations."),
        {"people operations"}, {"operations", "generalist"}),
    "customer-service": (
        ("Customer Service Representative. Deliver Customer Service by phone. Customer "
         "service experience required. Customer service metrics."),
        {"customer service"}, {"representative", "service by phone"}),
}


@pytest.mark.parametrize("field", list(NON_TECH))
def test_a_non_tech_posting_keeps_its_field_phrases(field):
    jd, want, leftovers = NON_TECH[field]
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert want <= set(phrases), phrases
    assert not leftovers & set(phrases), phrases
    missing = analyze(jd, MASTER, company="Acme").missing
    assert want <= set(missing) and not leftovers & set(missing), missing


def test_the_skills_offered_as_buttons_are_never_a_leftover_word():
    """Before: "I've used Relations" / "I've used Administration"."""
    from app.tailoring.requirements import review
    jd = NON_TECH["hr"][0]
    rv = review(MASTER, MASTER, jd, company="Acme")
    assert "employee relations" in rv.missing_skills
    assert not {"relations", "administration"} & set(rv.missing_skills)


def test_boilerplate_still_never_reaches_the_list():
    for noise in ("cybersecurity company", "competitive salary", "equal opportunity employer",
                  "inclusive culture", "dental insurance", "stock options", "parental leave",
                  "growth opportunities", "company culture", "drive process"):
        assert _is_noise_phrase(noise), noise
    for field in ("cell culture", "gene expression", "employee relations", "public policy",
                  "dental hygiene", "benefits administration", "process improvement",
                  "family medicine", "people operations", "identity management",
                  "customer service", "wound care", "health equity"):
        assert not _is_noise_phrase(field), field


def test_cutting_the_company_name_leaves_no_orphan_word():
    """Before: "Salesforce Administrator at Salesforce" left "administrator"."""
    jd = ("Salesforce Administrator at Salesforce. You will configure Salesforce for our sales "
          "teams. Experience administering Salesforce and writing Apex is required. You will "
          "build Salesforce flows and reports.")
    phrases = extract_jd_phrases(jd, top_n=18, company="Salesforce")
    assert "administrator" not in phrases and "flows" not in phrases, phrases
    assert "apex" in phrases, phrases
    assert not any("salesforce" in p for p in phrases), phrases


def test_a_word_beside_company_boilerplate_is_not_promoted():
    """"a Fintech company" describes the employer; dropping the phrase must not
    leave "fintech" behind as a keyword the resume lacks."""
    jd = ("Acme is a Fintech company. Acme is the leading Fintech company in Ohio. "
          "You will build Python services. Python experience required.")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert "fintech" not in phrases and "python" in phrases, phrases


# ── 2. a phrase written once still counts when it is listed or a known tool ──

def test_lower_case_tools_written_once_are_keywords():
    jd = ("Requirements: 2+ years with pandas, numpy and scikit-learn. Experience with dbt. "
          "Comfortable with pytest. Strong SQL.")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"pandas", "numpy", "dbt", "pytest", "sql", "scikit-learn"} <= set(phrases), phrases
    assert "comfortable with pytest" not in phrases


@pytest.mark.parametrize("fmt", ["dash", "bullet", "html", "escaped", "lines"])
def test_a_bulleted_requirements_list_is_kept(fmt):
    import html
    items = ["Wound care", "Phlebotomy", "Telemetry", "Medication administration",
             "Patient education", "BLS and ACLS"]
    if fmt == "dash":
        jd = "Registered Nurse\nRequirements:\n" + "\n".join(f"- {i}" for i in items)
    elif fmt == "bullet":
        jd = "Registered Nurse\nRequirements:\n" + "\n".join(f"• {i}" for i in items)
    elif fmt in ("html", "escaped"):
        jd = ("<p>Registered Nurse</p><p>Requirements</p><ul>"
              + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
        if fmt == "escaped":       # the Greenhouse API's "content" field
            jd = html.escape(jd)
    else:   # how the scrapers store an HTML list: get_text(separator="\n")
        jd = "Registered Nurse\nRequirements\n" + "\n".join(items)
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert {"wound care", "phlebotomy", "telemetry", "medication administration",
            "patient education", "bls", "acls"} <= set(phrases), phrases
    # Two list items never fuse into one phrase across the line break, and no
    # markup becomes a keyword.
    assert not [p for p in phrases if "acls patient" in p or "care phlebotomy" in p], phrases
    assert not [p for p in phrases if re.search(r"\b(?:li|ul)\b", p)], phrases


def test_a_less_than_sign_in_prose_is_not_markup():
    jd = ("Analyst. Requires &lt;3 years of Tableau work and Statistical Modeling. "
          "Statistical modeling daily. Tableau dashboards &gt; spreadsheets.")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert "statistical modeling" in phrases and "tableau" in phrases, phrases


def test_list_items_in_other_fields_and_named_shapes_at_line_start():
    jd = ("Data Analyst\nRequirements:\n- Tableau\n- Looker\n- Statistical modeling\n"
          "- A/B testing\n- Excel and PowerPoint\n- Stakeholder communication")
    phrases = set(extract_jd_phrases(jd, top_n=18, company="Acme"))
    assert {"tableau", "looker", "statistical modeling", "a/b testing", "excel", "powerpoint",
            "stakeholder communication"} <= phrases, phrases
    acct = "Staff Accountant\nMonth-end close\nAccount reconciliations\nNetSuite\nGAAP\nJournal entries"
    assert {"month-end close", "account reconciliations", "netsuite", "gaap",
            "journal entries"} <= set(extract_jd_phrases(acct, top_n=18, company="Acme"))


def test_a_benefits_list_is_the_offer_not_a_keyword():
    jd = ("Software Engineer\nYou will build Python services.\nBenefits\n"
          "- Medical, dental and vision\n- 401(k) matching\n- Unlimited PTO\n- Paid parental leave\n"
          "Requirements\n- Python\n- Elasticsearch")
    phrases = extract_jd_phrases(jd, top_n=18, company="Acme")
    assert "elasticsearch" in phrases
    assert not [p for p in phrases if re.search(r"pto|matching|medical|dental|vision", p)], phrases


# ── 3. a pipe-separated summary tagline is fact-checked ─────────────────────

PIPE_MASTER = """# Alex Tenant
alex@example.com | (555) 555-0100

## Summary
Backend Engineer | Python | AWS | Kubernetes

## Experience
**Software Engineer** | NTT Data | Jan 2021 - Present
- Built REST APIs with FastAPI serving 2,500 requests per minute.
- Diagnosed failing partner integrations by reading service logs.
"""
CLAIM_A = "Translating API documentation into tested connectors"
CLAIM_B = "Shipping systems customers configure without support escalation"
PIPE_DRAFT = PIPE_MASTER.replace("Backend Engineer | Python | AWS | Kubernetes",
                                 f"Integrations Engineer | {CLAIM_A} | {CLAIM_B}")


def test_a_pipe_tagline_yields_its_claims_and_a_skill_list_none():
    assert summary_sentences(PIPE_DRAFT) == [CLAIM_A, CLAIM_B]
    assert summary_sentences(PIPE_MASTER) == []
    # The same tagline sitting under the name (no heading) is read too.
    unheaded = PIPE_DRAFT.replace("## Summary\n", "")
    assert summary_sentences(unheaded) == [CLAIM_A, CLAIM_B]


def test_an_invented_pipe_claim_fails_grounding_and_comes_out_alone(monkeypatch):
    c = GroundingChecker.__new__(GroundingChecker)
    c.model = None
    verify_cache.clear_local()
    monkeypatch.setattr(settings, "grounding_cache_enabled", False, raising=False)
    calls: list = []

    def fake_batch(patches, source_md, kinds=None):
        calls.append([claim for claim, _ in patches])
        return ["API documentation" not in claim for claim, _ in patches]
    monkeypatch.setattr(c, "verify_batch", fake_batch)
    result = c.check(PIPE_MASTER, PIPE_DRAFT)
    assert result.passed is False
    assert [f["bullet"] for f in result.flagged_summary] == [CLAIM_A]
    assert calls and CLAIM_A in calls[0] and CLAIM_B in calls[0]
    md, removed = remove_summary_sentences(PIPE_DRAFT, [CLAIM_A], PIPE_MASTER)
    assert removed == [CLAIM_A]
    assert f"Integrations Engineer | {CLAIM_B}" in md and "API documentation" not in md


# ── 4. "No-code" is a word, not a "No -" lead-in ─────────────────────────────

def test_a_hyphenated_no_or_yes_word_is_not_cut():
    from app.tailoring.doctor import clean_verdict, tidy_verdict
    raw = "No-code tools are absent from the resume, so it is borderline. The biggest risk is GraphQL."
    assert clean_verdict(raw) == raw
    assert tidy_verdict("Borderline-quality match overall.") == "Borderline-quality match overall."
    assert tidy_verdict("No - it lacks GraphQL.") == "It lacks GraphQL."
    assert tidy_verdict("Yes -- strong match.") == "Strong match."
    assert tidy_verdict("1.Yes — strong match.") == "Strong match."


# ── 5. every keyword path knows the company, by any spelling ────────────────

def test_a_slug_company_is_read_against_the_postings_own_spelling():
    from app.tailoring.ats_keywords import _cut_company
    jd = ("Scale AI is hiring. At Scale, we label data for Scale AI customers. You will build "
          "data pipelines at scale. Scale AI uses Python.")
    assert {"scaleai", "scale ai"} <= _company_terms("Scaleai", jd)
    assert "scale" not in _company_terms("Scaleai", jd)        # "at scale" is not the company
    cut = _cut_company(jd, "Scaleai")
    assert "Scale AI" not in cut and "At Scale," not in cut and "at scale" in cut
    phrases = extract_jd_phrases(jd, top_n=18, company="Scaleai")
    assert not [p for p in phrases if "scale" in p], phrases
    assert "data pipelines" in phrases

    pj = ("Palantir Technologies builds Foundry. At Palantir you deploy Foundry. Palantir "
          "engineers write Java. Foundry pipelines.")
    assert "palantir" in _company_terms("Palantirtechnologies", pj)
    phrases = extract_jd_phrases(pj, top_n=18, company="Palantirtechnologies")
    assert not [p for p in phrases if "palantir" in p or "technologies" in p], phrases
    assert "foundry" in phrases


def test_an_ordinary_word_in_the_company_name_is_not_cut_from_the_posting():
    """Before: company "OpenAI" cut "open" from "open source", leaving "source"."""
    jd = ("OpenAI is hiring. You will contribute to open source tooling. Open source "
          "experience required. OpenAI uses Python.")
    phrases = extract_jd_phrases(jd, top_n=18, company="OpenAI")
    assert "open source" in phrases and "source" not in phrases, phrases


def test_the_free_fit_check_never_reports_the_employer(monkeypatch):
    import app.intelligence.job_check as jc

    class _Resp:
        status_code = 200

        def json(self):
            return {"title": "Backend Engineer", "updated_at": "",
                    "content": ("<p>Scale AI builds data tooling. At Scale AI you write Python "
                                "and Kafka. Scale AI teams ship daily. Scale AI customers rely on "
                                "Kafka.</p>")}

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            return _Resp()
    monkeypatch.setattr(jc.httpx, "Client", _Client)
    out = jc.check_job_url("https://boards.greenhouse.io/scaleai/jobs/1",
                           resume_text="Python developer.")
    assert out["fit"] is not None
    assert "kafka" in out["fit"]["missing"]
    assert not [p for p in out["fit"]["missing"] + out["fit"]["matched"] if "scale" in p], out["fit"]


def test_the_stuffing_check_does_not_count_the_employers_name():
    from app.tailoring import rules
    jd = ("Horizon3 builds autonomous pentesting. Horizon3 engineers know Python and GraphQL. "
          "Horizon3 ships integrations. Horizon3 customers rely on Horizon3.")
    md = ("# Alex\n## Summary\nI want to help Horizon3 grow. Horizon3 needs integrations, "
          "Horizon3 needs reliability, and Horizon3 needs Python.\n## Experience\n- Built APIs.")
    assert not [p for p in rules.keyword_stuffing(md, jd, company="Horizon3Ai")
                if "horizon3" in p]
    rows = rules.build_checklist(
        master=md, md=md, jd=jd, rewritten=True, email_status="ok", latest=None,
        human_passed=True, fabrications=[], grounding_status="passed", headline="",
        headline_source="", location_note="", filename_docx="a.docx", filename_pdf="",
        metadata_clean=True, pages=1, coverage={}, trimmed=[], company="Horizon3Ai")
    woven = next(r for r in rows if r["key"] == "keywords_woven")
    assert woven["ok"] is True, woven
