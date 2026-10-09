"""ATS phrase coverage — making a resume FINDABLE, not "beating a scanner".

What this module is NOT for. It does not defeat an auto-rejecter, because no
such thing rejects on resume keywords. Greenhouse's own documentation states
that applications "are reviewed by real people, one by one, in the order
received", that "AI doesn't score or rank applications, nor does it make any
decisions", and that auto-reject filters are "set and controlled directly by
recruiters". In Greenhouse and Ashby, auto-reject fires exclusively on custom
application questions of type yes/no, single-select or multi-select — the
knockout questions (work authorisation, sponsorship, location, clearance,
minimum years). The widely-repeated "75% of resumes are auto-rejected by the
ATS" traces to a defunct resume company around 2012 and has no verifiable
source. (docs/research/hiring-machine-2026-08.md §1.3.)

What the real mechanic is, and why phrase coverage still matters. The ATS is a
database that recruiters query with boolean strings, and resumes are parsed
into structured fields by third-party parsers that pattern-match rather than
comprehend. You are not rejected — you are *unsearchable*, and then unread.
With ~244 applications per requisition, a resume that never surfaces for the
recruiter's query is never opened. Greenhouse notes its search expands to
synonyms, so exact-string matching is not required, but relevance is.

So: cover the terms a recruiter would actually search for. Do not stuff.

This module:
  1. Extracts the top N high-signal phrases from a JD (1-3 word n-grams,
     weighted toward technical terms and requirement language).
  2. Checks which of those phrases appear in the resume.
  3. Reports the missing ones so the tailoring step can target them
     specifically — and so the Doctor can score real phrase coverage.

Coverage is a findability signal, not a pass/fail gate — and it never licenses
adding a skill the candidate does not have (see tailoring/grounding.py).

No LLM / network calls — fully deterministic and fast.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

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
    # Plain English function words. The ranker counted single words by
    # frequency, so "where" and "them" reached the Tailoring Studio as "Not
    # covered" keywords and as "I've used Where" buttons (live test,
    # 2026-10-09). None of these is a technology, so none can start or end a
    # phrase, and none is ever a keyword on its own. (Words that double as a
    # technology, such as "go", "rest", "next", "express" or "swift", are left
    # out on purpose.)
    "where", "when", "which", "while", "whom", "whose", "why", "how",
    "them", "these", "those", "there", "here", "then", "theirs", "ours",
    "yours", "us", "he", "she", "his", "her", "him", "me", "my", "mine",
    "do", "does", "did", "done", "doing", "was", "were", "being", "had",
    "having", "if", "else", "so", "too", "very", "just", "only", "both",
    "either", "neither", "every", "few", "many", "much", "most", "other",
    "others", "another", "some", "same", "up", "down", "out", "over",
    "under", "again", "further", "once", "above", "below", "between",
    "through", "during", "before", "after", "without", "upon", "against",
    "among", "around", "toward", "towards", "whether", "because", "since",
    "until", "unless", "although", "though", "yet", "nor", "no", "could",
    "shall", "might", "get", "gets", "got", "always", "often", "never",
    "ever", "really", "already", "still", "even", "everyone", "anyone",
    "something", "thing", "things", "way", "ways", "lot", "lots", "today",
    "currently", "ensure", "ensuring", "whatever", "wherever", "whenever",
    "whoever", "you'll", "we'll", "they're", "you're", "it's", "am", "best",
    # Third-person verbs: a phrase that starts or ends on one is a clause
    # fragment ("customer team works"), not a skill.
    "works", "helps", "uses", "makes", "takes", "gives", "provides",
    "requires", "offers", "seeks", "means", "keeps", "lets", "becomes",
    "matters", "need", "needs", "want", "wants", "love", "loves", "care", "cares",
    # Adjectives a posting uses about itself, never about a skill.
    "dedicated", "committed", "fully", "eligible", "various", "numerous",
}
# Small words that DO sit inside real skill phrases ("infrastructure as code",
# "proof of concept", "research and development"). Any other stop word inside a
# phrase marks a sentence fragment ("works where cybersecurity").
_INNER_OK = frozenset({"of", "as", "a", "an", "and", "the", "for", "to", "in", "on",
                       "with", "by", "at", "or"})

# The company talking about itself and the job: never a keyword, alone or as a
# whole phrase ("company culture", "team members").
_SELF_TALK = frozenset({
    "company", "companies", "culture", "team", "teams", "mission", "missions",
    "values", "vision", "opportunity", "opportunities", "position", "positions",
    "candidate", "candidates", "applicant", "applicants", "people", "person",
    "benefits", "salary", "equity", "bonus", "compensation", "world", "future",
    "impact", "everything", "anything", "someone", "day", "days", "employer",
    "employees", "employee", "equal", "accommodation", "accommodations",
    "interview", "process", "growth", "career", "careers", "job", "jobs",
    "role", "roles", "member", "members", "ideal", "passion", "passionate",
    "exciting", "awesome", "amazing", "headquarters", "hq",
    # Compensation, perks and equal-opportunity boilerplate, which every
    # posting repeats and no resume should be "missing".
    "diversity", "inclusion", "inclusive", "belong", "community", "communities",
    "workplace", "workforce", "perspectives", "commitment", "respect",
    "respected", "valued", "rewarded", "perks", "insurance", "dental",
    "vacation", "parental", "leave", "policy", "family", "travel", "annually",
    "range", "stock", "options", "package", "legally", "protected", "national",
    "origin", "identity", "expression", "genetic", "marital", "sex", "age",
    "color", "contributions", "potential", "unique", "welcome", "qualified",
    "individuals", "duties", "duty", "listing", "notice", "materials",
    "information", "competitive", "dynamic", "innovative", "innovation",
    "creativity", "results", "ownership", "collaboration", "collaborative",
})
# Words that are never a keyword ON THEIR OWN even when the posting repeats
# them. "customer" reached the "Not covered" list for a support engineering
# role; nobody is "missing" customer. Inside a phrase they still count
# ("customer support", "product design", "customer success"), because the
# phrase names real work.
_FLUFF = _SELF_TALK | frozenset({
    "customer", "customers", "client", "clients", "user", "users", "end",
    "solution", "solutions", "platform", "platforms", "product", "products",
    "service", "services", "tool", "tools", "technology", "technologies",
    "system", "systems", "application", "applications", "project", "projects",
    "environment", "environments", "software", "office", "location", "locations",
    "remote", "hybrid",
    "onsite", "status", "gender", "race", "veteran", "veterans", "disability",
    "religion", "orientation", "partners", "stakeholders",
    "problem", "problems", "issues", "needs", "level", "quality", "fast",
    "paced", "fast-paced", "industry", "market", "markets", "organization",
    "organizations", "global", "time", "times", "including",
    # A posting's own structure and hedging (requirements._NOT_A_SKILL agrees).
    "minimum", "maximum", "proficiency", "familiarity", "understanding",
    "expertise", "background", "comfort", "ability", "abilities",
})

# Curated multi-word and single tech terms that should always be captured when
# present in the JD — these are the terms a recruiter actually types into the
# ATS boolean search, which is what makes a parsed resume surface at all.
_TECH_PHRASES: List[str] = [
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
_TECH_PHRASE_SET = frozenset(_TECH_PHRASES)


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


# Legal-form and generic words in a company's name, so "Horizon3Ai" is known by
# "horizon3" and "Acme Labs Inc" by "acme".
_COMPANY_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "corp", "corporation", "co", "company", "group",
    "holdings", "labs", "lab", "technologies", "technology", "tech", "ai",
    "io", "hq", "the", "plc", "gmbh", "limited", "incorporated", "international",
    "global", "usa", "us", "and",
})


def _company_terms(company: str) -> frozenset:
    """The forms a posting names its own company by, lower-case.

    "Horizon3Ai" → {"horizon3ai", "horizon3"}; "Acme Labs, Inc." → {"acmelabsinc",
    "acme"}. The employer's own name is not a keyword a candidate can lack: it
    reached the Tailoring Studio as "Not covered: horizon3" and as an "I've used
    HORIZON3" button (live test, 2026-10-09).
    """
    raw = (company or "").strip()
    if not raw:
        return frozenset()
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)     # Horizon3Ai → Horizon3 Ai
    words = re.findall(r"[a-z0-9]+", spaced.lower())
    terms = {"".join(words)}
    core = [w for w in words if w not in _COMPANY_SUFFIXES]
    if core:
        # "horizon3" for Horizon3Ai. Never a lone word of a longer name: the
        # "data" of "Data Robot" is not the company.
        terms.add("".join(core))
        terms.add(" ".join(core))
    return frozenset(t for t in terms if len(t) >= 3)


def _names_company(phrase: str, terms: frozenset) -> bool:
    """True when ``phrase`` is the company's name or built on it."""
    if not terms:
        return False
    p = (phrase or "").lower().strip()
    if p in _TECH_PHRASE_SET:
        # A curated technology stays a keyword even at the company that makes
        # it: a MongoDB posting still wants MongoDB.
        return False
    compact = re.sub(r"[^a-z0-9]+", "", p)
    if compact in terms or p in terms:
        return True
    return any(tok in terms for tok in re.findall(r"[a-z0-9]+", p) if len(tok) >= 4)


