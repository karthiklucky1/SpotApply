"""Tailoring Studio findings from the owner's live test (2026-10-09, Horizon3).

The result panel said "Grounded in your resume" over a summary that claimed
work the master resume never shows; "Not covered" listed "where", "them",
"customer" and the hiring company's own name, with "I've used Where" buttons;
the recruiter's read opened "1. Yes —"; and the prompt never asked for varied
bullet lengths, which the Doctor then flagged. Each test here fails on the code
before the fix.
"""
from __future__ import annotations

import json
import re
import sys
import types

import pytest
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job
from app.tailoring import verify_cache
from app.tailoring.evidence import remove_summary_sentences, summary_sentences
from app.tailoring.grounding import GroundingChecker

EM = "—"

MASTER = """# Alex Tenant
alex@example.com | (555) 555-0100

## Summary
Backend engineer who builds Python services and keeps partner integrations healthy in production.

## Experience
**Software Engineer** | NTT Data | Jan 2021 - Present
- Built REST APIs with FastAPI serving 2,500 requests per minute.
- Diagnosed failing partner integrations by reading service logs and replaying payloads.
- Automated the release pipeline with GitHub Actions and Docker images on Kubernetes.

## Education
B.S. Computer Science, Ohio State University, 2020
"""

INVENTED = ("Experienced translating API documentation into tested connectors and shipping "
            "well-documented systems that customers can configure without support escalation.")

TAILORED = MASTER.replace(
    "keeps partner integrations healthy in production.",
    "keeps partner integrations healthy in production. " + INVENTED)


@pytest.fixture
def checker(monkeypatch):
    c = GroundingChecker.__new__(GroundingChecker)
    c.model = None
    verify_cache.clear_local()
    monkeypatch.setattr(settings, "grounding_cache_enabled", False, raising=False)
    return c


def _fake_verifier(calls):
    """FABRICATED for the invented duty, SUPPORTED for everything else."""
    def fake_batch(patches, source_md, kinds=None):
        calls.append({"claims": [c for c, _ in patches], "kinds": list(kinds or [])})
        return ["API documentation" not in claim for claim, _ in patches]
    return fake_batch


# ── 1. the summary is verified like a bullet ─────────────────────────────────

def test_summary_sentences_are_found_headed_and_unheaded():
    assert summary_sentences(TAILORED) == [
        "Backend engineer who builds Python services and keeps partner integrations "
        "healthy in production.", INVENTED]
    unheaded = ("# Alex Tenant\nalex@example.com | (555) 555-0100\n"
                "Backend engineer who builds Python services for partner teams.\n\n"
                "## Experience\n- Built REST APIs with FastAPI.")
    assert summary_sentences(unheaded) == [
        "Backend engineer who builds Python services for partner teams."]
    # Bullets, the name and the contact line are not summary sentences.
    assert not any("REST APIs" in s for s in summary_sentences(MASTER))
    # The opt-in relocation line is the user's profile setting, not a claim the
    # master must back (tailoring/relocation.py asks for it in the summary).
    with_reloc = MASTER.replace("in production.", "in production. Open to relocation to Chicago, IL.")
    assert not any("relocation" in s for s in summary_sentences(with_reloc))


def test_an_invented_summary_claim_fails_grounding(checker, monkeypatch):
    """Before: only Experience/Projects bullets were read, every bullet here is
    the master's own, so the check returned L0 and PASSED with no call."""
    calls: list = []
    monkeypatch.setattr(checker, "verify_batch", _fake_verifier(calls))
    result = checker.check(MASTER, TAILORED)
    assert result.passed is False
    assert result.flagged_bullets == []
    assert [f["bullet"] for f in result.flagged_summary] == [INVENTED]
    assert result.summary_checked == 1
    # The master's own summary sentence was not sent: only what changed is.
    assert calls == [{"claims": [INVENTED], "kinds": ["summary"]}]


def test_an_unchanged_summary_costs_nothing(checker, monkeypatch):
    calls: list = []
    monkeypatch.setattr(checker, "verify_batch", _fake_verifier(calls))
    result = checker.check(MASTER, MASTER)
    assert result.passed is True and result.llm_calls == 0 and calls == []


