"""Trusted evidence — parse the master résumé ONCE, reuse it everywhere.

Grounding, the Doctor's integrity anchors and the fabrication guard all used to
re-derive "what does this person actually claim" from raw markdown, separately,
on every call. Grounding then went further and re-sent the whole master résumé
to the LLM once per bullet — 1,423 input tokens to answer a five-token question,
fourteen times over, which a measured audit put at 64% of a tailor's total cost.

The fix is to build one immutable, content-addressed representation of the
master and hand *pieces* of it to whoever needs them:

  * ``Evidence.evidence_id`` — sha256 of the normalized master. Two résumés with
    the same evidence_id are the same evidence, so a verdict computed against
    one is valid for the other. This is what makes the verification cache safe.
  * ``Evidence.spans`` — the claim-bearing lines, each with its own content hash,
    so a generated span can be paired with the ONE source span it came from
    instead of the entire document.
  * ``Evidence.facts`` — the deterministic fact sets (employers, titles, dates,
    degrees, institutions, certifications, numbers). Set difference in the
    ADDITION direction is a hallucination detector that needs no model and
    cannot itself hallucinate: anything factual in the output that is not in the
    input is, by construction, invented.

Nothing here calls an LLM or the network.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

# ── normalization ────────────────────────────────────────────────────────────

_MD_NOISE_RE = re.compile(r"[*_`]+")
_BULLET_GLYPHS = "-*•·–—‣▪◦●» \t"


def normalize_text(s: str) -> str:
    """Case/whitespace/markdown-insensitive form used for identity comparisons.

    Bold markers are stripped because ``**FastAPI**`` and ``FastAPI`` are the
    same claim — a tailoring pass that only changes emphasis has not changed a
    fact and must not be charged for a verification call.
    """
    s = _MD_NOISE_RE.sub("", s or "")
    return re.sub(r"\s+", " ", s).strip().lower()


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── fact extraction ──────────────────────────────────────────────────────────

_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"

# "**Senior Backend Engineer** | Acme | Jun 2022 - Mar 2024 | Remote"
_EXPERIENCE_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?\*\*(?P<title>[^*|]+)\*\*\s*\|(?P<rest>.+)$")

_DATE_RANGE_RE = re.compile(
    rf"\b(?:{_MONTHS})[a-z]*\.?\s*\d{{4}}\s*(?:[-–—]|to)\s*"
    rf"(?:(?:{_MONTHS})[a-z]*\.?\s*\d{{4}}|present|current|now|ongoing)",
    re.IGNORECASE,
)
_STANDALONE_MONTH_YEAR_RE = re.compile(rf"\b(?:{_MONTHS})[a-z]*\.?\s*\d{{4}}\b", re.IGNORECASE)

_DEGREE_RE = re.compile(
    r"\b(?:master|bachelor|doctor|associate)(?:'s)?(?:\s+of\s+[a-z][a-z ]{1,40}[a-z])?"
    r"|\bph\.?\s?d\b|\bm\.?b\.?a\b|\bb\.?s\.?c?\b|\bm\.?s\.?c?\b|\bb\.?tech\b|\bm\.?tech\b",
    re.IGNORECASE,
)
_INSTITUTION_RE = re.compile(
    r"(?:[A-Z][\w&.'-]*\s+){0,4}(?:University|College|Institute|Polytechnic|Academy)"
    r"(?:\s+of(?:\s+[A-Z][\w&.'-]*){1,3})?",
)
# Certifications: an explicit "certified/certification" phrase, or a well-known
# credential acronym. Both directions matter — a résumé that gains "AWS Certified
# Solutions Architect" it never had is exactly the fabrication users get caught on.
# Words join on SPACES only: a credential never spans a line, and ``\s`` let
# "AWS Certified Cloud Practitioner" absorb the capitalised word starting the
# next line ("ML …") — a fact the master "lacked", which blocked real tailors.
_CERT_PHRASE_RE = re.compile(
    r"\b(?:[A-Z][\w+.#-]*[ \t]+){0,4}"
    r"(?:Certified|Certification|Certificate)"
    r"(?:[ \t]+[A-Z][\w+.#-]*){0,4}\b",
)
_CERT_ACRONYMS = frozenset({
    "pmp", "cissp", "ccna", "ccnp", "cka", "ckad", "ckm", "cfa", "cpa", "csm",
    "itil", "comptia", "security+", "network+", "aws-sa", "gcp-ace", "az-900",
    "scrum master", "six sigma",
})

# Every number the document asserts, with commas stripped. Compared as a SET of
# VALUES, never as substrings: "5%" is a substring of "45%", so the old
# ``token not in source`` test let a fabricated 5% ride on a real 45%.
_NUMBER_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w.]*\d)")

# Numbers that are structure, not claims: years inside dates are covered by the
# date fact set, and a lone 1-2 digit list index is noise.
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

_SECTION_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
# "…, Columbus, OH" / "… — Remote" — a place, not a claim.
_ORG_LOCATION_RE = re.compile(
    r"(?:,\s*[A-Za-z]{2}|[,—–-]\s*(?:remote|hybrid|onsite|on-site))\s*$", re.I)


def _norm_fact(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().strip(".,;:|-").lower())


_MONTH_WORD_RE = re.compile(rf"\b({_MONTHS})[a-z]*\.?", re.IGNORECASE)


def _canon_date(s: str) -> str:
    """One spelling per date range: "June 2022 — Mar 2024", "Jun 2022 to Mar
    2024" and "jun 2022 - mar 2024" are the same fact. The tailored resume
    writes ranges with a plain hyphen (no em dashes, owner 2026-10-08), so
    comparing the raw text reported every reformatted range as an invented
    employment date. A changed month or year still differs."""
    s = _MONTH_WORD_RE.sub(lambda m: m.group(1).lower()[:3], s or "")
    s = re.sub(r"\s*(?:[-–—]|\bto\b)\s*", " - ", s)
    return re.sub(r"\s+", " ", s).strip()


def _numbers(text: str) -> FrozenSet[str]:
    out = set()
    for m in _NUMBER_RE.finditer(text or ""):
        raw = m.group(1).replace(",", "")
        if _YEAR_RE.match(raw):
            continue          # dates are their own fact set
        if raw.endswith(".0"):
            raw = raw[:-2]
        out.add(raw)
    return frozenset(out)


def _certifications(text: str) -> FrozenSet[str]:
    out = set()
    for m in _CERT_PHRASE_RE.finditer(text or ""):
        phrase = _norm_fact(m.group(0))
        # "Certified" alone, or a bare "Certification" heading, is not a credential.
        if len(phrase.split()) >= 2:
            out.add(phrase)
    low = (text or "").lower()
    for acronym in _CERT_ACRONYMS:
        if re.search(rf"(?<!\w){re.escape(acronym)}(?!\w)", low):
            out.add(acronym)
    return frozenset(out)


@dataclass(frozen=True)
class FactSet:
    """What the résumé asserts, as sets. Deterministic; no model involved."""
    employers: FrozenSet[str]
    titles: FrozenSet[str]
    dates: FrozenSet[str]
    degrees: FrozenSet[str]
    institutions: FrozenSet[str]
    certifications: FrozenSet[str]
    numbers: FrozenSet[str]

    _LABELS = (
        ("employers", "employer"),
        ("titles", "job title"),
        ("dates", "employment date"),
        ("degrees", "degree"),
        ("institutions", "institution"),
        ("certifications", "certification"),
        ("numbers", "number"),
    )

    def added_against(self, other: "FactSet") -> List[Tuple[str, str]]:
        """Facts present in THIS set and absent from ``other`` (the master).

        Addition-direction only. A tailored résumé that DROPS a fact has made an
        editing choice; one that GAINS a fact has invented it.
        """
        out: List[Tuple[str, str]] = []
        for attr, label in self._LABELS:
            for value in sorted(getattr(self, attr) - getattr(other, attr)):
                out.append((label, value))
        return out


def extract_facts(md: str) -> FactSet:
    """Pull every checkable fact out of a résumé's markdown."""
    text = md or ""
    employers: set[str] = set()
    titles: set[str] = set()

    for line in text.splitlines():
        m = _EXPERIENCE_LINE_RE.match(line)
        if not m:
            continue
        titles.add(_norm_fact(m.group("title")))
        for seg in m.group("rest").split("|"):
            seg = _norm_fact(seg)
            if not seg or len(seg) > 60:
                continue
            if _DATE_RANGE_RE.search(seg) or _STANDALONE_MONTH_YEAR_RE.search(seg):
                continue
            # "Cincinnati, OH" / "Remote" / "Hybrid" are locations, not employers.
            if re.search(r",\s*[a-z]{2}$", seg) or seg in {"remote", "hybrid", "onsite", "on-site"}:
                continue
            employers.add(seg)

    dates = {_canon_date(_norm_fact(m.group(0))) for m in _DATE_RANGE_RE.finditer(text)}
    degrees = {_norm_fact(m.group(0)) for m in _DEGREE_RE.finditer(text)}
    degrees.discard("")
    institutions = {_norm_fact(m.group(0)) for m in _INSTITUTION_RE.finditer(text)
                    if len(m.group(0).split()) >= 2}

    return FactSet(
        employers=frozenset(employers),
        titles=frozenset(titles),
        dates=frozenset(dates),
        degrees=frozenset(degrees),
        institutions=frozenset(institutions),
        certifications=_certifications(text),
        numbers=_numbers(text),
    )


