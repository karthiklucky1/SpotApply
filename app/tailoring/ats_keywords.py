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
    "matters", "need", "needs", "want", "wants", "love", "loves",
    # Adjectives a posting uses about itself, never about a skill.
    "dedicated", "committed", "fully", "eligible", "various", "numerous",
    # Sentence adverbs and pairings that open a clause, never a skill.
    "however", "additionally", "ideally", "finally", "overall", "typically",
    "specifically", "instead", "therefore", "furthermore", "moreover",
    "preferably", "and/or", "he/she", "s/he", "his/her", "him/her",
}
# Small words that DO sit inside real skill phrases ("infrastructure as code",
# "proof of concept", "research and development"). Any other stop word inside a
# phrase marks a sentence fragment ("works where cybersecurity").
_INNER_OK = frozenset({"of", "as", "a", "an", "and", "the", "for", "to", "in", "on",
                       "with", "by", "at", "or"})

# The company describing itself, the offer and equal-opportunity boilerplate.
# None of these heads or ends a skill in any field, so a phrase holding one is
# never a keyword ("cybersecurity company", "competitive salary", "equal
# opportunity employer"), and a word that sat next to one is not promoted to a
# keyword on that occurrence (see _orphaned).
_SELF_TALK = frozenset({
    "company", "companies", "startup", "startups", "employer", "employers",
    "opportunity", "opportunities", "position", "positions", "salary",
    "salaries", "bonus", "bonuses", "perks", "headquarters", "hq", "values",
    "ideal", "passion", "passionate", "exciting", "excited", "awesome",
    "amazing", "everything", "anything", "someone", "future", "belong",
    "belonging", "perspectives", "commitment", "respect", "respected", "valued",
    "rewarded", "vacation", "parental", "annually", "legally", "marital", "sex",
    "age", "origin", "potential", "unique", "welcome", "qualified",
    "individuals", "duties", "notice", "creativity", "innovative", "careers",
    "jobs", "roles", "members", "employees", "equal", "day", "days", "world",
    "pay",
})
# Words a posting also uses about itself ("our culture", "competitive
# benefits", "growth opportunities") that NAME A FIELD inside a phrase: cell
# culture, gene expression, employee relations, benefits administration, public
# policy, dental hygiene, process improvement, family medicine, people
# operations, identity management, growth marketing, health equity. Never a
# keyword on their own; a phrase on one is dropped only when every other word
# in it is generic too ("inclusive culture", "dental insurance").
_FIELD_TALK = frozenset({
    "culture", "teams", "mission", "missions", "vision", "candidate",
    "candidates", "applicant", "applicants", "people", "person", "benefits",
    "equity", "compensation", "impact", "employee", "accommodation",
    "accommodations", "interview", "process", "growth", "career", "job",
    "diversity", "inclusion", "inclusive", "community", "communities",
    "workplace", "workforce", "insurance", "dental", "leave", "policy", "family",
    "travel", "range", "stock", "options", "package", "protected", "national",
    "identity", "expression", "genetic", "color", "contributions", "listing",
    "materials", "information", "competitive", "dynamic", "innovation",
    "results", "ownership", "collaboration", "collaborative", "offer", "member",
    "duty", "care",
})
# Words that are never a keyword ON THEIR OWN even when the posting repeats
# them. "customer" reached the "Not covered" list for a support engineering
# role; nobody is "missing" customer. Inside a phrase they still count
# ("customer support", "product design", "customer success"), because the
# phrase names real work.
_FLUFF = _SELF_TALK | _FIELD_TALK | frozenset({
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
    # Job metadata lines ("Employment Type", "Full-time", "Department") and
    # the posting's own furniture ("Job Description", "background check",
    # "reasonable accommodation", "hiring process", "pay transparency").
    "employment", "type", "department", "full-time", "part-time", "permanent",
    "temporary", "contract", "seasonal", "description", "summary", "overview",
    "reasonable", "check", "checks", "hiring", "transparency",
})
# Too broad to be a keyword alone, yet a real word inside one ("health
# equity", "health policy", "medical assistant").
_ALONE_ONLY = frozenset({"health", "medical"})
# Phrases made only of generic words that still name a field of work.
_FIELD_PHRASES = frozenset({
    "customer service", "customer services", "client service", "client services",
    "customer care", "member services", "guest services", "health care",
    "medical care",
})
# A posting's verbs: a phrase that OPENS on one is a clause ("conduct public
# policy research", "deliver customer service", "writing apex"), never the
# keyword, and they count as generic beside a field word ("drive process").
_CLAUSE_VERBS = frozenset({
    "own", "owns", "owning", "drive", "drives", "leads", "collaborate",
    "collaborates", "collaborating", "write", "writes", "writing", "ship",
    "ships", "deliver", "delivers", "delivering", "supporting", "improve",
    "improves", "improving", "grow", "grows", "growing", "hire", "make",
    "making", "provide", "providing", "maintain", "maintains", "maintaining",
    "conduct", "conducts", "conducting", "configure", "configuring", "perform",
    "join", "apply", "embrace", "fostering", "promote", "promoting", "encourage",
    "encourages", "strive", "thrive", "believe", "enjoy", "receive", "offer",
})
_GENERIC = frozenset(_STOP) | _FLUFF | _CLAUSE_VERBS

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


