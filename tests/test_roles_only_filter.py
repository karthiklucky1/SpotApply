"""role_match_terms: SQL-safe title terms for the All Jobs "my roles" filter."""
from __future__ import annotations

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