# ── spans ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EvidenceSpan:
    """One claim-bearing line, addressed by the hash of its own normalized text."""
    span_id: str
    text: str
    section: str

    @property
    def normalized(self) -> str:
        return normalize_text(self.text)


def _is_structural(line: str) -> bool:
    """Headers, date lines, employer/location lines and bare titles are structure.

    They carry facts (checked by the fact sets and pinned by tailoring/lock.py),
    not claims, so sending them to a fact-checker only manufactures failures —
    a date range cannot be "supported" by a bullet about building an API.
    """
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return True
    if s.endswith(":"):
        return True                       # "Languages:" — a list label
    core = s.lstrip(_BULLET_GLYPHS)
    if core.startswith("#") or _EXPERIENCE_LINE_RE.match(s):
        return True
    if _DATE_RANGE_RE.fullmatch(_norm_fact(core)) or _STANDALONE_MONTH_YEAR_RE.fullmatch(_norm_fact(core)):
        return True
    # Organisation / location lines: "Ohio State University, Columbus, OH",
    # "Acme Corp — Remote". Five words with no verb and no full stop, they are
    # not claims, and treating them as such is how a fact-checker gets asked
    # whether a university's address is "supported by" a bullet about APIs — a
    # question with no honest answer, which then fails the whole résumé.
    if not core.endswith((".", "!", "?")) and (
        _ORG_LOCATION_RE.search(core) or _INSTITUTION_RE.search(core)
    ):
        return True
    return len(core.split()) < 4