_LIST_TAG_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)
_BLOCK_TAG_RE = re.compile(
    r"</?(?:li|ul|ol|p|div|br|h[1-6]|tr|td|th|dd|dt|dl|table|section|article)\b[^>]*>",
    re.IGNORECASE)


def _strip_html(text: str) -> str:
    """Remove HTML tags/entities so markup ('<ul><li>') never becomes a
    "missing keyword" chip like 'ul li'. A list item stays its own line with a
    bullet and a block ends its line, so "<li>Tableau</li><li>Looker</li>" is
    two list items, not the phrase "tableau looker"."""
    import html as _html
    text = _html.unescape(text or "")          # an escaped body ("&lt;li&gt;") is markup too
    text = _LIST_TAG_RE.sub("\n• ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    # Only a real tag: "<3 years" or "<$100k" in the prose is text, not markup.
    text = re.sub(r"<[A-Za-z!/][^<>]*>", " ", text)
    return _html.unescape(text)


# Legal-form and generic words in a company's name, so "Horizon3Ai" is known by
# "horizon3" and "Acme Labs Inc" by "acme".
_COMPANY_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "corp", "corporation", "co", "company", "group",
    "holdings", "labs", "lab", "technologies", "technology", "tech", "ai",
    "io", "hq", "the", "plc", "gmbh", "limited", "incorporated", "international",
    "global", "usa", "us", "and",
})


# What a company slug can end in when the posting writes the rest on its own
# ("palantirtechnologies" -> "Palantir", "scaleai" -> "Scale").
_SLUG_SUFFIXES = ("technologies", "technology", "international", "incorporated",
                  "corporation", "holdings", "limited", "group", "labs", "lab", "tech",
                  "corp", "inc", "llc", "ltd", "ai", "io", "hq")
# Stands in for the company's name once it is cut from the text, so a word
# that sat beside the name is known as such (see _orphaned).
_CO_MARK = "zzcompanyzz"
_SENTENCE_END = ".!?:;\n•·▪◦‣-*–—)"


