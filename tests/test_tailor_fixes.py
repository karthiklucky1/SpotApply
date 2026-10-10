"""Tailoring hardening, 2026-10-10 — the audit the founder asked for ("make it
perfect"). Each test pins one defect found by measurement or by reading the
pipeline end to end:

* A PDF or DOCX upload is read back as PLAIN TEXT, and the inventory parser
  only knew markdown. The founder's own résumé rendered to PDF and read back
  the way Storage returns it parsed to ZERO roles and "none on the resume" for
  tenure (3 of 3 files). Tenure feeds the reranker's seniority rules, the
  RuleFilter and the pre-download review.
* A year-only range ("2019 - 2022") was not a date to the fabrication guard,
  so it read as an EMPLOYER — and a draft that re-spaced it was blocked for
  inventing one.
* The cover letter's on-disk metadata header reached the extension and the
  drawer.
* The onboarding stub (resume.md) was read ahead of a later PDF upload, and
  the stub was re-written on every onboarding save.
* The extension route charged a tailor credit for a draft it then refused.
* The rebuild loop shipped the LAST draft, not the best one, and a style-only
  Doctor failure (banned words, weak bullets) parked a truthful résumé at
  ERROR: 8 of the 19 drafts production ever refused were that.

Rows this file writes carry the ``tailorfix-`` prefix and are removed by it.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import delete, select

from app.tailoring import inventory as inv_mod
from app.tailoring.inventory import ACADEMIC, INTERNSHIP, PROFESSIONAL, build_inventory

NOW = 2026 * 12 + 10


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    monkeypatch.setattr(inv_mod, "_current_month_index", lambda: NOW)


# ── 1. the inventory reads the shapes an uploaded résumé actually has ────────

MD_SHAPE = """# Jane Doe
## Experience
**Software Engineer** | Acme Corp | Jan 2022 - Present | Columbus, OH
- Built the billing service in Python and FastAPI serving 2,000 customers.
**Software Engineering Intern** | Beta LLC | Jun 2021 - Aug 2021 | Remote
- Wrote integration tests for the payments API.
## Education
**B.S. Computer Science** | Ohio State University | Aug 2017 - May 2021
## Skills
Python, FastAPI, SQL
"""

# What pypdf returns for the same résumé: no `#`, no `**`, bullets as glyphs.
PDF_TEXT_SHAPE = """JANE DOE
Columbus, OH | jane@example.com | (614) 555-0100
PROFESSIONAL EXPERIENCE
Software Engineer | Acme Corp | Jan 2022 - Present | Columbus, OH
• Built the billing service in Python and FastAPI serving 2,000 customers.
Software Engineering Intern | Beta LLC | Jun 2021 - Aug 2021 | Remote
• Wrote integration tests for the payments API.
EDUCATION
B.S. Computer Science | Ohio State University | Aug 2017 - May 2021
TECHNICAL SKILLS
Python, FastAPI, SQL
"""

# Title Case section names, an en dash, no glyphs at all (python-docx text).
DOCX_TEXT_SHAPE = """Jane Doe
Work Experience
Software Engineer | Acme Corp | Jan 2022 – Present | Columbus, OH
Built the billing service in Python and FastAPI serving 2,000 customers.
Software Engineering Intern | Beta LLC | Jun 2021 – Aug 2021 | Remote
Wrote integration tests for the payments API.
Education
B.S. Computer Science | Ohio State University | Aug 2017 – May 2021
Skills
Python, FastAPI, SQL
"""

# Letter-spaced small-caps headings, as some PDF fonts extract.
SMALL_CAPS_SHAPE = PDF_TEXT_SHAPE.replace("PROFESSIONAL EXPERIENCE", "E X P E R I E N C E") \
                                 .replace("EDUCATION", "E D U C A T I O N") \
                                 .replace("TECHNICAL SKILLS", "S K I L L S")


def _kinds(inv):
    return sorted(e.kind for e in inv.engagements)


@pytest.mark.parametrize("shape", [PDF_TEXT_SHAPE, DOCX_TEXT_SHAPE, SMALL_CAPS_SHAPE],
                         ids=["pdf-text", "docx-text", "small-caps"])
def test_plain_text_reads_the_same_as_markdown(shape):
    truth = build_inventory(MD_SHAPE)
    got = build_inventory(shape)
    assert truth.employment_months > 0 and truth.internship_months > 0
    assert got.employment_months == truth.employment_months
    assert got.internship_months == truth.internship_months
    assert _kinds(got) == _kinds(truth) == [ACADEMIC, INTERNSHIP, PROFESSIONAL]
    acme = next(e for e in got.engagements if e.kind == PROFESSIONAL)
    assert (acme.title, acme.org) == ("Software Engineer", "Acme Corp")


def test_the_founders_resume_survives_its_own_pdf_and_docx():
    """The measurement that found the defect, kept as the guard: render the
    markdown master the way the tailor does, read the files back the way the
    Storage loader does, and the inventory must agree with the markdown."""
    import io
    import tempfile

    from app.matching.pipeline import resume_text_from_bytes
    from app.tailoring.render import TIERS, fit_one_page, write_docx

    md_path = Path(__file__).resolve().parents[1] / "data" / "profiles" / "backend.md"
    md = md_path.read_text(encoding="utf-8")
    truth = build_inventory(md)
    assert truth.employment_months >= 36 and len(truth.engagements) == 2
    fit = fit_one_page(md, jd_text="", author="A B", title="Resume")
    pdf_text = resume_text_from_bytes("resume.pdf", fit.pdf_bytes)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "resume.docx"
        write_docx(fit.md, p, TIERS[fit.tier_index], author="A B", title="Resume")
        docx_text = resume_text_from_bytes("resume.docx", p.read_bytes())
    for text in (pdf_text, docx_text):
        got = build_inventory(text)
        assert got.employment_months == truth.employment_months
        assert [(e.title, e.org) for e in got.engagements] == \
               [(e.title, e.org) for e in truth.engagements]
    del io


THREE_LINE_SHAPE = """Experience
Senior Data Analyst
Globex Corporation, Chicago, IL
March 2020 - February 2023
- Led a 6-person team from Jan 2020 - Mar 2021 to deliver the reporting platform.
Data Analyst
Initech
2018 - 2020
- Built dashboards for the finance team.
Education
Bachelor of Science in Statistics, University of Illinois
2014 - 2018
"""


def test_title_org_and_dates_on_three_lines():
    inv = build_inventory(THREE_LINE_SHAPE)
    roles = [(e.kind, e.title, e.org) for e in inv.engagements]
    assert roles == [
        (PROFESSIONAL, "Senior Data Analyst", "Globex Corporation"),
        (PROFESSIONAL, "Data Analyst", "Initech"),
        (ACADEMIC, "Bachelor of Science in Statistics", "University of Illinois"),
    ]
    # Mid-2018 → Feb 2023, concurrency counted once; the degree counts for nothing.
    assert inv.employment_months == inv_mod.merged_months(
        [(2018 * 12 + 6, 2020 * 12 + 6), (2020 * 12 + 3, 2023 * 12 + 2)])
    assert inv.approximate is True          # the year-only Initech dates


def test_a_bullet_mentioning_a_date_range_is_not_a_role():
    inv = build_inventory(THREE_LINE_SHAPE)
    assert not any("6-person" in e.title or "6-person" in e.org for e in inv.engagements)


SUBHEADING_SHAPE = """## Experience
### Backend Engineer | Acme | 2019 - 2022
- Built the ingestion pipeline in Python.
### Platform Engineer, Beta Inc
Jan 2023 - Present
- Moved the fleet to Kubernetes.
## Education
### B.S. Computer Science, Ohio State University
2015 - 2019
"""


def test_role_subheadings_are_roles_and_a_degree_subheading_is_not_a_job():
    inv = build_inventory(SUBHEADING_SHAPE)
    roles = [(e.kind, e.title, e.org) for e in inv.engagements]
    assert roles == [
        (PROFESSIONAL, "Backend Engineer", "Acme"),
        (PROFESSIONAL, "Platform Engineer", "Beta Inc"),
        (ACADEMIC, "B.S. Computer Science", "Ohio State University"),
    ]
    assert inv.employment_months == inv_mod.merged_months(
        [(2019 * 12 + 6, 2022 * 12 + 6), (2023 * 12 + 1, NOW)])


ONE_LINE_SHAPE = """EXPERIENCE
Acme Corp — Software Engineer (June 2019 - August 2021)
Developed the ingestion pipeline handling 10,000 records per hour.
Reduced latency by 40% across services.
Beta Inc — Senior Software Engineer (September 2021 - Present)
Led the migration to Kubernetes for twelve services.
EDUCATION
Ohio State University — B.S. Computer Science (2015 - 2019)
"""


def test_employer_title_and_dates_on_one_line():
    inv = build_inventory(ONE_LINE_SHAPE)
    assert _kinds(inv) == [ACADEMIC, PROFESSIONAL, PROFESSIONAL]
    assert inv.employment_months == inv_mod.merged_months([(2019 * 12 + 6, NOW)])


def test_a_degree_line_is_academic_even_with_no_sections_at_all():
    inv = build_inventory("Software Engineer | Acme | 2020 - 2023\n"
                          "B.S. in Computer Science, Ohio State University | 2016 - 2020\n")
    assert _kinds(inv) == [ACADEMIC, PROFESSIONAL]
    assert inv.employment_months == inv_mod.merged_months([(2020 * 12 + 6, 2023 * 12 + 6)])


def test_a_role_title_containing_a_section_word_is_not_a_section():
    text = """EXPERIENCE
