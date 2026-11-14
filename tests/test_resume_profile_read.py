"""The resume read fills the profile with everything the resume says.

Founder, 2026-11-14: "my resume has a lot of skills and data but only half is
extracted". Four cuts added up to that (app/intelligence/resume_profile_extract.py
has the list): a prompt that asked for a 1-2 sentence summary per job and never
asked for projects or certifications, a skills key with nothing saying "all", a
2,500-token reply ceiling that cut a long resume's JSON (which then fell back to
a parser keeping 40 skills and ONE bullet per job), and the resume itself cut at
16,000 characters.

The model cannot be called from the suite, so the route is driven with a
stand-in client; the free reader is run for real on a long resume. Rows here
belong to the "rpr-" users and are removed by that prefix.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.api import server
from app.db.init_db import get_session
from app.db.models import UserProfile
from app.intelligence import resume_profile_extract as rpe
from app.intelligence.resume_basic_extract import _split_sections, basic_extract_profile

ROOT = Path(__file__).resolve().parents[1]

RESUME = """Jordan Lee
Machine Learning Engineer
Cincinnati, OH 45220 | jordan.lee@example.com | (555) 010-7788
linkedin.com/in/jordan-lee | github.com/jordanlee

SUMMARY
Machine learning engineer with four years of experience building production ML systems, LLM applications and data platforms across retail and healthcare.

EXPERIENCE
AI/ML Engineer, Northwind Retail    Jun 2025 - Present
- Built a demand forecasting service used by 400 stores, cutting stock-outs by 12%
- Cut inference cost 38% by batching requests and caching embeddings in Redis
- Led the evaluation harness for the LLM support assistant (RAG over 2M documents)
- Migrated feature pipelines from cron jobs to Airflow with data quality checks
- Mentored two junior engineers through their first production launches
- Designed the A/B testing framework used by three product teams

Machine Learning Engineer, Contoso Health    May 2022 - Aug 2024
- Shipped a claims triage model that reduced manual review by a quarter
- Moved training pipelines from notebooks to Airflow and MLflow
- Built a PySpark job processing 40M records nightly on EMR
- Deployed models behind FastAPI on Kubernetes with autoscaling
- Wrote the monitoring dashboards for drift and latency in Grafana

Data Science Intern, Fabrikam Labs    Jan 2021 - Aug 2021
- Prototyped a churn model in scikit-learn with 0.81 AUC
- Automated weekly reporting with SQL and Tableau

PROJECTS
Volta - LLM hallucination verification platform
- Trained a 120M parameter transformer from scratch in PyTorch
- Multi-agent verification with semantic retrieval and trust scoring
- FAISS-based caching for repeated claims
SpotCheck - open-source data validation library
- 300+ GitHub stars; used by two university research groups
- Pluggable rule engine with a YAML configuration format

EDUCATION
University of Cincinnati    2024 - 2026
Master of Engineering in Computer Science, GPA: 3.8/4.0
State Technical University    2017 - 2021
Bachelor of Technology in Information Technology

SKILLS
Languages: Python, SQL, Java, Scala, Bash, R
ML & AI: PyTorch, TensorFlow, Scikit-learn, XGBoost, LightGBM, Hugging Face, LangChain, RAG, LLMs, NLP, Computer Vision, Recommender Systems
Data: Spark, PySpark, Kafka, Airflow, dbt, Snowflake, BigQuery, Redshift, PostgreSQL, MongoDB, Redis, Elasticsearch
MLOps: MLflow, Kubeflow, Vertex AI, SageMaker, Docker, Kubernetes, Terraform, CI/CD, GitHub Actions, Jenkins
Cloud: AWS (EC2, S3, EMR, Lambda), GCP, Azure
Backend: FastAPI, Flask, gRPC, REST APIs, GraphQL
Tools: Git, Grafana, Prometheus, Tableau, Jupyter, FAISS, Pinecone

CERTIFICATIONS
AWS Certified Machine Learning - Specialty, Amazon Web Services (2024)
Google Professional Data Engineer, Google Cloud, 2023