def _company_pattern(company: str):
    """A regex for the company's name as a posting writes it ("Horizon3.ai",
    "Horizon3 AI", "horizon3"), or None. Used to cut the name out of the text
    before phrases are counted, so it neither ranks nor glues phrases together
    across it ("ai culture values" from "Horizon3.ai culture values")."""
    raw = (company or "").strip()
    if not raw:
        return None
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    words = re.findall(r"[a-z0-9]+", spaced.lower())
    core = [w for w in words if w not in _COMPANY_SUFFIXES]
    if not core or len(core[0]) < 3:
        return None
    head = r"[\s.\-]*".join(re.escape(w) for w in core)
    tail = "".join(rf"(?:[\s.,\-]*{re.escape(w)})?" for w in words[words.index(core[-1]) + 1:])
    return re.compile(rf"(?<![a-z0-9]){head}{tail}(?![a-z0-9])")


def _is_noise_phrase(phrase: str) -> bool:
    """A phrase that names nothing a person can have: a single generic word, a
    sentence fragment (a stop word inside it), or a phrase made only of
    function words and generic talk ("customer team")."""
    tokens = phrase.split()
    if len(tokens) == 1:
        t = tokens[0]
        return (t in _STOP or t in _FLUFF or t in _UNIGRAM_VERBS or t in _TITLE_TOKENS
                or t == "success")
    if any(t in _STOP and t not in _INNER_OK for t in tokens[1:-1]):
        return True
    # "remote cybersecurity company", "mission of enabling", "salary range":
    # the company describing itself or the offer, not a skill.
    if tokens[0] in _SELF_TALK or tokens[-1] in _SELF_TALK:
        return True
    return all(t in _STOP or t in _FLUFF for t in tokens)