Project Manager
Acme Corp | 2020 - 2023
- Delivered the data platform programme on time.
Software Engineering Intern
Beta | Jun 2019 - Aug 2019
- Built the reporting job.
"""
    inv = build_inventory(text)
    roles = [(e.kind, e.title, e.org) for e in inv.engagements]
    assert roles == [(PROFESSIONAL, "Project Manager", "Acme Corp"),
                     (INTERNSHIP, "Software Engineering Intern", "Beta")]


@pytest.mark.parametrize("line", [
    "EXPERIENCE", "Work Experience", "PROFESSIONAL EXPERIENCE:", "— Skills —",
    "Education", "TECHNICAL SKILLS", "Projects", "E X P E R I E N C E",
])
def test_bare_section_names(line):
    assert inv_mod._bare_section(line) is not None


@pytest.mark.parametrize("line", [
    "Project Manager", "Software Engineering Intern", "Research Assistant",
    "Experience with Kubernetes and Docker", "Tools: Git, Docker",
    "- Built the billing service.", "Senior Software Engineer",
])
def test_lines_that_are_not_section_names(line):
    assert inv_mod._bare_section(line) is None


def test_the_markdown_fixtures_other_files_rely_on_still_parse_identically():
    """The markdown path is unchanged: pipe headers, kinds and totals."""
    from tests.test_evidence_inventory import MASTER
    inv = build_inventory(MASTER)
    assert inv.employment_months > 0
    assert any(e.kind == INTERNSHIP for e in inv.engagements)


# ── 2. year-only ranges are dates to the fabrication guard ───────────────────

def test_a_respaced_year_only_range_is_not_an_invented_employer():
    from app.tailoring.evidence import fabrication_violations
    master = "## Experience\n**Engineer** | Acme | 2019 - 2022\n- Built the API.\n"
    draft = "## Experience\n**Engineer** | Acme | 2019-2022\n- Built the API.\n"
    assert fabrication_violations(master, draft) == []


def test_a_stretched_year_only_range_is_still_caught():
    from app.tailoring.evidence import fabrication_violations
    master = "## Experience\n**Engineer** | Acme | 2019 - 2022\n- Built the API.\n"
    draft = "## Experience\n**Engineer** | Acme | 2018 - 2022\n- Built the API.\n"
    assert ("employment date", "2018 - 2022") in fabrication_violations(master, draft)


def test_a_graduation_year_is_not_an_employer():
    from app.tailoring.evidence import extract_facts
    facts = extract_facts("## Education\n**Bachelor of Science** | 2019\nUniversity of Texas\n")
    assert "2019" not in facts.employers


def test_a_year_only_date_line_is_structure_not_a_claim():
    from app.tailoring.evidence import _is_structural
    assert _is_structural("2019 - 2022")
    assert _is_structural("2021 - Present")


# ── 3. the cover letter body is the letter ───────────────────────────────────

def test_cover_body_strips_the_on_disk_header():
    from app.api.server import _cover_body
    raw = ("Company:   Acme\nRole:      Engineer\nPosted:    unknown\n"
           "URL:       https://x\n\n---COVER---\n\nDear Hiring Team,\nI build APIs.\n")
    assert _cover_body(raw) == "Dear Hiring Team,\nI build APIs."
    assert _cover_body("Dear team,\nplain letter\n") == "Dear team,\nplain letter"
    assert _cover_body("") == ""


def test_both_cover_readers_use_the_one_helper():
    import inspect
    import re

    from app.api import server
    src = inspect.getsource(server)
    reads = [m.start() for m in re.finditer(r"cover_letter_path\)\.read_text\(", src)]
    assert len(reads) >= 2, "the cover-letter reads moved — point this test at them"
    for pos in reads:
        assert "_cover_body(" in src[max(0, pos - 160):pos], src[pos - 160:pos + 40]


# ── 4. one master résumé, the newest one ─────────────────────────────────────

class _Bucket:
    def __init__(self, files, blobs=None, fail_list=False):
        self._files, self._blobs, self._fail = files, blobs or {}, fail_list
        self.removed = []

    def list(self, prefix):
        if self._fail:
            raise RuntimeError("storage down")
        return list(self._files)

    def download(self, key):
        return self._blobs[key]

    def remove(self, keys):
        self.removed.extend(keys)


class _SB:
    def __init__(self, bucket):
        self.storage = SimpleNamespace(from_=lambda name: bucket)


def test_resume_objects_are_listed_newest_first():
    from app.matching.pipeline import list_resume_objects
    files = [{"name": "resume.md", "updated_at": "2026-10-01T10:00:00Z"},
             {"name": "resume.pdf", "updated_at": "2026-10-01T10:05:00Z"},
             {"name": "other.txt", "updated_at": "2026-10-02T10:00:00Z"}]
    assert list_resume_objects(_SB(_Bucket(files)), "u1") == ["resume.pdf", "resume.md"]


def test_a_tie_prefers_the_uploaded_document_over_the_stub():
    from app.matching.pipeline import list_resume_objects
    files = [{"name": "resume.md", "updated_at": "T"}, {"name": "resume.pdf", "updated_at": "T"}]
    assert list_resume_objects(_SB(_Bucket(files)), "u1")[0] == "resume.pdf"


def test_a_failed_listing_is_none_not_empty():
    from app.matching.pipeline import list_resume_objects
    assert list_resume_objects(_SB(_Bucket([], fail_list=True)), "u1") is None
    assert list_resume_objects(_SB(_Bucket([])), "u1") == []


def test_the_scorer_reads_the_newest_resume(monkeypatch):
    import app.db.supabase_client as sc
    from app.matching import pipeline as pl
    files = [{"name": "resume.md", "updated_at": "2026-10-01T10:00:00Z"},
             {"name": "resume.txt", "updated_at": "2026-10-03T10:00:00Z"}]
    bucket = _Bucket(files, blobs={"u1/resume.md": b"# stub", "u1/resume.txt": b"the real one"})
    monkeypatch.setattr(sc, "service_client", lambda: _SB(bucket))
    assert pl._fetch_resume_from_storage("u1") == "the real one"


def test_no_resume_raises_without_guessing(monkeypatch):
    import app.db.supabase_client as sc
    from app.matching import pipeline as pl
    monkeypatch.setattr(sc, "service_client", lambda: _SB(_Bucket([])))
    with pytest.raises(ValueError):
        pl._fetch_resume_from_storage("u1")


def test_the_upload_route_removes_the_other_extensions():
    import inspect

    from app.api import server
    src = inspect.getsource(server.upload_resume)
    assert "RESUME_EXTENSIONS if e != ext" in src and '.remove(stale)' in src


def test_synthesize_keeps_a_resume_already_on_file(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import server
    monkeypatch.setattr(server, "_user_has_resume", lambda uid: True)
    called = []
    monkeypatch.setattr("builtins.open", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
        AssertionError("the stub must not be written over a real resume")))
    client = TestClient(server.app)
    r = client.post("/api/resume/synthesize")
    assert r.status_code == 200 and r.json().get("skipped") is True
    assert not called


# ── 5. the extension route charges only a delivered draft ────────────────────

_PREFIX = "tailorfix-"


@pytest.fixture()
def seeded():
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus, Job, JobSource
    with get_session() as s:
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id=_PREFIX + "job",
                  company="TailorFix Co", title="Backend Engineer", url="https://x/tf",
                  description="Python backend role.")
        s.add(job)
        s.commit()
        s.refresh(job)
        app_row = Application(job_id=job.id, status=ApplicationStatus.SHORTLISTED,
                              apply_track="manual")
        s.add(app_row)
        s.commit()
        s.refresh(app_row)
        ids = (job.id, app_row.id)
    yield ids
    with get_session() as s:
        s.exec(delete(Application).where(Application.job_id == ids[0]))
        s.exec(delete(Job).where(Job.id == ids[0]))
        s.commit()


def _route_for(server, name):
    return next(r.path for r in server.app.routes if getattr(r, "name", "") == name)


def test_a_refused_draft_is_not_charged_on_the_extension_route(monkeypatch, seeded, tmp_path):
    from fastapi.testclient import TestClient

    from app.api import server
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus
    import app.tailoring.tailor as tailor_mod
    _, app_id = seeded
    charged = []
    monkeypatch.setattr(server, "_increment_tailor", lambda uid: charged.append(uid))
    monkeypatch.setattr(server, "_autofill_resume_source", lambda session, uid: "tailored")
    monkeypatch.setattr(server, "_base_resume_bytes", lambda uid: None)

    def refusing_tailor(application_id, instruction=None):
        with get_session() as s:
            row = s.get(Application, application_id)
            row.status = ApplicationStatus.ERROR
            row.notes = "Grounding check failed. Flagged bullets:\n- invented"
            s.add(row)
            s.commit()
        p = tmp_path / "Resume.docx"
        p.write_bytes(b"x")
        return p, tmp_path / "Cover.txt"

    monkeypatch.setattr(tailor_mod, "tailor_for_application", refusing_tailor)
    client = TestClient(server.app)
    r = client.get(_route_for(server, "get_tailored_resume").format(application_id=app_id))
    assert r.status_code == 409
    assert charged == []


def test_a_delivered_draft_is_charged_once(monkeypatch, seeded, tmp_path):
    from fastapi.testclient import TestClient

    from app.api import server
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus
    import app.tailoring.tailor as tailor_mod
    _, app_id = seeded
    charged = []
    monkeypatch.setattr(server, "_increment_tailor", lambda uid: charged.append(uid))
    monkeypatch.setattr(server, "_autofill_resume_source", lambda session, uid: "tailored")
    monkeypatch.setattr(server, "_export_verdict",
                        lambda application_id, **k: SimpleNamespace(blocked=False, reason=""))

    def delivering_tailor(application_id, instruction=None):
        p = tmp_path / "Jane_Doe_Resume.docx"
        p.write_bytes(b"PK\x03\x04docx")
        with get_session() as s:
            row = s.get(Application, application_id)
            row.status = ApplicationStatus.TAILORED
            row.tailored_resume_path = str(p)
            s.add(row)
            s.commit()
        return p, tmp_path / "Cover.txt"

    monkeypatch.setattr(tailor_mod, "tailor_for_application", delivering_tailor)
    client = TestClient(server.app)
    r = client.get(_route_for(server, "get_tailored_resume").format(application_id=app_id))
    assert r.status_code == 200, r.text
    assert r.json().get("base64")                 # the document itself was handed over
    assert charged == ["local"]


# ── 6. best draft wins; style never withholds, integrity always does ─────────

def _attempt(**over):
    base = {k: None for k in __import__("app.tailoring.tailor", fromlist=["_ATTEMPT_FIELDS"])._ATTEMPT_FIELDS}
    base.update({"fabrications": [], "grounding_failed": False, "doctor_integrity": [],
                 "doctor_failed": False, "human_failed": False, "coverage_short": False,
                 "doctor_score": 70, "keyword_cov": {"kept_pct": 100}})
    base.update(over)
    return base


def test_the_best_shippable_attempt_is_chosen_not_the_last():
    from app.tailoring.tailor import _best_attempt
    a = _attempt(resume_md="A", doctor_failed=True, doctor_score=60)
    b = _attempt(resume_md="B", doctor_failed=True, doctor_score=50)
    assert _best_attempt([a, b]) is a


def test_a_passing_draft_beats_a_higher_scoring_failed_one():
    from app.tailoring.tailor import _best_attempt
    a = _attempt(resume_md="A", doctor_failed=False, doctor_score=66)
    b = _attempt(resume_md="B", doctor_failed=True, doctor_score=80)
    assert _best_attempt([a, b]) is a


def test_an_unshippable_draft_never_wins_whatever_its_score():
    from app.tailoring.tailor import _best_attempt
    a = _attempt(resume_md="A", fabrications=[("employer", "Globex")], doctor_score=95)
    b = _attempt(resume_md="B", doctor_failed=True, doctor_score=40)
    assert _best_attempt([a, b]) is b
    assert _best_attempt([a]) is None


def test_the_quality_note_names_the_findings_without_the_score_line():
    from app.tailoring.tailor import _quality_note
    note = _quality_note(58, "Doctor score=58/100.\nBanned words found: dynamic\n"
                             "4/12 bullets have neither impact verb nor metric", 2)
    assert note.startswith("⚠ Quality check: 58/100 after 2 drafts.")
    assert "Banned words found: dynamic" in note and "Doctor score=" not in note
    assert "—" not in note


# A full run of tailor_for_application with the model and the verifier stubbed.
MASTER = """# Karthik Test
## Summary
- Python engineer with 5 years experience