def extract_spans(md: str) -> Tuple[EvidenceSpan, ...]:
    """Claim-bearing lines, in document order, de-duplicated by content."""
    spans: List[EvidenceSpan] = []
    seen: set[str] = set()
    section = ""
    for line in (md or "").splitlines():
        header = _SECTION_RE.match(line)
        if header:
            section = header.group(1).strip()
            continue
        if _is_structural(line):
            continue
        text = line.strip().lstrip(_BULLET_GLYPHS).strip()
        norm = normalize_text(text)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        spans.append(EvidenceSpan(span_id=_hash(norm)[:16], text=text, section=section))
    return tuple(spans)


# ── the evidence object ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Evidence:
    """An immutable, content-addressed view of one master résumé."""
    evidence_id: str
    text: str
    spans: Tuple[EvidenceSpan, ...]
    facts: FactSet

    def span_by_normalized(self) -> Dict[str, EvidenceSpan]:
        return {s.normalized: s for s in self.spans}

    def contains(self, text: str) -> bool:
        """True when this exact claim already appears in the master."""
        return normalize_text(text) in {s.normalized for s in self.spans}


# Small content-keyed cache. Evidence is immutable and cheap to hold; rebuilding
# it per bullet was part of what made the old path quadratic in résumé length.
_EVIDENCE_CACHE: Dict[str, Evidence] = {}
_EVIDENCE_CACHE_MAX = 64


def build_evidence(master_md: str) -> Evidence:
    """Parse a master résumé into reusable evidence, memoized on its content."""
    text = master_md or ""
    evidence_id = _hash(normalize_text(text))
    cached = _EVIDENCE_CACHE.get(evidence_id)
    if cached is not None:
        return cached
    ev = Evidence(
        evidence_id=evidence_id,
        text=text,
        spans=extract_spans(text),
        facts=extract_facts(text),
    )
    if len(_EVIDENCE_CACHE) >= _EVIDENCE_CACHE_MAX:
        _EVIDENCE_CACHE.clear()
    _EVIDENCE_CACHE[evidence_id] = ev
    return ev