LANGUAGES
English (Fluent), Spanish (Conversational), Korean (Native)
"""

# Every item of the sample's Skills section (59), for "none was lost" checks.
ALL_SKILLS = [
    "Python", "SQL", "Java", "Scala", "Bash", "R", "PyTorch", "TensorFlow", "Scikit-learn", "XGBoost",
    "LightGBM", "Hugging Face", "LangChain", "RAG", "LLMs", "NLP", "Computer Vision",
    "Recommender Systems", "Spark", "PySpark", "Kafka", "Airflow", "dbt", "Snowflake", "BigQuery",
    "Redshift", "PostgreSQL", "MongoDB", "Redis", "Elasticsearch", "MLflow", "Kubeflow", "Vertex AI",
    "SageMaker", "Docker", "Kubernetes", "Terraform", "CI/CD", "GitHub Actions", "Jenkins", "AWS",
    "EC2", "S3", "EMR", "Lambda", "GCP", "Azure", "FastAPI", "Flask", "gRPC", "REST APIs", "GraphQL",
    "Git", "Grafana", "Prometheus", "Tableau", "Jupyter", "FAISS", "Pinecone",
]


@pytest.fixture(autouse=True)
def _own_rows():
    yield
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id.like("rpr-%")))
        s.commit()


def _model_reply(**over) -> dict:
    """What a complete model read of RESUME looks like (the shape the prompt asks for)."""
    reply = {
        "first_name": "Jordan", "last_name": "Lee", "email": "jordan.lee@example.com",
        "phone": "(555) 010-7788", "location": "Cincinnati, OH",
        "address": {"street": None, "city": "Cincinnati", "state": "OH", "postal_code": "45220", "country": None},
        "current_title": "AI/ML Engineer", "years_experience": 4,
        "linkedin_url": "linkedin.com/in/jordan-lee", "github_url": "github.com/jordanlee", "portfolio_url": None,
        "professional_summary": "Machine learning engineer with four years of experience.",
        "key_skills": ALL_SKILLS[:20],           # a model that shortened its list
        "experience": [
            {"title": "AI/ML Engineer", "company": "Northwind Retail", "location": None,
             "employment_type": "Full-time", "start": "Jun 2025", "end": "Present", "current": True,
             "bullets": ["Built a demand forecasting service used by 400 stores, cutting stock-outs by 12%",
                         "Cut inference cost 38% by batching requests and caching embeddings in Redis",
                         "Led the evaluation harness for the LLM support assistant (RAG over 2M documents)",
                         "Migrated feature pipelines from cron jobs to Airflow with data quality checks",
                         "Mentored two junior engineers through their first production launches",
                         "Designed the A/B testing framework used by three product teams"]},
            {"title": "Machine Learning Engineer", "company": "Contoso Health", "location": None,
             "employment_type": None, "start": "May 2022", "end": "Aug 2024", "current": False,
             "bullets": ["Shipped a claims triage model that reduced manual review by a quarter",
                         "Moved training pipelines from notebooks to Airflow and MLflow"]},
            {"title": "Data Science Intern", "company": "Fabrikam Labs", "location": None,
             "employment_type": "Internship", "start": "Jan 2021", "end": "Aug 2021", "current": False,
             "bullets": ["Prototyped a churn model in scikit-learn with 0.81 AUC"]},
        ],
        "education": [
            {"degree": "Master of Engineering", "field": "Computer Science", "university": "University of Cincinnati",
             "country": None, "start_year": 2024, "end_year": 2026, "gpa": "3.8", "gpa_scale": "4.0"},
            {"degree": "Bachelor of Technology", "field": "Information Technology",
             "university": "State Technical University", "country": None, "start_year": 2017, "end_year": 2021,
             "gpa": "", "gpa_scale": ""},
        ],
        "projects": [
            {"title": "Volta - LLM hallucination verification platform",
             "bullets": ["Trained a 120M parameter transformer from scratch in PyTorch",
                         "Multi-agent verification with semantic retrieval and trust scoring",
                         "FAISS-based caching for repeated claims"]},
            {"title": "SpotCheck - open-source data validation library",
             "bullets": ["300+ GitHub stars; used by two university research groups"]},
        ],
        "certifications": [
            {"name": "AWS Certified Machine Learning - Specialty", "issuer": "Amazon Web Services", "year": 2024},
            {"name": "Google Professional Data Engineer", "issuer": "Google Cloud", "year": "2023"},
        ],
        "languages": [{"language": "English", "proficiency": "Fluent"},
                      {"language": "Spanish", "proficiency": "Conversational"},
                      {"language": "Korean", "proficiency": "Native"}],
        "suggested_target_roles": ["Machine Learning Engineer", "AI Engineer"],
        "degree": "Master of Engineering", "university": "University of Cincinnati", "graduation_year": 2026,
    }
    reply.update(over)
    return reply


class _FakeClient:
    """Stands in for the shared Anthropic client; records what it was asked."""

    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.calls: list = []
        self._text, self._stop = text, stop_reason
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)],
                               stop_reason=self._stop)


def _run(monkeypatch, uid, *, client=None, resume=RESUME):
    """Drive the real route. ``client`` None = no API key (the free reader)."""
    from starlette.requests import Request as _Request

    from app.config import Settings, settings as _settings
    seen = {}
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: False))
    monkeypatch.setattr(_settings, "anthropic_api_key", "test-key" if client else "", raising=False)
    monkeypatch.setattr(server, "_require_user", lambda request: uid)
    monkeypatch.setattr("app.matching.pipeline._load_resume", lambda user_id=None: resume)
    monkeypatch.setattr(server, "analyze_resume_text", lambda text, uid_: {"findings": []}, raising=False)

    def _shared(*, timeout=None, max_retries=None):
        seen["timeout"], seen["max_retries"] = timeout, max_retries
        return client
    monkeypatch.setattr("app.common.llm.shared_anthropic", _shared)

    class _BG:
        def add_task(self, *a, **k):
            pass

    # The route allows 5 reads a minute per client address: each of these users
    # is its own address, so this file never spends another test's allowance.
    req = _Request({"type": "http", "method": "POST", "path": "/api/resume/extract-profile",
                    "headers": [], "query_string": b"", "client": (f"{uid}.test", 1), "app": server.app})
    out = server.extract_profile_from_resume(request=req, background_tasks=_BG(), defer_search=True)
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        saved = {
            "skills": [x.strip() for x in (p.key_skills or "").split(",") if x.strip()],
            "experience": json.loads(p.experience_json or "[]"),
            "education": json.loads(p.education_json or "[]"),
            "autofill": json.loads(p.autofill_json or "{}"),
            "summary": p.professional_summary,
        }
    return out, saved, seen


# ── what the model is asked ──────────────────────────────────────────────────

def test_the_prompt_asks_for_everything_and_never_for_a_summary_of_a_job():
    prompt = rpe.build_prompt(RESUME)
    for ask in ("EVERY skill", "EVERY job", "EVERY bullet", "EVERY school", "word for word",
                "projects (", "certifications (", "languages (", "Do not summarise"):
        assert ask in prompt, ask
    assert "a resume that names 60 skills returns 60 entries" in prompt
    assert "1-2 sentences" not in prompt, "the old instruction replaced every job's bullets with a summary"
    assert RESUME.strip() in prompt


def test_a_long_resume_is_sent_whole_and_a_cut_is_never_silent():
    # 40,000 characters: the old read kept the first 11,000 and the last 5,000.
    long = "HEAD-MARK\n" + ("experience line\n" * 1300) + "MIDDLE-MARK\n" + ("skill line\n" * 1700) + "TAIL-MARK"
    assert len(long) > 30_000
    text, cut = rpe.resume_for_prompt(long)
    assert cut is False and "MIDDLE-MARK" in text and text == long
    # Past the ceiling the head and tail survive and the caller is told.
    huge = "HEAD-MARK " + ("x" * (rpe.MAX_RESUME_CHARS + 5000)) + " TAIL-MARK"
    text, cut = rpe.resume_for_prompt(huge)
    assert cut is True and text.startswith("HEAD-MARK") and text.endswith("TAIL-MARK")
    src = (ROOT / "app" / "api" / "server.py").read_text()
    route = src[src.index("def extract_profile_from_resume("):src.index('@app.get("/api/resume/status")')]
    assert "_MAX = 16000" not in route and "resume_text[:11000]" not in route
    assert '"resume_truncated": _resume_cut' in route


def test_the_reply_has_room_and_the_request_has_time():
    from app.config import settings
    # 2,500 cut a long resume's JSON. The SDK refuses a non-streaming request
    # it expects to run past ten minutes (about 21,000 tokens).
    assert 12_000 <= rpe.MAX_OUTPUT_TOKENS <= 21_000
    assert rpe.REQUEST_TIMEOUT_S > settings.llm_request_timeout, \
        "a word-for-word reply can take longer than a lane's per-request timeout"


@pytest.mark.parametrize("raw,ok", [
    ('{"first_name": "A"}', True),
    ('```json\n{"first_name": "A"}\n```', True),
    ('Here is the JSON:\n{"first_name": "A"}', True),
    ('{"first_name": "A", "experience": [{"title": "Eng', False),     # cut mid-object
    ("", False), ("[1, 2]", False), ("no json here", False),
])
def test_a_cut_reply_is_recognised_as_incomplete(raw, ok):
    assert (rpe.parse_reply(raw) is not None) is ok


# ── the shape ────────────────────────────────────────────────────────────────

def test_bullets_become_one_line_each_and_nothing_is_shortened():
    read = rpe.normalise(_model_reply())
    job = read["experience"][0]
    assert job["summary"].split("\n") == _model_reply()["experience"][0]["bullets"]
    assert job["end"] == "Present" and job["current"] is True and job["employment_type"] == "Full-time"
    assert read["projects"][0]["description"].count("\n") == 2
    assert read["certifications"][0] == {"name": "AWS Certified Machine Learning - Specialty",
                                         "issuer": "Amazon Web Services", "year": "2024"}
    assert read["languages"][2] == {"language": "Korean", "proficiency": "Native or bilingual"}
    assert read["education"][0]["gpa_scale"] == "4.0" and read["education"][1]["end_year"] == 2021
    # The older reply shape (a `summary` string) still reads.
    old = rpe.normalise({"experience": [{"title": "Eng", "company": "X", "summary": "- did a\n- did b"}]})
    assert old["experience"][0]["summary"] == "did a\ndid b"


def test_a_language_level_is_mapped_and_never_guessed():
    assert rpe.language_level("Native speaker") == "Native or bilingual"
    assert rpe.language_level("Full professional proficiency") == "Fluent"
    assert rpe.language_level("Limited working proficiency") == "Conversational"
    assert rpe.language_level("Professional working") == "Professional"
    assert rpe.language_level("elementary") == "Basic"
    assert rpe.language_level(None) == "" and rpe.language_level("B2") == ""
    assert set(rpe.LANGUAGE_LEVELS) == set(__import__("app.autofill.autofill_fields", fromlist=["x"]).LANGUAGE_LEVELS)


def test_skills_accept_a_list_or_a_string_and_drop_duplicates():
    assert rpe.split_skills(["Python", "python ", "SQL", "", None, 3]) == ["Python", "SQL", "3"]
    assert rpe.split_skills("Python, SQL; Go\nRust") == ["Python", "SQL", "Go", "Rust"]
    assert len(rpe.split_skills([f"s{i}" for i in range(500)])) == rpe.MAX_SKILLS


# ── a skill the resume lists is never lost ───────────────────────────────────

def test_the_skills_section_is_read_item_by_item():
    got = rpe.skills_backstop(RESUME)
    assert got == ALL_SKILLS, sorted(set(ALL_SKILLS) ^ set(got))
    assert "CI/CD" in got, "a slash is not a separator"
    assert "SKILLS" not in got and "Languages" not in got and "Cloud" not in got


def test_a_shortened_model_list_is_completed_from_the_skills_section():
    merged = rpe.split_skills(rpe.merge_skills(ALL_SKILLS[:20] + ["Leadership"], rpe.skills_backstop(RESUME)))
    assert merged[:21] == ALL_SKILLS[:20] + ["Leadership"], "the model's own list and order come first"
    assert set(ALL_SKILLS) <= set(merged) and len(merged) == len(ALL_SKILLS) + 1


def test_a_heading_line_is_not_an_item():
    """The section patterns begin with whitespace, so a match can start on the
    blank line above a heading: the heading itself used to stay in the body and
    come out as a skill, a project, a certification."""
    sections = _split_sections(RESUME)
    for name in ("skills", "projects", "certifications", "languages", "experience", "education"):
        first = sections[name].splitlines()[0]
        assert first.strip().lower() != name, name
    assert _split_sections("# A B\n\n## Skills\nLanguages: Python, Go\n")["skills"] == "Languages: Python, Go"
    assert _split_sections("Skills: Python, Go, SQL\n\nEducation\nX University 2020\n")["skills"] == "Python, Go, SQL"


# ── the free reader (no model) reads the whole resume too ────────────────────

def test_the_free_reader_keeps_every_skill_and_every_bullet():
    read = rpe.normalise(basic_extract_profile(RESUME))
    assert rpe.split_skills(read["key_skills"]) == ALL_SKILLS, "it used to stop at 40 items / 500 characters"
    jobs = read["experience"]
    assert [(j["title"], j["company"]) for j in jobs] == [
        ("AI/ML Engineer", "Northwind Retail"), ("Machine Learning Engineer", "Contoso Health"),
        ("Data Science Intern", "Fabrikam Labs")]
    assert [len(j["summary"].split("\n")) for j in jobs] == [6, 5, 2], "it used to keep ONE bullet per job"
    assert "Designed the A/B testing framework used by three product teams" in jobs[0]["summary"]
    assert [p["title"] for p in read["projects"]] == [
        "Volta - LLM hallucination verification platform", "SpotCheck - open-source data validation library"]
    assert read["projects"][0]["description"].count("\n") == 2
    assert [c["name"] for c in read["certifications"]] == [
        "AWS Certified Machine Learning - Specialty", "Google Professional Data Engineer"]
    assert read["certifications"][0]["issuer"] == "Amazon Web Services" and read["certifications"][0]["year"] == "2024"
    assert read["languages"] == [{"language": "English", "proficiency": "Fluent"},
                                 {"language": "Spanish", "proficiency": "Conversational"},
                                 {"language": "Korean", "proficiency": "Native or bilingual"}]
    assert read["education"][0]["degree"] == "Master of Engineering in Computer Science"
    assert read["education"][0]["gpa"].startswith("3.8")
    assert rpe.counts(read) == {"skills": 59, "jobs": 3, "bullets": 13, "schools": 2, "projects": 2,
                                "certifications": 2, "languages": 3}


# ── the route ────────────────────────────────────────────────────────────────

def test_the_route_saves_the_whole_read(monkeypatch):
    client = _FakeClient(json.dumps(_model_reply()))
    out, saved, seen = _run(monkeypatch, "rpr-full", client=client)
    assert out["method"] == "llm" and out["resume_truncated"] is False
    # asked with room and time
    assert client.calls[0]["max_tokens"] == rpe.MAX_OUTPUT_TOKENS
    assert seen == {"timeout": rpe.REQUEST_TIMEOUT_S, "max_retries": 1}
    assert "EVERY bullet" in client.calls[0]["messages"][0]["content"]
    # every skill: the model returned 20, the Skills section has 59
    assert set(ALL_SKILLS) <= set(saved["skills"]) and len(saved["skills"]) == 59
    # every job with every bullet
    assert len(saved["experience"]) == 3
    assert saved["experience"][0]["summary"].count("\n") == 5
    assert saved["experience"][2]["employment_type"] == "Internship"
    assert len(saved["education"]) == 2 and saved["education"][0]["field"] == "Computer Science"
    # projects, certifications and languages reach the profile page's sections
    af = saved["autofill"]
    assert [p["title"] for p in af["projects"]] == ["Volta - LLM hallucination verification platform",
                                                    "SpotCheck - open-source data validation library"]
    assert af["projects"][0]["description"].count("\n") == 2
    assert af["certifications"][0]["name"] == "AWS Certified Machine Learning - Specialty"
    assert af["languages"][2] == {"language": "Korean", "proficiency": "Native or bilingual"}
    # the header's address parts fill the empty auto-fill answers
    assert af["city"] == "Cincinnati" and af["state"] == "OH" and af["postal_code"] == "45220"
    assert af["highest_education"] == "Master's degree" and "country" not in af
    assert out["read"] == {"skills": 59, "jobs": 3, "bullets": 9, "schools": 2, "projects": 2,
                           "certifications": 2, "languages": 3}


@pytest.mark.parametrize("text,stop", [
    ('{"first_name": "Jordan", "experience": [{"title": "AI/ML Eng', "max_tokens"),   # cut by the ceiling
    ("I could not read this resume.", "end_turn"),
])
def test_a_cut_or_unreadable_reply_still_ends_in_a_full_read(monkeypatch, text, stop):
    """The reply that used to mean "half a profile": the free reader takes over,
    and it now reads the whole resume."""
    out, saved, _seen = _run(monkeypatch, f"rpr-cut-{stop}", client=_FakeClient(text, stop))
    assert out["method"] == "basic"
    assert len(saved["skills"]) == 59
    assert [e["summary"].count("\n") + 1 for e in saved["experience"]] == [6, 5, 2]
    assert len(saved["autofill"]["projects"]) == 2 and len(saved["autofill"]["certifications"]) == 2


def test_no_api_key_reads_the_whole_resume_too(monkeypatch):
    out, saved, _seen = _run(monkeypatch, "rpr-nokey")
    assert out["method"] == "basic" and out["read"]["skills"] == 59 and out["read"]["bullets"] == 13
    assert saved["autofill"]["languages"][0] == {"language": "English", "proficiency": "Fluent"}


def test_what_the_user_saved_is_never_overwritten_by_the_resume(monkeypatch):
    from app.autofill import autofill_fields as af
    uid = "rpr-keeps"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, autofill_json=af.dumps({
            "city": "Columbus", "middle_name": "Q", "non_compete": "No",
            "projects": [{"title": "Old project"}]})))
        s.commit()
    _out, saved, _seen = _run(monkeypatch, uid, client=_FakeClient(json.dumps(_model_reply())))
    got = saved["autofill"]
    assert got["city"] == "Columbus", "an answer the user typed stays"
    assert got["middle_name"] == "Q" and got["non_compete"] == "No"
    assert got["state"] == "OH" and got["postal_code"] == "45220", "empty answers are filled"
    # The resume's own lists are replaced, like jobs and schools.
    assert [p["title"] for p in got["projects"]][0] == "Volta - LLM hallucination verification platform"


def test_a_resume_without_those_sections_leaves_the_saved_lists_alone(monkeypatch):
    from app.autofill import autofill_fields as af
    uid = "rpr-thin"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, autofill_json=af.dumps({
            "projects": [{"title": "Kept project"}], "languages": [{"language": "Telugu"}]})))
        s.commit()
    thin = _model_reply(projects=[], certifications=[], languages=None)
    _out, saved, _seen = _run(monkeypatch, uid, client=_FakeClient(json.dumps(thin)),
                              resume="Jordan Lee\nEngineer\n\nEXPERIENCE\nEngineer, X    Jan 2020 - Present\n- did things\n")
    assert saved["autofill"]["projects"] == [{"title": "Kept project"}]
    assert saved["autofill"]["languages"] == [{"language": "Telugu"}]


def test_highest_education_comes_from_the_degrees_listed():
    assert rpe.highest_education([{"degree": "Bachelor of Technology"}, {"degree": "Master of Engineering"}]) == "Master's degree"
    assert rpe.highest_education([{"degree": "B.S. in Biology"}]) == "Bachelor's degree"
    assert rpe.highest_education([{"degree": "PhD in Physics"}, {"degree": "M.S."}]) == "Doctorate (PhD)"
    assert rpe.highest_education([{"degree": "Doctor of Medicine"}]) == "Professional degree (MD, JD)"
    assert rpe.highest_education([{"degree": "Diploma in Design"}]) == "" and rpe.highest_education([]) == ""
    from app.autofill import autofill_fields as af
    levels = set(af.FIELDS["highest_education"].options)
    assert {lvl for _rx, lvl in rpe._EDU_LEVELS} <= levels, "a level the profile cannot store"


# ── the fill pack's own history read, and the page ───────────────────────────

def test_the_fill_packs_history_read_has_the_same_room():
    src = (ROOT / "app" / "autofill" / "answer_pack.py").read_text()
    fn = src[src.index("def _extract_history_from_resume("):src.index("def generate_answer_pack(")]
    assert "resume_text[:12000]" not in fn and "resume_for_prompt(resume_text)[0]" in fn
    assert "max_tokens=1500" not in fn and "max_tokens=8000" in fn
    assert "EVERY bullet point written under that role" in fn


def test_the_page_says_what_was_read_and_protects_unsaved_edits():
    dash = (ROOT / "app" / "templates" / "dashboard.html").read_text()
    fn = dash[dash.index("async function fillHistoryFromResume(btn)"):]
    fn = fn[:fn.index("// ── Skills: tags over the one comma-separated profile field")]
    assert "Save or discard your unsaved changes first." in fn
    assert fn.index("_serializeProfileForm(_ppForm()) !== _profileSnapshot") < fn.index("fetch('/api/resume/extract-profile'")
    assert "_ppReadSummary(d.read)" in fn and "d.method === 'basic'" in fn and "d.resume_truncated" in fn
    # a job's description box holds every bullet (it was limited to 1,200 characters)
    assert re.search(r'<textarea class="pp-input" rows="5" maxlength="4000"', dash)
    from app.autofill import autofill_fields as af
    desc = [f for f in af.LISTS["projects"].item_fields if f.key == "description"][0]
    assert desc.max_len == 4000
