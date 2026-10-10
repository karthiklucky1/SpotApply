"""Skill-gap classification: résumé beats GitHub beats nothing."""
from __future__ import annotations

import json

from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, UserPersonalMemory, UserProfile


JD_A = """
Senior Backend Engineer. Requirements: strong Python, production Kafka
experience, and AWS infrastructure. You will build streaming pipelines with
Kafka and deploy services on AWS using Python and FastAPI.
"""
JD_B = """
Data Platform Engineer. Must have Python and Kafka. Experience with AWS and
Terraform is required. Kafka streaming and Python services are the core stack.
"""


def _seed(session):
    session.exec(delete(Job))
    session.exec(delete(UserPersonalMemory))
    for i, jd in enumerate((JD_A, JD_B)):
        session.add(Job(
            user_id=None, source=JobSource.GREENHOUSE, external_id=f"sg-{i}",
            company=f"Co{i}", title="Backend Engineer", url=f"https://x/{i}",
            description=jd, rerank_score=80.0 - i, blended_score=80.0 - i,
        ))
    # GitHub harvest cache: proof of kafka in a repo, nothing about aws
    session.add(UserPersonalMemory(
        user_id=None, source="github",
        raw_content=json.dumps({"github": {
            "ok": True, "username": "testuser",
            "repos": [{"name": "event-pipeline", "description": "Kafka consumer demo",
                       "language": "Python", "topics": ["kafka"], "stars": 1}],
            "events": [],
        }}),
    ))
    session.commit()


def test_skill_gap_classification(monkeypatch):
    import app.intelligence.skill_gap as sg

    with get_session() as session:
        _seed(session)

    # Résumé mentions python but not kafka/aws
    monkeypatch.setattr(
        "app.matching.pipeline._load_resume",
        lambda user_id=None: "Experienced engineer. Python, FastAPI, PostgreSQL.",
    )

    out = sg.compute_skill_gap("local", top_n_jobs=10)

    assert out["scanned_jobs"] == 2
    assert out["resume_loaded"] is True
    assert out["github"]["connected"] is True

    matched = {i["skill"] for i in out["matched"]}
    vis = {i["skill"] for i in out["add_visibility"]}
    learn = {i["skill"] for i in out["learn"]}

    assert "python" in matched
    assert "kafka" in vis, f"kafka should be add_visibility, got matched={matched} vis={vis} learn={learn}"
    kafka = next(i for i in out["add_visibility"] if i["skill"] == "kafka")
    assert kafka["evidence"]["repo"] == "event-pipeline"
    assert "add it to your resume" in kafka["advice"]
    assert "aws" in learn
    aws = next(i for i in out["learn"] if i["skill"] == "aws")
    assert "GitHub" in aws["advice"] and "LinkedIn" in aws["advice"]
    # Demand counts: both JDs want kafka & python
    assert kafka["demand"] == 2


def test_skill_gap_empty_pool():
    from app.intelligence.skill_gap import compute_skill_gap
    with get_session() as session:
        session.exec(delete(Job))
        session.commit()
    out = compute_skill_gap("local")
    assert out["scanned_jobs"] == 0
    assert out["matched"] == [] and out["learn"] == []


# ── Accuracy (2026-10-10) ────────────────────────────────────────────────────
# Measured before this change on these pairs: precision 0.08, recall 0.58 — the
# "Learn these" cards were benefits/EEO boilerplate and spelling differences.
# Everything below runs the real compute_skill_gap with the DB stubbed out.

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

_NO_GH = {"connected": False, "username": "", "blob": "", "repos": [], "harvested_at": None}
_NO_LI = {"connected": False, "blob": ""}


def _run(monkeypatch, resume: str, jds: list[str]) -> dict:
    import app.intelligence.skill_gap as sg
    jobs = [SimpleNamespace(id=i, title=f"Role {i}", company=f"Co {i}", description=jd,
                            blended_score=90 - i, rerank_score=90 - i) for i, jd in enumerate(jds)]
    monkeypatch.setattr(sg, "_top_jobs", lambda user_id, limit: jobs[:limit])
    monkeypatch.setattr(sg, "_load_resume_text", lambda user_id, profile: (resume, True))
    monkeypatch.setattr(sg, "_github_evidence", lambda user_id, profile: dict(_NO_GH))
    monkeypatch.setattr(sg, "_linkedin_evidence", lambda user_id: dict(_NO_LI))
    return sg.compute_skill_gap("local", top_n_jobs=30)


EEO = ("We are an equal opportunity employer and consider applicants regardless of race, "
       "gender identity or veteran status. Benefits include medical, dental and vision insurance, "
       "401(k) matching and paid time off. ")