def _split_name(raw: str) -> List[str]:
    """'Horizon3Ai' -> ['horizon3', 'ai']; 'Acme Labs, Inc.' -> ['acme', 'labs', 'inc']."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw or "")
    return re.findall(r"[a-z0-9]+", spaced.lower())


def _posting_spelling(slug: str, text: str) -> List[str]:
    """How the posting itself writes a company known only by a one-word slug.

    A board slug run through str.title() has no internal capital ("Scaleai",
    "Palantirtechnologies"), so it cannot be split on its own, while the
    posting writes "Scale AI" or "Palantir Technologies". Returns the posting's
    words for the name, or [] when it never writes it in parts.
    """
    if len(slug) < 4 or not text:
        return []
    toks = list(re.finditer(r"[A-Za-z0-9]+", text))
    for i, first in enumerate(toks):
        if not slug.startswith(first.group(0).lower()):
            continue
        joined = ""
        for j in range(i, min(i + 4, len(toks))):
            if j > i and not re.fullmatch(r"[ \t.\-]{1,3}", text[toks[j - 1].end():toks[j].start()]):
                break
            joined += toks[j].group(0).lower()
            if joined == slug:
                words = _split_name(" ".join(t.group(0) for t in toks[i:j + 1]))
                if len(words) > 1:
                    return words
                break
            if not slug.startswith(joined):
                break
    for suffix in _SLUG_SUFFIXES:
        head = slug[:-len(suffix)]
        if (slug.endswith(suffix) and len(head) >= 3
                and re.search(rf"(?<![A-Za-z0-9]){re.escape(head)}(?![A-Za-z0-9])", text,
                              re.IGNORECASE)):
            return [head, suffix]
    return []


def _company_forms(company: str, text: str = "") -> Tuple[List[str], List[str], bool]:
    """(the name's words, its core words, whether the core alone is the company).

    The core is the name without legal or generic words ("horizon3" of
    Horizon3Ai). A one-word core that is an ordinary word stands for the
    company only when the posting never writes it in lower case and it is not
    a known skill: "scale" in "at scale" is not Scale AI, "open" in "open
    source" is not OpenAI, and "git" is a tool even at GitLab.
    """
    words = _split_name((company or "").strip())
    if len(words) == 1 and text:
        words = _posting_spelling(words[0], text) or words
    core = [w for w in words if w not in _COMPANY_SUFFIXES]
    if not core:
        return words, [], False
    alone = (core == words or len(core) > 1 or bool(re.search(r"\d", core[0]))
             or not text
             or (not _is_known_skill(core[0])
                 and not re.search(rf"(?<![A-Za-z0-9]){re.escape(core[0])}(?![A-Za-z0-9])",
                                   text)))
    return words, core, alone


def _company_terms(company: str, text: str = "") -> frozenset:
    """The forms a posting names its own company by, lower-case.

    "Horizon3Ai" → {"horizon3ai", "horizon3"}; "Acme Labs, Inc." → {"acmelabsinc",
    "acme"}; the slug "Scaleai" → {"scaleai", "scale ai"} (read against the
    posting's own "Scale AI"). The employer's own name is not a keyword a
    candidate can lack: it reached the Tailoring Studio as "Not covered:
    horizon3" and as an "I've used HORIZON3" button (live test, 2026-10-09).
    """
    words, core, alone = _company_forms(company, text)
    if not words:
        return frozenset()
    terms = {"".join(words), " ".join(words)}
    if core and alone:
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


def _cut_company(text: str, company: str) -> str:
    """The posting with its company's name ("Horizon3.ai", "Horizon3 AI",
    "horizon3", "Scale AI") replaced by a marker, so the name neither ranks nor
    glues phrases together across it ("ai culture values" from "Horizon3.ai
    culture values"). A one-word core that is an ordinary word ("Scale") is
    cut only where the posting writes it as a name, mid-sentence."""
    words, core, alone = _company_forms(company, text)
    if not core or len(core[0]) < 3:
        return text
    head = r"[\s.\-]*".join(re.escape(w) for w in core)
    after = words[words.index(core[-1]) + 1:]
    optional = "".join(rf"(?:[\s.,\-]*{re.escape(w)})?" for w in after[1:])
    if alone or not after:
        tail = (rf"(?:[\s.,\-]*{re.escape(after[0])})?" if after else "") + optional
    else:
        tail = rf"[\s.\-]*{re.escape(after[0])}" + optional
    mark = f" {_CO_MARK} "
    out = re.sub(rf"(?<![A-Za-z0-9]){head}{tail}(?![A-Za-z0-9])", mark, text,
                 flags=re.IGNORECASE)
    if not alone and len(core) == 1 and not _is_known_skill(core[0]):
        name = core[0]

        def _as_name(m):
            before = m.string[max(0, m.start() - 40):m.start()].rstrip(" \t\"'“(")
            return mark if before and before[-1] not in _SENTENCE_END else m.group(0)
        out = re.sub(rf"(?<![A-Za-z0-9])(?:{re.escape(name.capitalize())}|{re.escape(name.upper())})"
                     rf"(?![A-Za-z0-9])", _as_name, out)
    return out


def _is_noise_phrase(phrase: str) -> bool:
    """A phrase that names nothing a person can have: a single generic word, a
    sentence fragment (a stop word inside it), the company talking about
    itself or the offer, a clause opening on a verb, or a phrase made only of
    function words and generic talk ("customer team")."""
    tokens = phrase.split()
    if not tokens or _CO_MARK in tokens:
        return True
    if len(tokens) == 1:
        t = tokens[0]
        return (t in _STOP or t in _FLUFF or t in _ALONE_ONLY or t in _UNIGRAM_VERBS
                or t in _TITLE_TOKENS or t == "success")
    if any(t in _STOP and t not in _INNER_OK for t in tokens[1:-1]):
        return True
    # "remote cybersecurity company", "competitive salary", "equal opportunity
    # employer": the company describing itself or the offer, not a skill.
    if any(t in _SELF_TALK for t in tokens):
        return True
    if tokens[0] in _CLAUSE_VERBS:
        return True
    if phrase in _FIELD_PHRASES:
        return False
    # "inclusive culture" and "dental insurance" are the offer; "cell culture"
    # and "dental hygiene" are a field, because the other word is real.
    if ((tokens[0] in _FIELD_TALK or tokens[-1] in _FIELD_TALK)
            and all(t in _GENERIC for t in tokens if t not in _FIELD_TALK)):
        return True
    return all(t in _STOP or t in _FLUFF for t in tokens)


def _named_tokens(text: str) -> frozenset:
    """Lower-cased words the posting writes like a NAME somewhere: capitalised
    away from the start of a sentence ("Splunk", "Jira", "ServiceNow"), or
    shaped like one wherever they stand, even opening a line: a digit or symbol
    ("s3", "c#"), an internal capital ("PowerPoint", "qPCR"), a short slashed
    term ("A/B", "CI/CD") or an acronym ("SQL", "ACLS") on a line that is not
    itself all capitals (a heading). A word the posting uses once and writes in
    lower case ("counts", "alone") is prose, not a tool."""
    out = set()
    for line in (text or "").splitlines():
        heading = not re.search(r"[a-z]", line)
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#/\-]*", line):
            word = m.group(0)
            low = word.lower()
            if low in _STOP:
                continue
            if (re.search(r"[0-9+#]", word) or re.search(r"[a-z][A-Z]", word)
                    or ("/" in word and all(1 <= len(p) <= 4 for p in word.split("/")))
                    or (word.isupper() and 2 <= len(word) <= 6 and not heading)):
                out.add(low)
                continue
            if not re.search(r"[A-Z]", word):
                continue
            before = line[:m.start()].rstrip(" \t\"'“(")
            if before and before[-1] not in _SENTENCE_END:
                out.add(low)
    return frozenset(out)


# ── words known to be skills, so a lower-case mention still counts ──────────
# Tools a posting writes in lower case, or opens a line with, that a
# capitalisation rule cannot see. The skill graph (data/skill_graph.json)
# adds the technical ones it knows (pandas, numpy, dbt, pytest ...).
_KNOWN_TOOLS = frozenset({
    "pandas", "numpy", "scipy", "matplotlib", "seaborn", "jupyter", "pytest", "dbt",
    "git", "bash", "npm", "yarn", "webpack", "kubectl", "helm", "nginx", "ansible",
    "excel", "tableau", "looker", "workday", "netsuite", "quickbooks", "hubspot",
    "salesforce", "apex", "jira", "confluence", "figma", "photoshop", "autocad",
    "solidworks", "matlab", "stata", "spss", "zendesk", "servicenow", "splunk",
})
# Skill-graph names that are also everyday English ("express interest", "react
# to", "swift action"): written once in lower case they are prose.
_AMBIGUOUS_SKILLS = frozenset({
    "express", "swift", "node", "react", "go", "lambda", "spark", "rust", "flutter",
    "angular", "vue", "tailwind", "py", "js", "ts", "tf", "cv", "ml", "dl",
})
_KNOWN_CACHE: Dict[int, frozenset] = {}


def _known_skills() -> frozenset:
    """Curated terms + the skill graph's names + _KNOWN_TOOLS, hyphens as spaces."""
    graph = None
    try:
        from app.matching.skill_graph import load_graph
        graph = load_graph()
    except Exception:
        graph = None
    key = id(graph)
    cached = _KNOWN_CACHE.get(key)
    if cached is not None:
        return cached
    names = set(_KNOWN_TOOLS) | set(_TECH_PHRASE_SET)
    if graph is not None:
        try:
            names |= set(graph.aliases) | set(graph.aliases.values()) | set(graph.edges)
            names |= {d for m in graph.edges.values() for d in m}
        except Exception:
            pass
    known = frozenset(n.replace("-", " ") for n in names if n) - _AMBIGUOUS_SKILLS
    _KNOWN_CACHE.clear()
    _KNOWN_CACHE[key] = known
    return known


def _is_known_skill(phrase: str) -> bool:
    return (phrase or "").lower().replace("-", " ").strip() in _known_skills()


# ── list items: a short line in a list is a keyword, not prose ──────────────
_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪◦‣–]|\(?\d{1,2}[.)])\s+")
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+#/\-]*(?:\.[a-zA-Z]+)?")
# A line that opens like a sentence or a heading ("What you'll bring", "We
# offer") is not a list item.
_LINE_OPENERS = frozenset({
    "what", "who", "why", "how", "where", "when", "you", "you'll", "you're", "we",
    "we're", "we'll", "our", "your", "they", "it", "this", "that", "these", "those",
    "the", "a", "an", "about", "if", "here", "there", "i", "please", "all", "any",
})
_OFFER_WORDS = frozenset({"benefits", "benefit", "perks", "perk", "compensation",
                          "salary", "offer", "rewards", "pay"})