def _named_tokens(text: str) -> frozenset:
    """Lower-cased words the posting writes like a NAME somewhere: capitalised
    away from the start of a sentence ("Splunk", "Jira", "ServiceNow"), or
    carrying a digit or symbol ("s3", "c#"). A word the posting uses once and
    writes in lower case ("counts", "alone") is prose, not a tool."""
    out = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#/\-]*", text or ""):
        word = m.group(0)
        if re.search(r"[0-9+#]", word):
            out.add(word.lower())
            continue
        if not re.search(r"[A-Z]", word):
            continue
        before = (text or "")[:m.start()].rstrip(" \t\"'“(")
        if before and before[-1] not in ".!?:;\n•-*–—":
            out.add(word.lower())
    return frozenset(out)


def extract_jd_phrases(jd_text: str, top_n: int = 18, company: str = "") -> List[str]:
    """Extract the top N high-signal exact phrases from a job description.

    Combines:
      - curated tech phrases present in the JD (always included, ranked first)
      - frequent 2-3 word n-grams not starting/ending on stop words
      - frequent meaningful single tokens

    Never returns an English function word, a generic word on its own
    ("customer", "culture") or, when ``company`` is given, the hiring company's
    own name: only real skills, tools and domains.
    """
    company_terms = _company_terms(company)
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
    # The company's own name is cut out first (curated terms above already read
    # the full text, so a MongoDB posting still finds "mongodb").
    company_re = _company_pattern(company)
    phrase_text = company_re.sub(" . ", norm) if company_re else norm
    segments = re.split(r"[.;:!?,\n\r\t()\[\]]+|\s[-–]\s", phrase_text)
    ngram_freq: Dict[str, int] = {}

    def _count_segment(tokens: List[str], seq_len: int) -> None:
        for i in range(len(tokens) - seq_len + 1):
            gram = tokens[i:i + seq_len]
            if gram[0] in _STOP or gram[-1] in _STOP:
                continue
            if any(len(t) < 2 for t in gram):
                continue
            phrase = " ".join(gram)
            if _is_noise_phrase(phrase):
                continue
            ngram_freq[phrase] = ngram_freq.get(phrase, 0) + 1

    for seg in segments:
        seg_tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9+#/\-]*(?:\.[a-zA-Z]+)?", seg)
        _count_segment(seg_tokens, 3)
        _count_segment(seg_tokens, 2)
        for t in seg_tokens:
            if len(t) < 3 or _is_noise_phrase(t):
                continue
            ngram_freq[t] = ngram_freq.get(t, 0) + 1

    # Score: longer phrases and repeated phrases rank higher
    def _score(item: Tuple[str, int]) -> float:
        phrase, freq = item
        word_count = phrase.count(" ") + 1
        # multi-word phrases get a length multiplier; repetition matters most
        return freq * (1.0 + 0.6 * (word_count - 1))

    # What the posting REPEATS ranks first. A run of words written once is
    # usually a sentence ("organizations to proactively", "find and fix"), and
    # the length multiplier let it outscore a word the posting uses twice; on
    # a long posting that pushed real terms out of the top N. Once-seen phrases
    # still rank (longest first) when nothing repeated is left, which a short
    # posting needs: "Amazon Web Services" written once must stay one phrase.
    ranked = sorted(ngram_freq.items(),
                    key=lambda item: (item[1] >= 2, _score(item)), reverse=True)

    # 3. Merge: curated tech hits first, then top-ranked n-grams
    result: List[str] = list(tech_hits)
    named = _named_tokens(_strip_html(jd_text))

    def _within(longer: str, shorter: str) -> bool:
        return longer != shorter and f" {shorter} " in f" {longer} "

    for phrase, freq in ranked:
        if len(result) >= top_n:
            break
        if phrase in result:
            continue
        if _is_noise_phrase(phrase) or _names_company(phrase, company_terms):
            continue
        # skip n-grams already covered by a curated phrase
        if any(phrase in t or t in phrase for t in tech_hits):
            continue
        if freq < 2 and (not any(tok in named for tok in phrase.split())
                         or re.search(r" (?:and|or) ", phrase)):
            # Written once and in lower case ("counts", "customers rely",
            # "images run"): prose, not a keyword. Written once as "A and B":
            # a list of two things, not one. "Amazon Web Services" or "Jira"
            # written once still counts; a curated term always does.
            continue
        picked = result[len(tech_hits):]
        if any(_within(k, phrase) for k in picked):
            continue                   # a higher-ranked phrase already says it
        inside = [k for k in picked if _within(phrase, k)]
        if inside:
            # A REPEATED longer phrase is the more specific keyword ("security
            # tools" over "security"); a once-seen one is a sentence fragment
            # and never pushes out a term the posting repeats ("customer
            # configures integrations" must not displace "integrations").
            if freq < 2:
                continue
            result = [r for r in result if r not in inside]
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