# (resume, [jd...], expected learn gaps)
PAIRS = [
    ("Go, Kubernetes (K8s), Postgres, PySpark, Amazon Web Services. Built REST APIs in Python.",
     [EEO + "Requirements: Golang, kubernetes, postgresql, spark, aws, rest apis, python. Design and own the backbone for enterprise."],
     set()),
    ("Python, Django, PostgreSQL. ML pipelines with scikit-learn. NodeJS services.",
     [EEO + "You will build machine learning systems with sklearn and Python; Node.js and Django experience required; Kafka a must."],
     {"kafka"}),
    ("Java, Spring Boot, MySQL, Jenkins CI/CD.",
     [EEO + "Strong Java and Spring Boot. Kubernetes and Terraform required. CI/CD with Jenkins. AWS preferred."],
     {"kubernetes", "terraform", "aws"}),
    ("Data engineer: Airflow, Snowflake, dbt, SQL, Python, data pipelines.",
     [EEO + "Own data pipelines on Airflow and Snowflake; SQL and Python daily; Spark and Kafka streaming required."],
     {"spark", "kafka"}),
    ("LLM apps with LangChain, RAG, vector search, OpenAI APIs, Python, FastAPI.",
     [EEO + "Experience with large language models, retrieval augmented generation, LangChain and Python. PyTorch required."],
     {"pytorch"}),
    ("Analyst: Excel, SQL, Tableau, R and Python for statistics.",
     [EEO + "Must know SQL, Tableau and R; Python preferred. Our go-to-market team and R&D budget are growing."],
     set()),
    ("Frontend: React, TypeScript, JavaScript, Node.js, GraphQL.",
     [EEO + "React and TypeScript daily; GraphQL and Node.js APIs; Docker and Kubernetes for deployment."],
     {"docker", "kubernetes"}),
    ("C/C++ systems programmer; Rust; Linux kernel modules.",
     [EEO + "C and C++ required; Rust a plus; Go services on the side."],
     {"go"}),
]


def test_learn_gaps_are_real_skills_the_resume_lacks(monkeypatch):
    tp = fp = fn = 0
    misses = []
    for resume, jds, want in PAIRS:
        out = _run(monkeypatch, resume, jds)
        got = {i["skill"] for i in out["learn"]}
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
        if got != want:
            misses.append((resume[:40], sorted(got - want), sorted(want - got)))
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    print(f"\n[skill-gap accuracy on {len(PAIRS)} pairs] precision {precision:.2f} recall {recall:.2f} "
          f"(legacy: precision 0.08 recall 0.58)")
    for m in misses:
        print("  MISS", m)
    assert precision >= 0.85 and recall >= 0.85, misses


def test_boilerplate_and_glue_never_become_gaps(monkeypatch):
    out = _run(monkeypatch, "Python.", [EEO + "Design and own the backbone for enterprise. Python and Spark."])
    learn = {i["skill"] for i in out["learn"]}
    assert learn == {"spark"}, learn
    for bad in ("dental", "vision", "equal opportunity", "design and own", "backbone", "paid time off"):
        assert all(bad not in i["skill"] for i in out["learn"] + out["matched"]), bad


def test_spelling_differences_are_the_same_skill(monkeypatch):
    out = _run(monkeypatch,
               "Go, K8s, Postgres, PySpark, Amazon Web Services, NodeJS, LLMs, REST API, ML, CI/CD.",
               ["Golang, kubernetes, postgresql, spark, aws, node.js, large language models, "
                "rest apis, machine learning, ci/cd pipelines."])
    assert out["learn"] == []
    matched = {i["skill"] for i in out["matched"]}
    assert {"go", "kubernetes", "postgresql", "spark", "aws", "node.js", "llm", "rest api",
            "machine learning"} <= matched


def test_alias_variants_merge_into_one_demand(monkeypatch):
    out = _run(monkeypatch, "Python.", ["We use LLMs daily.", "Experience with large language models.",
                                        "LLM apps."])
    llm = [i for i in out["learn"] if i["skill"] == "llm"]
    assert len(llm) == 1 and llm[0]["demand"] == 3
    assert not any(i["skill"] in ("llms", "large language models") for i in out["learn"])


@pytest.mark.parametrize("jd,resume,want_demanded,want_matched", [
    ("Go services and R for statistics.", "Go, R", True, True),
    ("Our go-to-market team; R&D budget.", "Go, R", False, None),   # neither is a language here
    ("Go programming required.", "We go live monthly.", True, False),
    ("R programming required.", "R&D experience.", True, False),
    ("C/C++ required.", "C and C++ systems work.", True, True),
    ("Grade C or above.", "C", False, None),
])
def test_short_language_names_are_read_in_context(monkeypatch, jd, resume, want_demanded, want_matched):
    import app.intelligence.skill_gap as sg
    demanded = set(sg.demanded_skills(jd))
    short = demanded & {"go", "r", "c"}
    assert bool(short) is want_demanded, (jd, demanded)
    if want_matched is not None:
        for skill in short:
            assert sg.skill_present(skill, resume, sg._normalize(resume)) is want_matched, (skill, resume)


def test_a_failed_github_harvest_is_cached_not_repeated(monkeypatch):
    import app.intelligence.skill_gap as sg
    calls = []
    monkeypatch.setattr("app.intelligence.harvester.harvest_github",
                        lambda url: calls.append(url) or {"ok": False, "reason": "http_403", "repos": [], "events": []})
    with get_session() as session:
        session.exec(delete(UserPersonalMemory).where(UserPersonalMemory.user_id == "sg-gh-user"))
        session.commit()
    profile = SimpleNamespace(github_url="https://github.com/someone")
    sg._github_evidence("sg-gh-user", profile)
    sg._github_evidence("sg-gh-user", profile)
    assert len(calls) == 1
    with get_session() as session:
        rows = session.exec(select(UserPersonalMemory).where(UserPersonalMemory.user_id == "sg-gh-user")).all()
        assert len(rows) == 1
        session.exec(delete(UserPersonalMemory).where(UserPersonalMemory.user_id == "sg-gh-user"))
        session.commit()