def patch_hash(generated_text: str, source_span_id: Optional[str] = None) -> str:
    """Content address for one generated claim + the evidence it was judged against.

    Both halves are in the key because the verdict is a statement about the
    PAIR: the same sentence can be supported by one source span and fabricated
    against another.
    """
    return _hash(f"{source_span_id or ''}\x00{normalize_text(generated_text)}")[:32]


# ── the deterministic fabrication guard ──────────────────────────────────────

def fabrication_violations(master_md: str, tailored_md: str) -> List[Tuple[str, str]]:
    """Facts the tailored résumé asserts that the master does not.

    This is the check that cannot be argued with: no model, no threshold, no
    similarity score. If an employer, held title, employment date, degree,
    institution, certification or number appears in the output and not in the
    input, it was invented, and no amount of "it reads plausible" makes it true.
    """
    master = build_evidence(master_md)
    added = extract_facts(tailored_md or "").added_against(master.facts)
    if added and not master.facts.employers and not master.facts.titles:
        # A master with no "**Title** | Employer | dates" lines — every PDF or
        # DOCX upload, read back as plain text — has no structured roles to
        # compare against, so a tailored resume that writes its roles in that
        # shape "added" every employer and title it kept. Against such a
        # master, an employer or title counts as present when the master's
        # text says it. A renamed company or an upgraded title still does not.
        hay = _presence_text(master.text)
        added = [(kind, value) for kind, value in added
                 if not (kind in ("employer", "job title") and _appears_in(value, hay))]
    return added


# ── the summary ──────────────────────────────────────────────────────────────
# The summary is where a generator SYNTHESISES: it restates a career in two or
# three sentences, so it is also where a job's duties get written up as the
# candidate's ("translating API documentation into tested connectors"). Grounding
# used to read only Experience/Projects bullets, so those sentences were never
# checked and the result still said "Grounded in your resume" (live test,
# 2026-10-09). These helpers find the summary's sentences so grounding can
# verify them, and remove the ones it cannot back. Deterministic, no model.

_KNOWN_SECTION_RE = re.compile(
    r"^(?:professional\s+|career\s+|technical\s+|executive\s+|work\s+|core\s+|relevant\s+)?"
    r"(summary|profile|objective|about(?:\s+me)?|overview|introduction|experience|employment|"
    r"education|skills|projects|certifications?|achievements|awards|publications|leadership|"
    r"activities|history|competencies|qualifications|interests|volunteering|volunteer)$",
    re.IGNORECASE)
_SUMMARY_TITLE_RE = re.compile(
    r"\b(summary|profile|objective|about(?:\s+me)?|overview|introduction)\b", re.IGNORECASE)