# ── which phrases are safe to hand a generator as "use these EXACT words" ────
# The n-gram ranker is a frequency counter, so it happily returns fragments of
# the job title and the marketing copy: "learning engineer ai", "engineer ai
# platform", "ai platform company", "backbone for enterprise", "design and own",
# "own ml systems". Those are fine as *coverage* signals — they are genuinely
# what the JD repeats — but the tailor prompt tells the model to "incorporate
# the EXACT phrasing", and a model told to work "design and own" and "GPU
# clusters" into a résumé does exactly that. Lifting a JD responsibility line
# into someone's employment history is precisely the fabrication grounding then
# has to catch and pay to rebuild. So the tailor gets a filtered list; coverage
# measurement keeps the full one.

# Verbs and role/marketing nouns that make a phrase a sentence fragment rather
# than a thing a person can have on their résumé.
_NON_SKILL_TOKENS = frozenset({
    "design", "designing", "own", "owning", "owns", "drive", "driving", "lead",
    "leads", "leading", "collaborate", "collaborating", "partner", "write",
    "writes", "writing", "ship", "ships", "shipping", "deliver", "delivers",
    "support", "supporting", "improve", "improving", "grow", "growing",
    "scale", "scaling", "hire", "hiring", "mentor", "mentoring", "want",
    "wants", "need", "needs", "love", "loves", "care", "make", "makes",
    "company", "companies", "startup", "mission", "culture", "customers",
    "customer", "clients", "product", "products", "business", "opportunity",
    "candidate", "candidates", "applicants", "benefits", "compensation",
    "salary", "equity", "bonus", "backbone", "world", "future", "impact",
    "everything", "anything", "someone", "people", "person", "day", "days",
})
# Job-title words. A phrase built out of these is describing the req, not a skill.
_TITLE_TOKENS = frozenset({
    "engineer", "engineers", "engineering", "developer", "developers",
    "manager", "scientist", "scientists", "architect", "analyst", "intern",
    "lead", "senior", "junior", "staff", "principal", "director", "head",
    "specialist", "consultant", "contractor", "founding",
})
# Single words that are a JD's verbs and marketing nouns, never a keyword on
# their own. "design" stays: for a designer it is the field itself.
_UNIGRAM_VERBS = _NON_SKILL_TOKENS - {"design", "designing"}