_SECTION_WORDS_RE = re.compile(
    r"\b(?:requirements?|qualifications?|responsibilities|skills|duties|role|you|your|"
    r"about|preferred|nice|bring|experience|position|job|overview|description)\b",
    re.IGNORECASE)


def _without_offer(text: str) -> str:
    """The posting minus its offer: the lines under a "Benefits", "Perks of
    Acme", "Compensation and Values" or "Why join us" heading, up to the next
    heading about the job ("Requirements", "What you'll do", "About the
    role"). "Unlimited PTO", "401(k) matching" and "Medical, dental and
    vision" describe the employer, never a skill the resume lacks."""
    out: List[str] = []
    in_offer = False
    for line in (text or "").splitlines():
        body = _BULLET_RE.sub("", line).strip()
        words = [w.lower() for w in _TOKEN_RE.findall(body)]
        if words and not _BULLET_RE.match(line) and len(body.split()) <= 5:
            if ((words[0] == "why" or _OFFER_WORDS & set(words))
                    and all(w in _GENERIC or w == _CO_MARK for w in words)):
                in_offer = True
                out.append("")
                continue
            if _SECTION_WORDS_RE.search(body):
                in_offer = False
        out.append("" if in_offer else line)
    return "\n".join(out)


def _list_items(text: str) -> frozenset:
    """The phrases the posting lists as items: a bulleted line, or one of
    adjacent short lines (how an HTML list reads once tags are stripped), of
    four words or fewer, split on "and"/"or"/commas, with leading and trailing
    stop words dropped ("Experience with dbt" -> "dbt"). Read it from
    _without_offer(): a benefits list is the offer, not the job."""
    lines = (text or "").splitlines()
    bodies = [_BULLET_RE.sub("", ln).strip() for ln in lines]

    def short(j: int) -> bool:
        return 0 <= j < len(lines) and bool(bodies[j]) and len(bodies[j].split()) <= 4

    out = set()
    for i, line in enumerate(lines):
        body = bodies[i]
        bullet = bool(_BULLET_RE.match(line))
        words = [w.lower() for w in _TOKEN_RE.findall(body)]
        if not body or ":" in body or len(body.split()) > 4 or not words:
            continue
        if not (bullet or short(i - 1) or short(i + 1)):
            continue
        if not (bullet or body[0].isupper() or body[0].isdigit()):
            continue                       # a line break inside a sentence
        if words[0] in _LINE_OPENERS:
            continue
        for part in re.split(r",|;|\s(?:and|or|&)\s|\s/\s", body.lower()):
            toks = _TOKEN_RE.findall(part)
            while toks and toks[0] in _STOP:
                toks.pop(0)
            while toks and toks[-1] in _STOP:
                toks.pop()
            if toks:
                out.add(" ".join(toks))
    return frozenset(out)


