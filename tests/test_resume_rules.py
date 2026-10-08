"""The owner's resume rules (2026-10-08) — every tailored resume follows them.

A friend's recipe that earned interview calls: fully rewritten for the job, a
natural voice, keywords woven in (never listed), strong verbs and real numbers,
nothing invented, no em dashes, the email and latest company exactly as on the
resume, the title under the name from the job posting when the user opts in,
a one-page PDF plus a Word file named YourName_Company_Resume with no AI or
tool traces in it, and every rule confirmed back to the user after the file is
made. What the model is asked lives in tailor.TAILOR_SYSTEM; what can be made
true without trusting it lives in app/tailoring/rules.py and is pinned here.

Not pinned, on purpose: "always 90+ ATS score on any tool". Third-party ATS
scores are proprietary, and on a job whose skills the resume lacks, 90 is only
reachable by claiming them. The honest version is pinned instead: the job
keywords the candidate genuinely has are in the document, or it is rebuilt.

Synthetic rows only, prefixed ``rr-`` and removed by that prefix.
"""
from __future__ import annotations

import json
import re
import sys
import types
import zipfile

import pytest
from sqlmodel import delete, select

from app.config import Settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, UserProfile
from app.tailoring import rules
from app.tailoring.evidence import fabrication_violations

EM = "—"


# ── no em dashes ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("before,after", [
    (f"Jun 2022 {EM} Mar 2024", "Jun 2022 - Mar 2024"),
    (f"Jan 2022{EM}Present", "Jan 2022 - Present"),
    (f"Cut costs {EM} and latency.", "Cut costs, and latency."),
    (f"{EM} Led the migration", "- Led the migration"),
    (f"10{EM}20 engineers", "10-20 engineers"),
    ("fast -- cheap", "fast, cheap"),
    (f"Results {EM}", "Results"),
])
def test_every_em_dash_goes(before, after):
    out, n = rules.scrub_em_dashes(before)
    assert out == after and n >= 1
    assert rules.count_em_dashes(out) == 0


def test_the_scrub_changes_no_fact():
    """A reformatted date range is the same fact; a changed year is not."""
    master = f"## Experience\n**Engineer** | Acme | Jun 2022 {EM} Mar 2024\n- Built 12 services"
    out, _ = rules.scrub_em_dashes(master)
    assert fabrication_violations(master, out) == []
    assert ("employment date", "jun 2021 - mar 2024") in fabrication_violations(
        master, out.replace("Jun 2022", "Jun 2021"))


def test_markdown_rules_and_hyphens_survive():
    text = "---\n- a bullet\nwell-known API\n## Skills"
    assert rules.scrub_em_dashes(text) == (text, 0)


# ── email and latest company ─────────────────────────────────────────────────

MASTER = ("# Jane Doe\nSoftware Engineer\nCincinnati, OH | jane.doe@gmail.com | (513) 555-0100\n\n"
          "## Experience\n**Software Engineer** | Acme Corp | Jan 2022 - Present\n"
          "- Built Python services handling 2M requests a day\n"
          "**Junior Developer** | Initech | Jun 2019 - Dec 2021\n- Maintained Django apps\n\n"
          "## Education\nB.S. Computer Science, Ohio State University, 2019")


@pytest.mark.parametrize("header,status", [
    ("Cincinnati, OH | jane.doe@gmail.com", "kept"),
    ("Cincinnati, OH | jane@company.com", "replaced"),
    ("Cincinnati, OH | (513) 555-0100", "restored"),
])
def test_the_email_is_the_one_on_the_resume(header, status):
    md = f"# Jane Doe\n{header}\n\n## Experience\n- x"
    out, got = rules.restore_email(md, MASTER)
    assert got == status
    assert rules.header_email(out) == "jane.doe@gmail.com"
    assert "jane@company.com" not in out


def test_no_email_on_the_resume_means_none_added():
    out, got = rules.restore_email("# Jane\nCincinnati, OH\n## X", "# Jane\n## X")
    assert got == "no_master_email" and "@" not in out


def test_latest_company_is_read_from_the_resume():
    assert rules.latest_role(MASTER) == {"title": "Software Engineer", "org": "Acme Corp",
                                         "dates": "Jan 2022 - Present"}
    assert rules.employer_kept("…**Software Engineer** | Acme Corp | …", "Acme Corp")
    assert not rules.employer_kept("…**Software Engineer** | Acme Inc | …", "Acme Corp")


