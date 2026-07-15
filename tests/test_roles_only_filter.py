"""role_match_terms: SQL-safe title terms for the All Jobs "my roles" filter."""
from __future__ import annotations

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, UserProfile
from app.discovery.title_filter import role_match_terms


def test_empty_roles_returns_no_terms():
    assert role_match_terms([]) == []
    assert role_match_terms(None) == []


def test_expands_aliases_and_keeps_only_safe_length_terms():
    terms = role_match_terms(["Machine Learning Engineer", "AI Engineer"])
    # Full phrases + alias expansions present.
    assert "machine learning engineer" in terms
    assert "machine learning" in terms
    assert "artificial intelligence" in terms   # alias of "ai"
    assert "ai engineer" in terms
    # Dangerous short substrings are dropped (would match 'chair', 'html', …).
    assert "ai" not in terms
    assert "ml" not in terms
    # Every emitted term is safe (>= 4 chars, ignoring internal spaces).
    assert all(len(t.replace(" ", "")) >= 4 for t in terms)


def test_titles_match_via_substring():
    terms = role_match_terms(["Machine Learning Engineer", "AI Engineer"])
    def hits(title):
        t = title.lower()
        return any(term in t for term in terms)
    assert hits("Senior AI Engineer, Agentic Systems")
    assert hits("Staff Machine Learning Engineer")
    assert hits("MLOps Platform Engineer")     # via 'mlops' alias
    assert not hits("Registered Nurse")         # clearly off-role
    assert not hits("Warehouse Associate")


def test_nontech_roles_supported():
    terms = role_match_terms(["Registered Nurse", "Financial Analyst"])
    assert "registered nurse" in terms
    assert "nursing" in terms          # alias
    assert "financial analyst" in terms
    t = "ICU Registered Nurse".lower()
    assert any(term in t for term in terms)


def test_roles_only_endpoint_semantic_catch():
    """The endpoint keeps off-title jobs the AI scored a real fit (semantic catch)
    — so 'same work, different title' isn't missed — while dropping off-title,
    low-score jobs. Reuses the existing fit score, no separate embedding pass."""
    from fastapi.testclient import TestClient
    from app.api.server import app

    def _job(ext, title, score):
        return Job(source=JobSource.GREENHOUSE, external_id=ext, company=ext,
                   title=title, url=f"http://x/{ext}", description="x",
                   rerank_score=score, blended_score=score)

    with get_session() as s:
        s.exec(delete(UserProfile))
        for j in s.exec(select(Job).where(Job.external_id.like("rofilt-%"))).all():
            s.delete(j)
        s.commit()
        s.add(UserProfile(user_id=None, target_roles="Machine Learning Engineer"))
        s.add(_job("rofilt-title", "Senior Machine Learning Engineer", 20))  # title match
        s.add(_job("rofilt-sem", "Applied Scientist", 82))    # off-title, high fit → keep
        s.add(_job("rofilt-off", "Warehouse Associate", 10))  # off-title, low fit → drop
        s.commit()

    try:
        data = TestClient(app).get("/api/jobs?roles_only=1&limit=200").json()
        titles = {j["title"] for j in data["jobs"]}
        assert "Senior Machine Learning Engineer" in titles   # via title
        assert "Applied Scientist" in titles                   # via score (semantic)
        assert "Warehouse Associate" not in titles             # neither → hidden
    finally:
        with get_session() as s:
            s.exec(delete(UserProfile))
            for j in s.exec(select(Job).where(Job.external_id.like("rofilt-%"))).all():
                s.delete(j)
            s.commit()