def _counts_seen_once(phrase: str, named: frozenset, listed: frozenset,
                      known: frozenset) -> bool:
    """Whether a phrase the posting writes only once is still a keyword: a list
    item, a known skill (pandas, dbt, Excel), or a phrase with a word written
    like a name ("Amazon Web Services", "Jira", "SQL"). A once-seen "A and B"
    is two things, not one."""
    if phrase in listed or phrase.replace("-", " ") in known:
        return True
    if re.search(r" (?:and|or) ", phrase):
        return False
    return any(tok in named for tok in phrase.split())


def _continues_kept(phrase: str, kept: List[str], text: str) -> bool:
    """True when a once-seen phrase is the overhang of a kept one: "policy
    degree" from "public policy degree", "service representative" from
    "customer service representative". The kept phrase already names the
    skill; its leftover half is a fragment, not a second keyword."""
    words = phrase.split()
    for k in kept:
        kw = k.split()
        if len(kw) < 2:
            continue
        if len(words) == 1:
            # "representative" written only in "Customer Service Representative"
            joined = (f"{k} {phrase}", f"{phrase} {k}")
        elif words[0] == kw[-1]:
            joined = (" ".join(kw + words[1:]),)
        elif words[-1] == kw[0]:
            joined = (" ".join(words[:-1] + kw),)
        else:
            continue
        if any(re.search(rf"(?<![\w-]){re.escape(j)}(?![\w-])", text) for j in joined):
            return True
    return False