# ── the title under the name ─────────────────────────────────────────────────

def test_the_resumes_own_title_is_found():
    assert rules.master_headline(MASTER) == "Software Engineer"
    plain = "JANE DOE\nData Analyst\njane@x.com | 555-123-4567\nSUMMARY\nAnalyst with five years."
    assert rules.master_headline(plain) == "Data Analyst"
    assert rules.master_headline("# Jane\nCincinnati, OH | jane@x.com\n## Summary") == ""
    assert rules.master_headline("# Jane\nBuilt systems for ten years.\n## X") == ""


@pytest.mark.parametrize("title,clean", [
    ("Senior Backend Engineer (Remote) - R12345", "Senior Backend Engineer"),
    ("Software Engineer - Austin, TX", "Software Engineer"),
    ("Software Engineer II", "Software Engineer II"),
    ("Machine Learning Engineer, Platform", "Machine Learning Engineer, Platform"),
    ("Engineer 3 | Payments", "Engineer"),
])
def test_a_posting_title_becomes_a_clean_headline(title, clean):
    assert rules.sanitize_jd_title(title) == clean
    assert not re.search(r"\d", rules.sanitize_jd_title(title)), "numbers are claims"


def test_the_headline_is_set_replaced_or_removed_never_a_held_title():
    md = ("# Jane Doe\nData Wizard\nCincinnati, OH | jane.doe@gmail.com\n\n## Experience\n"
          "**Software Engineer** | Acme Corp | Jan 2022 - Present\n- Built things")
    on = rules.apply_headline(md, "Backend Engineer")
    assert on.splitlines()[1] == "Backend Engineer" and "Data Wizard" not in on
    assert "**Software Engineer** | Acme Corp" in on, "a held title never changes"
    off = rules.apply_headline(md, "")
    assert off.splitlines()[1].startswith("Cincinnati") and "Data Wizard" not in off


def test_the_prompt_names_what_to_copy_and_how_to_frame_the_role():
    on = rules.prompt_block(MASTER, "Backend Engineer (Remote)", title_from_jd=True)
    assert "jane.doe@gmail.com" in on and '"Acme Corp"' in on
    assert "applying as a Backend Engineer" in on and "without claiming they held" in on
    off = rules.prompt_block(MASTER, "Backend Engineer", title_from_jd=False)
    assert "applying as" not in off and "titles they actually held" in off


# ── keywords: woven, never stuffed; the honest coverage number ───────────────

JD = ("We need a backend engineer with Python, PostgreSQL, Docker and Kubernetes "
      "experience. Python and PostgreSQL daily; Docker for deploys; REST APIs.")


def test_coverage_counts_only_keywords_the_candidate_has():
    master = "## Skills\nPython, PostgreSQL\n## Experience\n- Built REST APIs in Python on PostgreSQL"
    full = rules.keyword_coverage(master, master, JD)
    assert full["achievable"] >= 2 and full["kept_pct"] == 100
    dropped = rules.keyword_coverage(master, "## Experience\n- Built services", JD)
    assert dropped["kept_pct"] < 90 and dropped["missing_achievable"]
    assert "kubernetes" not in [p.lower() for p in dropped["missing_achievable"]], \
        "a skill the resume lacks is never 'missing' from it"


def test_a_keyword_dump_is_caught():
    md = ("## Summary\nPython, PostgreSQL, Docker, Kubernetes, REST APIs\n"
          "## Skills\nPython, PostgreSQL, Docker")
    assert rules.keyword_stuffing(md, JD)
    woven = "## Summary\nBuilds Python services on PostgreSQL and ships them with Docker."
    assert not rules.keyword_stuffing(woven, JD)


# ── the prompt and the defaults ──────────────────────────────────────────────

def test_the_prompt_carries_every_rule_and_no_em_dash():
    from app.tailoring.tailor import COVER_SYSTEM, TAILOR_SYSTEM
    assert EM not in TAILOR_SYSTEM
    for must in ("fully rewrite and restructure", "natural human voice", "never use the em dash",
                 "exactly as the master resume gives them", "most recent employer",
                 'never add a "keywords" line', "action verb", "Never invent, round or inflate a number",
                 "ONE PAGE", "WORD CHOICE", "Rule 2 still wins"):
        assert must.lower() in TAILOR_SYSTEM.lower(), must
    assert "touch roughly a third" not in TAILOR_SYSTEM
    assert "em dash" in COVER_SYSTEM