def test_summary_sentences_ride_in_the_same_request_as_bullets(checker, monkeypatch):
    """Cost stays bounded: one batched call, not one more per summary."""
    calls: list = []
    monkeypatch.setattr(checker, "verify_batch", _fake_verifier(calls))
    tailored = TAILORED.replace(
        "- Automated the release pipeline with GitHub Actions and Docker images on Kubernetes.",
        "- Shipped Kafka streaming for 40 partner feeds.")
    assert tailored != TAILORED
    result = checker.check(MASTER, tailored)
    assert result.llm_calls == 1 and len(calls) == 1
    assert calls[0]["kinds"].count("summary") == 1 and "bullet" in calls[0]["kinds"]
    assert [f["bullet"] for f in result.flagged_summary] == [INVENTED]


def test_the_batch_prompt_marks_summary_claims_and_their_stricter_rule(checker, monkeypatch):
    seen: dict = {}

    def fake_ask(prompt, *, system=None, max_tokens=10):
        seen["prompt"], seen["system"] = prompt, system
        return "1: SUPPORTED\n2: FABRICATED"

    monkeypatch.setattr(checker, "_ask_verifier", fake_ask)
    out = checker.verify_batch([("Built APIs.", "Built REST APIs."), (INVENTED, "")],
                               MASTER, kinds=["bullet", "summary"])
    assert out == [True, False]
    assert "1. CLAIM: Built APIs." in seen["prompt"]
    assert f"2. SUMMARY CLAIM: {INVENTED}" in seen["prompt"]
    assert "the whole master resume" in seen["prompt"]
    assert "SUMMARY CLAIMS" in seen["system"] and "clause by clause" in seen["system"]


def test_the_verifier_version_moved_with_the_prompt():
    """It is half the cache key: a verdict the old prompt gave is never served."""
    from app.tailoring.grounding import VERIFIER_VERSION
    assert VERIFIER_VERSION != "v2-batched-2026-09"


# ── removing the unbacked sentence ───────────────────────────────────────────

def test_removal_takes_out_only_the_unbacked_sentence():
    md, removed = remove_summary_sentences(TAILORED, [INVENTED], MASTER)
    assert removed == [INVENTED]
    assert "API documentation" not in md
    assert "keeps partner integrations healthy in production." in md
    assert md.count("## Experience") == 1 and "Built REST APIs" in md


def test_a_bold_opening_still_starts_a_new_sentence():
    draft = TAILORED.replace("Experienced translating", "**Experienced** translating")
    md, removed = remove_summary_sentences(draft, [INVENTED], MASTER)
    assert len(removed) == 1 and "API documentation" not in md
    assert "keeps partner integrations healthy in production." in md


def test_an_emptied_summary_falls_back_to_the_masters_own():
    draft = MASTER.replace(
        "Backend engineer who builds Python services and keeps partner integrations healthy in production.",
        INVENTED)
    md, removed = remove_summary_sentences(draft, [INVENTED], MASTER)
    assert removed == [INVENTED]
    assert "API documentation" not in md
    assert "Backend engineer who builds Python services" in md
    no_summary_master = MASTER.split("## Summary")[0] + "## Experience" + MASTER.split("## Experience")[1]
    md2, _ = remove_summary_sentences(draft, [INVENTED], no_summary_master)
    assert "## Summary" not in md2 and "## Experience" in md2


# ── end to end through tailor_for_application ────────────────────────────────

_U = "live-findings-tenant"


def _doctor():
    mod = types.ModuleType("app.tailoring.doctor")

    class _D:
        score, ats_coverage_pct, llm_verdict, human_score, passed = 95, 0.8, "", 90, True
        weak_bullets: list = []
        banned_found: list = []
        integrity_issues: list = []
        fingerprint_flags: list = []
        issues: list = []

    class ResumeDoctor:
        def check(self, resume_md, master, jd):
            return _D()
    mod.ResumeDoctor = ResumeDoctor
    mod.ACTION_VERBS = {"built", "diagnosed", "automated"}
    mod._METRIC_RE = re.compile(r"\d+")
    return mod


