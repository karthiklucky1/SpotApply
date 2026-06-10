"""Unit tests for ATS exact-phrase keyword matching."""
from __future__ import annotations

from app.tailoring.ats_keywords import (
    extract_jd_phrases,
    analyze,
    _phrase_present,
    _normalize,
)


SAMPLE_JD = """
We are looking for a Senior Machine Learning Engineer to join our team.

Responsibilities:
- Build and deploy large language models in production
- Design RESTful API services using FastAPI and Python
- Develop retrieval augmented generation (RAG) pipelines
- Work with PyTorch and Hugging Face transformers
- Deploy models on Kubernetes and AWS

Requirements:
- Strong experience with Python and machine learning
- Experience with vector databases and semantic search
- Familiarity with prompt engineering and fine-tuning
- Knowledge of CI/CD and Docker
"""


class TestPhrasePresent:
    def test_exact_match(self):
        assert _phrase_present("fastapi", _normalize("Built apps with FastAPI today"))

    def test_multiword_match(self):
        assert _phrase_present("machine learning", _normalize("I love Machine Learning a lot"))

    def test_no_partial_word_match(self):
        # "api" should not match inside "rapidly"
        assert not _phrase_present("api", _normalize("we move rapidly here"))

    def test_special_chars(self):
        assert _phrase_present("c++", _normalize("Expert in C++ programming"))
        assert _phrase_present("ci/cd", _normalize("We use CI/CD pipelines"))

    def test_absent_phrase(self):
        assert not _phrase_present("kubernetes", _normalize("we use docker only"))


class TestExtractPhrases:
    def test_extracts_tech_phrases(self):
        phrases = extract_jd_phrases(SAMPLE_JD, top_n=18)
        joined = " | ".join(phrases)
        # Core technical phrases from the JD should be captured
        assert "machine learning" in phrases
        assert "fastapi" in phrases
        assert "pytorch" in phrases
        assert any("rag" in p or "retrieval augmented generation" in p for p in phrases)

    def test_respects_top_n(self):
        phrases = extract_jd_phrases(SAMPLE_JD, top_n=10)
        assert len(phrases) <= 10

    def test_no_stopword_boundaries(self):
        phrases = extract_jd_phrases(SAMPLE_JD, top_n=18)
        for p in phrases:
            words = p.split()
            assert words[0] not in {"the", "and", "with", "for", "to", "of"}
            assert words[-1] not in {"the", "and", "with", "for", "to", "of"}

    def test_empty_jd(self):
        assert extract_jd_phrases("", top_n=18) == []


class TestAnalyze:
    def test_full_coverage(self):
        resume = SAMPLE_JD  # resume identical to JD → everything matched
        report = analyze(SAMPLE_JD, resume, top_n=18)
        assert report.coverage_pct == 1.0
        assert report.missing == []

    def test_zero_coverage(self):
        resume = "I enjoy gardening and painting watercolors on weekends."
        report = analyze(SAMPLE_JD, resume, top_n=18)
        assert report.coverage_pct < 0.2
        assert len(report.missing) > 0

    def test_partial_coverage_identifies_gaps(self):
        # Resume has Python + ML but not Kubernetes/FastAPI/RAG
        resume = """
        Machine Learning Engineer with strong Python experience.
        Built models with PyTorch and trained transformers.
        """
        report = analyze(SAMPLE_JD, resume, top_n=18)
        assert 0.0 < report.coverage_pct < 1.0
        assert "machine learning" in report.matched
        assert "fastapi" in report.missing

    def test_matched_and_missing_partition(self):
        resume = "Python and FastAPI developer."
        report = analyze(SAMPLE_JD, resume, top_n=18)
        # matched + missing should equal all top phrases, no overlap
        assert set(report.matched) | set(report.missing) == set(report.top_phrases)
        assert set(report.matched) & set(report.missing) == set()