def is_skill_like(phrase: str) -> bool:
    """True when a phrase names something a candidate can actually possess.

    Curated technology terms always qualify. Anything else must be free of
    sentence glue, generic verbs, marketing nouns and job-title words.
    """
    p = (phrase or "").strip().lower()
    if not p:
        return False
    if p in _TECH_PHRASE_SET:
        return True
    tokens = p.split()
    if not tokens:
        return False
    # Mid-phrase stop words are the tell for a clause fragment: the ranker
    # already refuses to START or END on one, so "backbone for enterprise" and
    # "design and own" are exactly what slips through.
    if any(t in _STOP for t in tokens):
        return False
    if any(t in _NON_SKILL_TOKENS for t in tokens):
        return False
    if any(t in _TITLE_TOKENS for t in tokens):
        return False
    return True


def skill_phrases(phrases: List[str]) -> List[str]:
    """Filter a ranked phrase list down to the ones safe to target verbatim."""
    return [p for p in phrases if is_skill_like(p)]


def analyze(jd_text: str, resume_text: str, top_n: int = 18,
            company: str = "") -> ATSKeywordReport:
    """Full report: top JD phrases, which are matched verbatim, which are missing.

    Pass the hiring ``company`` whenever it is known, so its own name is never
    reported as a keyword the resume lacks."""
    phrases = extract_jd_phrases(jd_text, top_n=top_n, company=company)
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