# Shaped like the live posting: the employer's name, "where", "them" and
# "customer" repeated far more often than any real skill.
JD = (
    "Horizon3 builds autonomous pentesting. "
    "At Horizon3 you ship integrations where it counts. "
    "Customers rely on them daily. "
    "Horizon3 integrations reach GraphQL endpoints where teams work. "
    "We hand them cybersecurity findings. "
    "Horizon3 engineers know Python. "
    "Every customer configures them alone. "
    "Docker images run where Kubernetes schedules them. "
    "Each customer matters to Horizon3. "
    "Integrations go where every customer needs them. "
    "Horizon3.ai culture welcomes cybersecurity people. "
    "Our customer culture is where Horizon3 started."
)
_JUNK = re.compile(r"\b(where|them|horizon3|customers?|culture|counts|alone)\b")


@pytest.fixture
def run_tailor(tmp_path, monkeypatch):
    from app.tailoring import tailor as tailor_mod

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "grounding_cache_enabled", False, raising=False)
    verify_cache.clear_local()

    def _clean():
        with get_session() as s:
            jids = list(s.exec(select(Job.id).where(Job.user_id == _U)).all())
            if jids:
                s.exec(delete(Application).where(Application.job_id.in_(jids)))
                s.exec(delete(Job).where(Job.id.in_(jids)))
            s.commit()

    def make(draft=TAILORED):
        _clean()
        with get_session() as s:
            job = Job(user_id=_U, source="greenhouse", external_id="lf-1",
                      title="Software Engineer, Integrations", company="Horizon3Ai",
                      url="https://x/lf", description=JD)
            s.add(job)
            s.commit()
            s.refresh(job)
            app = Application(user_id=_U, job_id=job.id, status=ApplicationStatus.SHORTLISTED)
            s.add(app)
            s.commit()
            s.refresh(app)
            aid = app.id
        monkeypatch.setattr(tailor_mod.Tailor, "__init__", lambda self: None, raising=False)
        monkeypatch.setattr(tailor_mod.Tailor, "tailor_resume", lambda self, *a, **k: draft,
                            raising=False)
        monkeypatch.setattr(tailor_mod.Tailor, "write_cover_letter",
                            lambda self, *a, **k: "Horizon3 needs reliable integrations.",
                            raising=False)
        monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: MASTER)
        monkeypatch.setattr(GroundingChecker, "__init__",
                            lambda self: setattr(self, "model", None))
        monkeypatch.setattr(GroundingChecker, "verify_batch",
                            lambda self, patches, src, kinds=None:
                            ["API documentation" not in c for c, _ in patches])
        monkeypatch.setitem(sys.modules, "app.tailoring.doctor", _doctor())
        resume_path, _ = tailor_mod.tailor_for_application(aid)
        report = json.loads((resume_path.parent / "report.json").read_text())
        md = (resume_path.parent / "resume.md").read_text()
        with get_session() as s:
            status = s.get(Application, aid).status
        return status, report, md

    yield make
    _clean()


def test_the_delivered_resume_never_carries_the_unbacked_summary_claim(run_tailor):
    status, report, md = run_tailor()
    assert "API documentation" not in md
    assert "keeps partner integrations healthy in production" in md
    assert status == ApplicationStatus.TAILORED
    assert report["grounding_status"] == "passed"
    assert report["summary_claims_removed"] == [INVENTED]


def test_if_the_summary_cannot_be_cleaned_the_draft_fails_like_a_bullet(run_tailor, monkeypatch):
    import app.tailoring.evidence as evidence

    def boom(*a, **k):
        raise RuntimeError("parser bug")
    monkeypatch.setattr(evidence, "remove_summary_sentences", boom)
    status, report, _ = run_tailor()
    assert status == ApplicationStatus.ERROR
    assert report["grounding_status"] == "failed"


def test_skills_to_learn_are_real_skills_not_stopwords_or_the_employer(run_tailor):
    """Before: 'horizon3 builds autonomous' and 'endpoints where teams' were
    offered as skills to learn (and as "I've used ..." buttons)."""
    _, report, _ = run_tailor(draft=MASTER)
    learn = [s.lower() for s in report["skills_to_learn"]]
    assert not [s for s in learn if _JUNK.search(s)], learn
    assert "graphql" in learn and "cybersecurity" in learn
    # The cover letter names the employer; it must not lose that sentence.
    assert not report["removed_claims"]


# ── 2. keyword extraction ────────────────────────────────────────────────────