def _orphaned(run: List[str], i: int, cut_before: bool, cut_after: bool) -> bool:
    """True when this occurrence of a word stood next to the company's name or
    company/offer boilerplate. Its phrase was dropped, and the word alone is
    not promoted on that occurrence: "Salesforce Administrator" must not leave
    "administrator", nor "a cybersecurity company" leave a keyword that only
    that sentence used."""
    return ((i == 0 and cut_before) or (i == len(run) - 1 and cut_after)
            or (i > 0 and run[i - 1] in _SELF_TALK)
            or (i + 1 < len(run) and run[i + 1] in _SELF_TALK))


def extract_jd_phrases(jd_text: str, top_n: int = 18, company: str = "") -> List[str]:
    """Extract the top N high-signal exact phrases from a job description.

    Combines:
      - curated tech phrases present in the JD (always included, ranked first)
      - frequent 2-3 word n-grams not starting/ending on stop words
      - frequent meaningful single tokens

    Never returns an English function word, a generic word on its own
    ("customer", "culture") or, when ``company`` is given, the hiring company's
    own name: only real skills, tools and domains, in any field.
    """
    plain = _strip_html(jd_text)
    company_terms = _company_terms(company, plain)
    norm = _normalize(plain)

    # 1. Curated tech phrases that actually appear in this JD
    tech_hits: List[str] = []
    for term in _TECH_PHRASES:
        if _phrase_present(term, norm):
            tech_hits.append(term)
    # De-dupe overlapping curated terms (prefer longer phrase, drop contained shorter)
    tech_hits = _dedupe_contained(tech_hits)

    # 2. Frequency-counted n-grams (2 and 3 word) and unigrams.
    # Segment on sentence/clause punctuation AND line breaks first so n-grams
    # never span a boundary (e.g. "engineer. build large", or two list items
    # "acls patient education"). Tech tokens like node.js and ci/cd survive
    # because they have no period-followed-by-space.
    # The company's own name is cut out first (curated terms above already read
    # the full text, so a MongoDB posting still finds "mongodb").
    # The benefits/perks section is the offer, never a keyword source.
    job_text = _without_offer(_cut_company(plain, company) if company else plain)
    phrase_text = re.sub(r"[ \t\r\f\v]+", " ", job_text.lower())
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
        seg_tokens = _TOKEN_RE.findall(seg)
        # The company's marker splits a segment into runs; a word beside it is
        # an orphan on that occurrence.
        start = 0
        for end in [k for k, t in enumerate(seg_tokens) if t == _CO_MARK] + [len(seg_tokens)]:
            run = seg_tokens[start:end]
            cut_before = start > 0
            cut_after = end < len(seg_tokens)
            start = end + 1
            _count_segment(run, 3)
            _count_segment(run, 2)
            for i, t in enumerate(run):
                if len(t) < 3 or _is_noise_phrase(t) or _orphaned(run, i, cut_before, cut_after):
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
    named = _named_tokens(job_text)
    listed = _list_items(job_text)
    known = _known_skills()

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
        if freq < 2 and not _counts_seen_once(phrase, named, listed, known):
            # Written once and in lower case ("counts", "customers rely",
            # "images run"): prose, not a keyword. "Amazon Web Services",
            # "Jira", "pandas" or a bulleted "Wound care" written once still
            # counts; a curated term always does.
            continue
        if freq < 2 and _continues_kept(phrase, result, phrase_text):
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