_NOT_SUMMARY_TITLE_RE = re.compile(
    r"experience|employment|project|skill|education|certif|work history", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[*_]*[A-Z0-9\"“(])")
_CONTACT_HINT_RE = re.compile(
    r"@|https?://|www\.|linkedin|github|\(\d{3}\)|\d{3}[\s.-]\d{3}[\s.-]\d{4}", re.IGNORECASE)


def _section_title(line: str) -> Optional[str]:
    """The section a heading line opens, or None when the line is not a heading.

    A markdown ``# Name`` is the candidate's name, not a section, unless it is a
    known section word. A plain line counts as a heading when it is a short
    known section name ("Professional Summary") or a short ALL-CAPS line, which
    is how a PDF/DOCX upload writes them.
    """
    s = (line or "").strip()
    if not s:
        return None
    m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", s)
    if m:
        title = _MD_NOISE_RE.sub("", m.group(2)).strip().rstrip(":").strip()
        if len(m.group(1)) == 1 and not _KNOWN_SECTION_RE.match(title):
            return None                       # "# Alex Tenant"
        return title
    core = _MD_NOISE_RE.sub("", s).strip().rstrip(":").strip()
    if not core or len(core.split()) > 4 or core.endswith((".", ",", ";")):
        return None
    if _KNOWN_SECTION_RE.match(core):
        return core
    if core.isupper() and re.search(r"[A-Z]{3}", core):
        return core
    return None


def _is_summary_title(title: str) -> bool:
    return bool(_SUMMARY_TITLE_RE.search(title or "")) and not _NOT_SUMMARY_TITLE_RE.search(title or "")


def _is_prose(line: str) -> bool:
    """A sentence of description, not a contact line, headline or list. A
    pipe-separated tagline is prose when it is not just short titles or skills
    ("Integrations Engineer | Translating API documentation into tested
    connectors" asserts work; "Backend Engineer | Python | AWS" does not)."""
    s = (line or "").strip()
    if len(s.split()) < 8 or _CONTACT_HINT_RE.search(s):
        return False
    if "|" in s and _is_title_or_skill_list(_pipe_segments(_MD_NOISE_RE.sub("", s))):
        return False
    return bool(re.search(r"\b[a-z]{3,}\b", s))


def _summary_line_indexes(md: str) -> Tuple[List[int], Optional[int]]:
    """Line numbers that hold the summary, and the line of its heading (if any).

    The summary is the section headed Summary / Profile / Objective / About /
    Overview, plus any prose paragraph sitting above the first section (an
    unheaded summary under the name).
    """
    lines = (md or "").splitlines()
    idx: List[int] = []
    header_at: Optional[int] = None
    seen_section = False
    in_summary = False
    for i, line in enumerate(lines):
        title = _section_title(line)
        if title is not None:
            seen_section = True
            in_summary = _is_summary_title(title)
            if in_summary and header_at is None:
                header_at = i
            continue
        if in_summary:
            if line.strip():
                idx.append(i)
        elif not seen_section and _is_prose(line):
            idx.append(i)
    return idx, header_at


def _split_summary_line(line: str) -> Tuple[str, List[str]]:
    """(bullet lead, sentences) for one summary line."""
    m = re.match(r"^(\s*(?:[-*•·]\s+)?)", line or "")
    lead = m.group(1) if m else ""
    body = (line or "")[len(lead):].strip()
    return lead, [p.strip() for p in _SENTENCE_SPLIT_RE.split(body) if p.strip()]


def _is_summary_claim(sentence: str) -> bool:
    plain = _MD_NOISE_RE.sub("", sentence or "").strip()
    if len(plain.split()) < 4 or plain.endswith(":"):
        return False
    # The opt-in relocation line (tailoring/relocation.py) is the candidate's
    # own profile setting, written in by request, not a claim about past work;
    # the master resume never says it, so checking it would delete it.
    if re.match(r"^open to relocation to\b", plain, re.IGNORECASE):
        return False
    # A comma-run list of skills ("Core skills: Python, AWS, Docker") is checked
    # by the skill review (requirements.review), not read as a sentence.
    return not re.match(r"^[^.:]{1,40}:\s*\S+(?:\s*,\s*\S+){2,}\s*$", plain)


def _pipe_segments(text: str) -> List[str]:
    return [s.strip() for s in (text or "").split("|") if s.strip()]


# A segment that opens on a verb or holds a clause asserts something done:
# "Translating API documentation into tested connectors", "Led a 6-person team".
_LEAD_VERB_RE = re.compile(r"^[A-Za-z]+(?:ing|ed)$|^(?:led|built|ran|grew|won|made|wrote|"
                           r"drove|shipped|cut|saved|owned|own|owns|lead|leads|build|"
                           r"builds|ship|ships|deliver|delivers|turn|turns)$", re.IGNORECASE)
# Fields that end in -ing are nouns at the head of a title ("Engineering
# Manager", "Machine Learning", "Nursing Leadership").
_ING_NOUNS = frozenset({
    "engineering", "marketing", "accounting", "nursing", "learning", "computing",
    "testing", "manufacturing", "consulting", "banking", "publishing", "programming",
    "networking", "recruiting", "training", "planning", "processing", "sourcing",
    "modeling", "modelling", "billing", "coding", "lending", "underwriting",
    "auditing", "merchandising", "purchasing", "advertising", "teaching", "tutoring",
    "counseling", "counselling", "staffing", "trading", "pricing", "licensing",
})
_CLAUSE_WORD_RE = re.compile(r"\b(?:who|that|which)\b", re.IGNORECASE)


def _is_title_or_skill_list(segments: List[str]) -> bool:
    """True when every pipe segment is a short noun phrase (a title, a skill,
    a domain: four words or fewer, no verb, no clause). Only then is the line a
    headline the skill review covers rather than a claim to verify."""
    for seg in segments:
        words = re.findall(r"[A-Za-z0-9][\w'+#./-]*", seg)
        if len(words) > 4:
            return False
        if not words:
            continue
        first = words[0].lower()
        if (_LEAD_VERB_RE.match(first) and first not in _ING_NOUNS) or _CLAUSE_WORD_RE.search(seg):
            return False
    return True


def _claim_parts(sentence: str) -> List[str]:
    """The claim-bearing parts of one summary sentence, markdown removed.

    A pipe-separated line made only of titles and skills claims nothing the
    skill review does not already check. Any other pipe line is a run of
    claims: each segment of four or more words is verified on its own, so
    "Integrations Engineer | Translating API documentation into tested
    connectors | Shipping systems customers configure without support
    escalation" no longer passes unread as a "skills list"."""
    plain = _MD_NOISE_RE.sub("", sentence or "").strip()
    if "|" not in plain:
        return [plain] if _is_summary_claim(plain) else []
    segments = _pipe_segments(plain)
    if _is_title_or_skill_list(segments):
        return []
    return [s for s in segments if _is_summary_claim(s)]


def summary_sentences(md: str) -> List[str]:
    """The claim-bearing sentences of a resume's summary, markdown removed. A
    pipe-separated tagline contributes each claim-bearing segment."""
    lines = (md or "").splitlines()
    idx, _ = _summary_line_indexes(md)
    out: List[str] = []
    for i in idx:
        _, sentences = _split_summary_line(lines[i])
        for sentence in sentences:
            out.extend(_claim_parts(sentence))
    return out


def remove_summary_sentences(md: str, sentences, master_md: str = "") -> Tuple[str, List[str]]:
    """Take the given sentences out of the summary; never adds a word of its own.

    A summary sentence the master resume cannot back comes out, and the rest of
    the summary stays. When nothing is left, the master's own summary is put
    back (the candidate's words, already true) or, with none, the empty heading
    goes. Returns (new_markdown, sentences_actually_removed).
    """
    targets = {normalize_text(s) for s in (sentences or []) if (s or "").strip()}
    if not targets or not (md or "").strip():
        return md, []
    lines = (md or "").splitlines()
    idx, header_at = _summary_line_indexes(md)
    removed: List[str] = []
    replaced: Dict[int, Optional[str]] = {}
    for i in idx:
        lead, parts = _split_summary_line(lines[i])
        kept = []
        changed = False
        for part in parts:
            if normalize_text(part) in targets:
                removed.append(_MD_NOISE_RE.sub("", part).strip())
                changed = True
                continue
            if "|" in part:
                # A pipe tagline: drop only the unbacked segments, keep the rest
                # ("Integrations Engineer | Python" survives its invented clause).
                segs = [s.strip() for s in part.split("|")]
                left = [s for s in segs if s and normalize_text(s) not in targets]
                gone = [s for s in segs if s and normalize_text(s) in targets]
                if gone:
                    removed.extend(_MD_NOISE_RE.sub("", s).strip() for s in gone)
                    changed = True
                    if left:
                        kept.append(" | ".join(left))
                    continue
            kept.append(part)
        if changed:
            replaced[i] = (lead + " ".join(kept)) if kept else None
    if not removed:
        return md, []
    out: List[str] = []
    for i, line in enumerate(lines):
        if i in replaced:
            if replaced[i] is not None:
                out.append(replaced[i])
            continue
        out.append(line)
    new_md = "\n".join(out)
    if header_at is not None:
        left_idx, left_header = _summary_line_indexes(new_md)
        new_lines = new_md.splitlines()
        section_left = [j for j in left_idx if left_header is not None and j > left_header]
        if left_header is not None and not section_left:
            m_lines = (master_md or "").splitlines()
            m_idx, m_header = _summary_line_indexes(master_md or "")
            fallback = [m_lines[j] for j in m_idx if m_header is None or j > m_header]
            if fallback:
                new_lines[left_header + 1:left_header + 1] = fallback
            else:
                del new_lines[left_header]
            new_md = "\n".join(new_lines)
    return new_md, removed


def _presence_text(text: str) -> str:
    t = re.sub(r"[*_`#>]", " ", (text or "").lower())
    t = re.sub(r"[|•·]", " ", t)
    return re.sub(r"\s+", " ", t)


def _appears_in(value: str, hay: str) -> bool:
    v = re.sub(r"\s+", " ", (value or "").lower()).strip()
    return bool(v) and re.search(rf"(?<!\w){re.escape(v)}(?!\w)", hay) is not None
