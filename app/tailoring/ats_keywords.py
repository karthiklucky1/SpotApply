"""ATS exact-phrase keyword matching.

Real ATS systems (Greenhouse, Lever, Workday, Taleo) score a resume by how
many of the job description's *exact* terms appear verbatim. A resume that
says "built REST APIs" when the JD asks for "RESTful API design" can score
lower than expected because the parser looks for the literal phrase.

This module:
  1. Extracts the top N high-signal phrases from a JD (1-3 word n-grams,
     weighted toward technical terms and requirement language).
  2. Checks which of those phrases appear *verbatim* in the resume.
  3. Reports the missing ones so the tailoring step can target them
     specifically — and so the Doctor can score real phrase coverage.

No LLM / network calls — fully deterministic and fast.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

log = logging.getLogger(__name__)

# Words that should never start or end a meaningful phrase
_STOP = {
    "the", "and", "for", "with", "you", "our", "are", "this", "that", "will",
    "have", "from", "your", "not", "but", "can", "has", "been", "more", "than",
    "into", "within", "across", "each", "its", "about", "what", "such", "any",
    "a", "an", "is", "in", "of", "to", "at", "or", "by", "on", "it", "we",
    "as", "be", "we're", "who", "all", "able", "their", "they", "etc", "e.g",
    "i.e", "using", "use", "used", "via", "per", "must", "should", "would",
    "may", "also", "well", "like", "including", "include", "includes",
    "ability", "experience", "years", "year", "strong", "good", "great",
    "excellent", "proven", "track", "record", "looking", "seeking", "join",
    "team", "role", "work", "working", "help", "build", "building",
    "etc.", "plus", "nice", "preferred", "required", "requirements",
    "responsibilities", "qualifications", "skills", "knowledge",
}

# Curated multi-word and single tech terms that should always be captured
# verbatim when present in the JD (these are exactly what ATS parsers index).
_TECH_PHRASES = [
    # Languages / core
    "python", "typescript", "javascript", "golang", "java", "c++", "rust", "sql",
    # ML / AI
    "machine learning", "deep learning", "large language models", "llm", "llms",
    "natural language processing", "nlp", "computer vision", "generative ai",
    "fine-tuning", "fine tuning", "prompt engineering", "rag",
    "retrieval augmented generation", "retrieval-augmented generation",
    "multi-agent", "agentic", "embeddings", "vector database", "vector search",
    "model inference", "inference", "transformers", "pytorch", "tensorflow",
    "scikit-learn", "hugging face", "langchain", "llamaindex", "openai", "claude",
    "semantic search", "recommendation systems", "mlops", "model deployment",
    # Backend / infra
    "rest api", "restful api", "restful apis", "rest apis", "graphql", "grpc",
    "microservices", "fastapi", "flask", "django", "node.js", "express",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch", "kafka",
    "rabbitmq", "celery", "airflow", "spark", "pyspark", "bigquery", "snowflake",
    "data pipelines", "etl", "data engineering", "distributed systems",
    # Cloud / devops
    "aws", "gcp", "azure", "kubernetes", "docker", "terraform", "ci/cd",
    "github actions", "jenkins", "vertex ai", "sagemaker", "lambda",
    "cloud infrastructure", "containerization", "observability", "prometheus",
    "grafana", "datadog",
    # Practices
    "unit testing", "integration testing", "test-driven development",
    "agile", "scrum", "code review", "system design", "api design",
    "production systems", "scalable systems", "high availability",
]


@dataclass
class ATSKeywordReport:
    top_phrases: List[str] = field(default_factory=list)       # ranked JD phrases
    matched: List[str] = field(default_factory=list)           # present verbatim in resume
    missing: List[str] = field(default_factory=list)           # absent from resume
    coverage_pct: float = 0.0                                  # matched / total

    def summary(self) -> str:
        return (
            f"ATS phrase coverage: {self.coverage_pct:.0%} "
            f"({len(self.matched)}/{len(self.top_phrases)} matched). "
            f"Missing: {', '.join(self.missing[:8])}"
            + ("…" if len(self.missing) > 8 else "")
        )


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for verbatim matching."""
    return re.sub(r"\s+", " ", text.lower())


def _phrase_present(phrase: str, normalized_resume: str) -> bool:
    """True if phrase appears verbatim in resume (word-boundary aware)."""
    # Escape regex specials in phrase (c++, ci/cd, node.js, etc.)
    pat = re.escape(phrase.lower())
    # Allow the phrase to sit between non-word chars / boundaries.
    return re.search(rf"(?<!\w){pat}(?!\w)", normalized_resume) is not None