## Experience
### Senior Engineer — AcmeCorp (2021-2024)
- Built REST APIs with FastAPI, reduced latency by 30%
- Automated CI/CD with GitHub Actions

## Skills
Python, FastAPI, PostgreSQL, Docker
"""
DRAFT_A = MASTER.replace("Built REST APIs with FastAPI", "Built REST APIs in FastAPI for the platform")
DRAFT_B = MASTER.replace("Automated CI/CD with GitHub Actions", "Automated the CI/CD pipeline with GitHub Actions")


def _run_tailor(app_id, tmp_path, drafts, doctor_for):
    from app.tailoring.doctor import DoctorReport, ResumeDoctor
    from app.tailoring.grounding import GroundingResult

    def fake_check(self, tailored_md, master_md, jd_text):
        return doctor_for(tailored_md)

    with patch("app.matching.pipeline._load_resume", lambda user_id=None: MASTER), \
            patch("app.tailoring.tailor.settings") as mock_settings, \
            patch.object(ResumeDoctor, "check", fake_check), \
            patch("app.tailoring.grounding.GroundingChecker") as MockGrounding, \
            patch("app.tailoring.tailor.Tailor") as MockTailor:
        mock_settings.anthropic_api_key = "sk-fake"
        mock_settings.openai_api_key = ""
        mock_settings.tailoring_model = "claude"
        mock_settings.resume_path = tmp_path / "resume_master.md"
        mock_settings.profiles_dir = tmp_path / "profiles"
        mock_settings.data_dir = tmp_path
        mock_settings.tailor_skip_coverage_pct = 0.0
        mock_settings.grounding_required = False
        mock_settings.use_supabase = False
        MockGrounding.return_value.check.return_value = GroundingResult(
            passed=True, flagged_bullets=[], confidence_map={})
        inst = MockTailor.return_value
        inst.tailor_resume.side_effect = list(drafts)
        inst.write_cover_letter.return_value = "Dear team, I build APIs with FastAPI."
        from app.tailoring.tailor import tailor_for_application
        return tailor_for_application(app_id)
    del DoctorReport


def _style_report(score):
    from app.tailoring.doctor import DoctorReport
    return DoctorReport(passed=False, score=score, ats_coverage_pct=0.8,
                        banned_found=["dynamic"], weak_bullets=[], integrity_issues=[],
                        issues=["Banned words found: dynamic"], human_passed=True)


def test_a_style_only_doctor_failure_ships_the_best_draft_with_its_findings(seeded, tmp_path):
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus
    _, app_id = seeded

    def doctor_for(md):
        return _style_report(60 if "for the platform" in md else 50)

    resume_out, _ = _run_tailor(app_id, tmp_path, [DRAFT_A, DRAFT_B], doctor_for)
    with get_session() as s:
        row = s.get(Application, app_id)
        assert row.status == ApplicationStatus.TAILORED
        assert row.tailored_at is not None
        assert "Quality check: 60/100 after 2 drafts" in (row.notes or "")
        assert "Banned words found: dynamic" in row.notes
    shipped = (resume_out.parent / "resume.md").read_text(encoding="utf-8")
    assert "for the platform" in shipped            # attempt 1, the better draft
    assert "the CI/CD pipeline" not in shipped      # not attempt 2
    import json
    report = json.loads((resume_out.parent / "report.json").read_text(encoding="utf-8"))
    assert report["doctor_passed"] is False and report["doctor_score"] == 60
    assert report["attempts"] == 2


def test_an_integrity_failure_is_still_withheld(seeded, tmp_path):
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus
    from app.tailoring.doctor import DoctorReport
    _, app_id = seeded

    def doctor_for(md):
        return DoctorReport(passed=False, score=82, ats_coverage_pct=0.9,
                            integrity_issues=["Missing or altered: employer name"],
                            issues=["Missing or altered: employer name"], human_passed=True)

    _run_tailor(app_id, tmp_path, [DRAFT_A, DRAFT_B], doctor_for)
    with get_session() as s:
        row = s.get(Application, app_id)
        assert row.status == ApplicationStatus.ERROR
        assert "Missing or altered: employer name" in (row.notes or "")
        assert row.tailored_at is None


def test_the_error_branch_no_longer_keys_on_doctor_failed():
    import inspect

    from app.tailoring import tailor
    src = inspect.getsource(tailor.tailor_for_application)
    assert "elif doctor_integrity:" in src
    assert "elif doctor_failed:" not in src