def test_not_covered_lists_only_real_skills_tools_and_domains():
    from app.tailoring.ats_keywords import analyze, extract_jd_phrases
    phrases = extract_jd_phrases(JD, top_n=18, company="Horizon3Ai")
    assert not [p for p in phrases if _JUNK.search(p)], phrases
    assert {"graphql", "cybersecurity", "python", "kubernetes"} <= set(phrases), phrases
    rep = analyze(JD, MASTER, company="Horizon3Ai")
    assert not [p for p in rep.missing if _JUNK.search(p)], rep.missing
    assert "graphql" in rep.missing


def test_function_words_never_appear_even_without_a_company():
    from app.tailoring.ats_keywords import extract_jd_phrases
    phrases = extract_jd_phrases(JD, top_n=24)
    assert not {"where", "them", "customer", "customers", "culture"} & set(phrases)
    for p in phrases:
        assert not re.search(r"\b(where|them)\b", p), p


def test_a_word_written_once_in_lower_case_is_not_a_keyword():
    from app.tailoring.ats_keywords import extract_jd_phrases
    jd = "You will reach endpoints daily and work alone. Experience with Jira and Amazon Web Services."
    phrases = extract_jd_phrases(jd, top_n=18)
    assert not {"reach", "endpoints", "daily", "alone"} & set(phrases), phrases
    assert "jira" in phrases and "amazon web services" in phrases


def test_company_name_forms():
    from app.tailoring.ats_keywords import _company_terms, _names_company
    terms = _company_terms("Horizon3Ai")
    assert {"horizon3", "horizon3ai"} <= terms
    assert _names_company("horizon3", terms) and _names_company("horizon3 ai", terms)
    assert not _names_company("graphql", terms)
    # A curated technology stays a keyword at the company that makes it.
    assert not _names_company("mongodb", _company_terms("MongoDB, Inc."))
    # A short common prefix is not the company: "data" is not Datadog, and a
    # lone word of a longer name is not the company either.
    assert not _names_company("data pipelines", _company_terms("Datadog"))
    assert not _names_company("data modeling", _company_terms("Data Robot"))
    assert _names_company("data robot", _company_terms("Data Robot"))


def test_a_draft_naming_the_employer_is_not_withheld():
    """Before: "horizon3" was a skill the resume lacked, so a draft that named
    the employer was read as claiming it and the export gate withheld it."""
    from app.tailoring.export_gate import evaluate, reset_state
    reset_state()
    draft = MASTER.replace("in production.", "in production, the work Horizon3 needs.")
    v = evaluate(grounding_rejected=False, grounding_reason="", master=MASTER,
                 tailored=draft, jd=JD, company="Horizon3Ai")
    assert v.allowed, v.reason


# ── 3. the recruiter's read ──────────────────────────────────────────────────

def test_the_recruiters_read_loses_list_numbers_and_yes_dash_lead_ins():
    from app.tailoring.doctor import clean_verdict, tidy_verdict
    raw = (f"1. Yes {EM} strong keyword alignment with integration and API terms. "
           f"2. The biggest risk {EM} the summary claims connector work the roles never show.")
    out = clean_verdict(raw)
    assert out == ("Strong keyword alignment with integration and API terms. The biggest "
                   "risk, the summary claims connector work the roles never show.")
    assert EM not in out and not out.startswith("1.")
    assert tidy_verdict(f"No {EM} it lacks GraphQL.") == "It lacks GraphQL."
    assert tidy_verdict("Borderline - thin on GraphQL.") == "Borderline: thin on GraphQL."
    assert tidy_verdict("Yes, it would pass the screen.") == "Yes, it would pass the screen."


def test_the_verdict_prompt_no_longer_asks_for_a_numbered_list():
    import inspect

    from app.tailoring.doctor import ResumeDoctor
    src = inspect.getsource(ResumeDoctor._llm_verdict)
    assert "no numbering" in src
    assert "(yes/borderline/no + one reason)" not in src


# ── 1 + 5. the generator's instructions ──────────────────────────────────────

def test_the_prompt_bounds_the_summary_and_bridging_and_asks_for_varied_lengths():
    from app.tailoring.tailor import TAILOR_SYSTEM
    low = TAILOR_SYSTEM.lower()
    assert "the summary may only restate experience the master resume shows" in low
    assert "never asserts an activity" in low
    assert "vary bullet length" in low
    assert "is not a rewrite" in low
    assert EM not in TAILOR_SYSTEM