def _strip_html(text: str) -> str:
    """Remove HTML tags/entities so markup ('<ul><li>') never becomes a
    "missing keyword" chip like 'ul li'."""
    import html as _html
    text = re.sub(r"<[^>]+>", " ", text or "")
    return _html.unescape(text)


def extract_jd_phrases(jd_text: str, top_n: int = 18) -> List[str]:
    """Extract the top N high-signal exact phrases from a job description.

    Combines:
      - curated tech phrases present in the JD (always included, ranked first)
      - frequent 2-3 word n-grams not starting/ending on stop words
      - frequent meaningful single tokens
    """
    norm = _normalize(_strip_html(jd_text))

    # 1. Curated tech phrases that actually appear in this JD
    tech_hits: List[str] = []
    for term in _TECH_PHRASES:
        if _phrase_present(term, norm):
            tech_hits.append(term)
    # De-dupe overlapping curated terms (prefer longer phrase, drop contained shorter)
    tech_hits = _dedupe_contained(tech_hits)

    # 2. Frequency-counted n-grams (2 and 3 word) and unigrams.
    # Segment on sentence/clause punctuation first so n-grams never span a
    # boundary (e.g. "engineer. build large"). Tech tokens like node.js and
    # ci/cd survive because they have no period-followed-by-space.
    segments = re.split(r"[.;:!?,\n\r\t()\[\]]+|\s[-–]\s", norm)
    ngram_freq: Dict[str, int] = {}

    def _count_segment(tokens: List[str], seq_len: int) -> None:
        for i in range(len(tokens) - seq_len + 1):
            gram = tokens[i:i + seq_len]
            if gram[0] in _STOP or gram[-1] in _STOP:
                continue
            if any(len(t) < 2 for t in gram):
                continue
            phrase = " ".join(gram)
            ngram_freq[phrase] = ngram_freq.get(phrase, 0) + 1

    for seg in segments:
        seg_tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9+#/\-]*(?:\.[a-zA-Z]+)?", seg)
        _count_segment(seg_tokens, 3)
        _count_segment(seg_tokens, 2)
        for t in seg_tokens:
            if t in _STOP or len(t) < 3:
                continue
            ngram_freq[t] = ngram_freq.get(t, 0) + 1

    # Score: longer phrases and repeated phrases rank higher
    def _score(item: Tuple[str, int]) -> float:
        phrase, freq = item
        word_count = phrase.count(" ") + 1
        # multi-word phrases get a length multiplier; repetition matters most
        return freq * (1.0 + 0.6 * (word_count - 1))

    ranked = sorted(ngram_freq.items(), key=_score, reverse=True)

    # 3. Merge: curated tech hits first, then top-ranked n-grams
    result: List[str] = list(tech_hits)
    for phrase, _ in ranked:
        if len(result) >= top_n:
            break
        if phrase in result:
            continue
        # skip n-grams already covered by a curated phrase
        if any(phrase in t or t in phrase for t in tech_hits):
            continue
        result.append(phrase)

    return _dedupe_contained(result)[:top_n]


def _dedupe_contained(phrases: List[str]) -> List[str]:
    """Drop phrases that are fully contained in a longer phrase already kept.

    e.g. keep "restful api design", drop standalone "api" if redundant —
    but only when one is a substring token-sequence of another.
    """
    kept: List[str] = []
    # Sort longest first so longer phrases win
    for p in sorted(phrases, key=lambda x: -len(x)):
        if any((p != k) and (f" {p} " in f" {k} " or k.startswith(p + " ") or k.endswith(" " + p)) for k in kept):
            continue
        kept.append(p)
    # Restore original ordering preference (tech/freq order) by sorting against input index
    order = {p: i for i, p in enumerate(phrases)}
    return sorted(kept, key=lambda x: order.get(x, 999))


def analyze(jd_text: str, resume_text: str, top_n: int = 18) -> ATSKeywordReport:
    """Full report: top JD phrases, which are matched verbatim, which are missing."""
    phrases = extract_jd_phrases(jd_text, top_n=top_n)
    norm_resume = _normalize(resume_text)

    matched: List[str] = []
    missing: List[str] = []
    for p in phrases:
        if _phrase_present(p, norm_resume):
            matched.append(p)
        else:
            missing.append(p)

    coverage = len(matched) / len(phrases) if phrases else 1.0
    report = ATSKeywordReport(
        top_phrases=phrases,
        matched=matched,
        missing=missing,
        coverage_pct=round(coverage, 3),
    )
    log.info("ATSKeyword %s", report.summary())
    return report