def test_every_tailor_is_a_full_rewrite_by_default():
    assert Settings.model_fields["tailor_skip_coverage_pct"].default == 0.0


def test_strong_verbs_are_no_longer_an_ai_tell():
    from app.tailoring.doctor import ResumeDoctor
    bullets = ["Built a billing service", "Designed the schema", "Migrated the queue",
               "Automated the deploys", "Debugged the cache layer"]
    flags, _ = ResumeDoctor()._fingerprint_flags(bullets, "\n".join(f"- {b}" for b in bullets))
    assert not any("action verb" in f for f in flags)


# ── the fabrication guard against a plain-text resume ────────────────────────

PLAIN = ("JANE DOE\njane.doe@gmail.com\nEXPERIENCE\nData Analyst, Acme Corp, Jun 2022 - Present\n"
         "Built dashboards.\nEDUCATION\nB.S. Statistics")


def test_a_restructured_plain_text_resume_is_not_an_invention():
    """Every PDF/DOCX upload is plain text; writing its roles as
    "**Title** | Employer | dates" reported every one of them invented."""
    out = "## Experience\n**Data Analyst** | Acme Corp | Jun 2022 - Present\n- Built dashboards."
    assert fabrication_violations(PLAIN, out) == []


@pytest.mark.parametrize("line,kind", [
    ("**Senior Data Analyst** | Acme Corp | Jun 2022 - Present", "job title"),
    ("**Data Analyst** | Globex | Jun 2022 - Present", "employer"),
])
def test_a_renamed_company_or_upgraded_title_still_is(line, kind):
    assert kind in [k for k, _ in fabrication_violations(PLAIN, f"## Experience\n{line}")]


def test_a_structured_resume_keeps_the_strict_comparison():
    """The fallback is for plain-text masters only: against a structured one,
    an employer named only in a bullet is still not an employer."""
    master = "**Engineer** | Acme | Jan 2020 - Present\n- Integrated the Google Maps API"
    out = "**Engineer** | Google | Jan 2020 - Present\n- Integrated the Google Maps API"
    assert ("employer", "google") in fabrication_violations(master, out)


# ── trace scan ───────────────────────────────────────────────────────────────

def test_the_trace_scan_reads_metadata_not_the_resume(tmp_path):
    from docx import Document
    p = tmp_path / "x.docx"
    d = Document()
    d.add_paragraph("Skills: Claude, Python")          # a real skill: not a trace
    d.save(p)
    assert "python-docx" in rules.file_traces(p)        # the library's default metadata
    assert "claude" not in rules.file_traces(p)
    assert rules.file_traces(tmp_path / "missing.docx") is None


# ── end to end: tailor_for_application ───────────────────────────────────────

_U = "rr-tenant"


def _grounding(passed=True):
    mod = types.ModuleType("app.tailoring.grounding")

    class _R:
        def __init__(self):
            self.passed, self.flagged_bullets, self.confidence_map = passed, [], {"b": 0.9}

    class GroundingChecker:
        def check(self, master, tailored, *, use_cache=True):
            return _R()
    mod.GroundingChecker = GroundingChecker
    return mod


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
    mod.ACTION_VERBS = {"built", "designed", "migrated", "led", "shipped"}
    mod._METRIC_RE = re.compile(r"\d+")
    return mod


DRAFT = (f"# Jane Doe\nPython Ninja\nCincinnati, OH | jane@wrong.example | (513) 555-0100\n\n"
         f"## Summary\nBackend engineer {EM} Python services on PostgreSQL.\n\n"
         f"## Experience\n**Software Engineer** | Acme Corp | Jan 2022 {EM} Present\n"
         f"- Built Python services handling 2M requests a day\n"
         f"**Junior Developer** | Initech | Jun 2019 {EM} Dec 2021\n- Maintained Django apps\n\n"
         f"## Education\nB.S. Computer Science, Ohio State University, 2019")


