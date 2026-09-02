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
_CERT_PHRASE_RE = re.compile(
    r"\b(?:[A-Z][\w+.#-]*\s+){0,4}"
    r"(?:Certified|Certification|Certificate)"
    r"(?:\s+[A-Z][\w+.#-]*){0,4}\b",
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

    dates = {_norm_fact(m.group(0)) for m in _DATE_RANGE_RE.finditer(text)}
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
    return extract_facts(tailored_md or "").added_against(master.facts)