@pytest.fixture
def tailored(tmp_path, monkeypatch):
    from app.config import settings
    from app.tailoring import tailor as tailor_mod

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)

    def make(*, title_from_jd=True, first="Jane", last="Doe", job_title="Backend Engineer (Remote)",
             company="Globex Corporation, Inc.", jd=JD, draft=DRAFT):
        with get_session() as s:
            jids = list(s.exec(select(Job.id).where(Job.user_id == _U)).all())
            if jids:
                s.exec(delete(Application).where(Application.job_id.in_(jids)))
                s.exec(delete(Job).where(Job.id.in_(jids)))
            s.exec(delete(UserProfile).where(UserProfile.user_id == _U))
            s.add(UserProfile(user_id=_U, first_name=first, last_name=last,
                              location="Cincinnati, OH", resume_title_from_jd=title_from_jd))
            job = Job(user_id=_U, source="greenhouse", external_id="rr-1", title=job_title,
                      company=company, url="https://x/1", description=jd)
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
                            lambda self, *a, **k: f"Dear team {EM} hello.", raising=False)
        monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: MASTER)
        monkeypatch.setitem(sys.modules, "app.tailoring.grounding", _grounding())
        monkeypatch.setitem(sys.modules, "app.tailoring.doctor", _doctor())
        resume_path, _ = tailor_mod.tailor_for_application(aid)
        report = json.loads((resume_path.parent / "report.json").read_text())
        md = (resume_path.parent / "resume.md").read_text()
        return aid, resume_path, report, md

    yield make
    with get_session() as s:
        jids = list(s.exec(select(Job.id).where(Job.user_id == _U)).all())
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(Job).where(Job.id.in_(jids)))
        s.exec(delete(UserProfile).where(UserProfile.user_id == _U))
        s.commit()


def test_a_tailored_resume_follows_every_rule(tailored):
    aid, resume_path, report, md = tailored()
    assert rules.count_em_dashes(md) == 0
    assert rules.header_email(md) == "jane.doe@gmail.com"
    assert md.splitlines()[1] == "Backend Engineer", "the posting's title, opted in"
    assert "**Software Engineer** | Acme Corp" in md, "held titles and the company untouched"
    assert resume_path.name == "Jane_Doe_Globex_Resume.docx"
    pdf = resume_path.with_suffix(".pdf")
    assert pdf.exists() and report["resume_pdf"] == pdf.name
    assert report["page_count"] == 1
    assert rules.file_traces(resume_path) == [] and rules.file_traces(pdf) == []
    with zipfile.ZipFile(resume_path) as z:
        body = z.read("word/document.xml").decode()
    assert EM not in body
    rows = {r["key"]: r for r in report["rules_checklist"]}
    for key in ("rewrite", "email", "employer", "truthful", "em_dashes", "one_page",
                "files", "filename", "metadata", "keywords_woven"):
        assert rows[key]["ok"] is True, (key, rows[key])
    assert rows["title"]["detail"].endswith("(from the job posting)")
    assert "Cincinnati, OH" in rows["location"]["detail"]
    with get_session() as s:
        assert s.get(Application, aid).status == ApplicationStatus.TAILORED


def test_off_keeps_the_resumes_own_title(tailored):
    _aid, _p, report, md = tailored(title_from_jd=False)
    assert md.splitlines()[1] == "Software Engineer"
    assert report["headline_source"] == "resume"


def test_a_posting_title_naming_a_missing_skill_is_never_used(tailored):
    jd = "Rust engineer. You will write Rust services every day; Rust and Python required."
    _aid, _p, report, md = tailored(job_title="Senior Rust Engineer", jd=jd)
    assert "Rust" not in md.splitlines()[1]
    assert report["headline_source"] == "resume"


def test_a_tenant_without_a_name_never_gets_the_founders(tailored, monkeypatch):
    from app.tailoring import tailor as tailor_mod
    monkeypatch.setattr(tailor_mod.qa_resolver, "data",
                        {"identity": {"first_name": "Founder", "last_name": "Person"}})
    _aid, resume_path, _r, _md = tailored(first="", last="")
    assert "Founder" not in resume_path.name
    assert resume_path.name.startswith("Candidate_")


# ── the profile option and the dashboard ─────────────────────────────────────

def test_the_option_is_off_unless_chosen_and_round_trips():
    assert UserProfile.model_fields["resume_title_from_jd"].default is False
    from app.api.server import ProfileUpdate
    assert ProfileUpdate(resume_title_from_jd="true").resume_title_from_jd is True


def test_the_dashboard_offers_the_pdf_and_shows_the_checklist():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app/templates/dashboard.html").read_text()
    assert 'name="resume_title_from_jd"' in html
    assert 'id="ts-download-pdf"' in html and 'id="modal-download-pdf-btn"' in html
    assert "download-resume?format=pdf" in html
    assert "_tsRulesChecklist(q.rules_checklist" in html
