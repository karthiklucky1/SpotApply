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
from bisect import bisect_right
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

# A range is month+year or a bare year at either end: "Jun 2022 - Mar 2024",
# "2019 - 2022", "2021 - Present". Year-only ranges were not dates to this
# module, so "**Engineer** | Acme | 2019 - 2022" read "2019 - 2022" as an
# EMPLOYER — and a draft that re-spaced it to "2019-2022" was blocked for an
# invented employer — while an invented year-only range was never a date fact.
_YEAR_TOKEN = r"(?:19|20)\d{2}"
_DATE_RANGE_RE = re.compile(
    rf"\b(?:(?:{_MONTHS})[a-z]*\.?\s*{_YEAR_TOKEN}|{_YEAR_TOKEN})\s*(?:[-–—]|\bto\b)\s*"
    rf"(?:(?:{_MONTHS})[a-z]*\.?\s*{_YEAR_TOKEN}|{_YEAR_TOKEN}|present|current|now|ongoing)\b",
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


# ── dates, read the way people write them ────────────────────────────────────
# A plain-text master (every PDF/DOCX upload) writes dates any way its author
# liked: "Apr-2006", "04/2006", "Nov'07", "2007 – Till Date". The tailored draft
# writes them "Apr 2006 - Dec 2007". Comparing spellings blocked truthful drafts
# for "inventing" the user's own dates (production 2026-10-26: three ranges of
# one user's real history, on two tailors). A range is compared as VALUES —
# (year, month) at each end, any open end is "present" — and a draft may write
# a date LESS precisely than the master (drop a month), never more: "2007" for
# "Nov 2007" adds nothing, "Jan 2020" for "2020" invents a month.
_MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
              "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_ABBR = {v: k for k, v in _MONTH_NUM.items()}
_MONTH_WORDS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
                r"aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_ENDPOINT = (
    rf"(?:(?P<{{p}}mw>{_MONTH_WORDS})\.?(?:[\s,\-/]*(?P<{{p}}my>(?:19|20)\d{{{{2}}}})"
    rf"|\s*['’]\s*(?P<{{p}}my2>\d{{{{2}}}}))"
    rf"|(?<![\d.,])(?P<{{p}}mn>0?[1-9]|1[0-2])\s*[/.\-]\s*(?P<{{p}}ny>(?:19|20)\d{{{{2}}}})"
    rf"|(?<![\d.,])(?P<{{p}}iy>(?:19|20)\d{{{{2}}}})\s*[/\-]\s*(?P<{{p}}im>0[1-9]|1[0-2])(?!\d)"
    rf"|(?<![\d.,$])(?P<{{p}}y>(?:19|20)\d{{{{2}}}}))(?![\d%])"
)
_OPEN_END = r"(?P<open>(?:(?:till|until)\s+)?(?:present|current(?:ly)?|now|ongoing|today|date))\b"
_RANGE_SEP = r"\s*(?:[-–—‒−~]|\bto\b|\btill\b|\buntil\b|\bthrough\b|\bthru\b)\s*"
# The master side also accepts two dates side by side with only a cell break
# between them ("Apr 2006 | Dec 2007", a table row read back as text): being
# generous about what the MASTER says can only make the guard block less.
_LOOSE_SEP = r"(?:[\s|,:\-–—‒−~]|\b(?:to|till|until|through|thru|from)\b){1,8}"


def _range_re(sep: str) -> "re.Pattern[str]":
    start = _ENDPOINT.format(p="a")
    end = _ENDPOINT.format(p="b")
    return re.compile(rf"(?i)\b(?:{start}){sep}(?:{end}|{_OPEN_END})")


_STRICT_RANGE_RE = _range_re(_RANGE_SEP)
_LOOSE_RANGE_RE = _range_re(_LOOSE_SEP)

PRESENT = (9999, 0)


def _endpoint(m: "re.Match[str]", p: str) -> Optional[Tuple[int, Optional[int]]]:
    g = m.groupdict()
    if g.get(f"{p}mw"):
        month = _MONTH_NUM[g[f"{p}mw"].lower()[:3]]
        if g.get(f"{p}my"):
            return int(g[f"{p}my"]), month
        yy = int(g[f"{p}my2"])
        return (2000 + yy if yy <= 40 else 1900 + yy), month
    if g.get(f"{p}ny"):
        return int(g[f"{p}ny"]), int(g[f"{p}mn"])
    if g.get(f"{p}iy"):
        return int(g[f"{p}iy"]), int(g[f"{p}im"])
    if g.get(f"{p}y"):
        return int(g[f"{p}y"]), None
    return None


def _canon_endpoint(ep: Tuple[int, Optional[int]]) -> str:
    if ep == PRESENT:
        return "present"
    year, month = ep
    return f"{_MONTH_ABBR[month]} {year}" if month else str(year)


def date_ranges(text: str, *, loose: bool = False) -> List[Tuple[tuple, tuple]]:
    """Every employment-style date range in ``text`` as ((y, m), (y, m)) pairs,
    month None when not written, ``PRESENT`` for an open end."""
    out: List[Tuple[tuple, tuple]] = []
    for m in (_LOOSE_RANGE_RE if loose else _STRICT_RANGE_RE).finditer(text or ""):
        start = _endpoint(m, "a")
        end = PRESENT if m.group("open") else _endpoint(m, "b")
        if start and end:
            out.append((start, end))
    return out


def canon_range(rng: Tuple[tuple, tuple]) -> str:
    return f"{_canon_endpoint(rng[0])} - {_canon_endpoint(rng[1])}"


def _covers(source: tuple, claim: tuple) -> bool:
    """``claim`` says no more than ``source``: the same moment, or the same
    year with the month left out."""
    if claim == PRESENT or source == PRESENT:
        return claim == source
    return source[0] == claim[0] and (claim[1] is None or claim[1] == source[1])


def range_supported(claim: Tuple[tuple, tuple], sources) -> bool:
    return any(_covers(s[0], claim[0]) and _covers(s[1], claim[1]) for s in sources)


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
            if (_DATE_RANGE_RE.search(seg) or _STANDALONE_MONTH_YEAR_RE.search(seg)
                    or date_ranges(seg)):
                continue
            if _YEAR_RE.match(seg):
                continue          # "**B.S.** | 2019" — a graduation year, not an employer
            # "Cincinnati, OH" / "Remote" / "Hybrid" are locations, not employers.
            if re.search(r",\s*[a-z]{2}$", seg) or seg in {"remote", "hybrid", "onsite", "on-site"}:
                continue
            employers.add(seg)

    dates = {canon_range(r) for r in date_ranges(text)}
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
    if not added:
        return []
    # The sets above are a fast first pass. They compare SPELLINGS, and the
    # master is usually plain text (every PDF/DOCX upload) laid out however its
    # author liked, while the draft is markdown in one house shape — so a
    # set difference reads reformatting as invention: "Master of Science in
    # Information Systems" against a PDF line that ran on into "… Systems Aug
    # 2023", a degree line the draft wrote as a role header, a client named in
    # brackets. Each candidate is therefore re-asked against the master's TEXT,
    # as words and values: every word of an employer, title, degree, school or
    # credential must stand in the master in that order, close together; every
    # date range must be one the master states (or the same range, less
    # precise). A word the master never puts there ("Principal", "Stripe", a
    # second credential) still fails, and so does a moved year.
    stream = _support_stream(master.text)
    sources = _master_ranges(master.text)
    kept = []
    for kind, value in added:
        if kind == "employment date":
            claim = date_ranges(value)
            if claim and range_supported(claim[0], sources):
                continue
        elif kind in ("employer", "job title", "degree", "institution", "certification"):
            if words_supported(value, stream):
                continue
        kept.append((kind, value))
    return kept


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


_EMPHASIS_RE = re.compile(r"(?<!\*)\*\*(?!\*)|(?<!\*)\*(?!\*)")


def _rejoin_kept(pieces: List[Tuple[int, str, bool]]) -> str:
    """The summary line rebuilt from the pieces that stay: segments of one
    sentence pipe-joined, sentences space-joined.

    Emphasis that ran into a removed piece is closed (or reopened) inside the
    text that stays: "**Integrations Engineer | Translating API documentation
    into tested connectors**" loses its second segment and keeps
    "**Integrations Engineer**", never a lone "**" the renderer prints as
    text. Markers that were never paired are left as they were."""
    flat, spans = "", []
    for k, (sent, text, _) in enumerate(pieces):
        if k:
            flat += "|" if pieces[k - 1][0] == sent else " "
        spans.append((len(flat), len(flat) + len(text)))
        flat += text

    def piece_at(pos: int) -> int:
        return next(k for k, (a, b) in enumerate(spans) if a <= pos < b)

    pre, post = [""] * len(pieces), [""] * len(pieces)
    for mark in ("**", "*"):
        opened: List[int] = []
        for m in _EMPHASIS_RE.finditer(flat):
            if m.group(0) != mark:
                continue
            s, e = m.span()
            if s and not flat[s - 1].isspace() and opened:
                a, b = piece_at(opened.pop()), piece_at(s)
                if pieces[a][2] or pieces[b][2]:
                    kept = [k for k in range(a, b + 1) if not pieces[k][2] and pieces[k][1].strip()]
                    if kept and pieces[a][2]:
                        pre[kept[0]] = mark + pre[kept[0]]
                    if kept and pieces[b][2]:
                        post[kept[-1]] += mark
            elif e < len(flat) and not flat[e].isspace():
                opened.append(s)
    out, last = "", None
    for k, (sent, text, gone) in enumerate(pieces):
        if gone or not text.strip():
            continue
        if out:
            out += " | " if sent == last else " "
        out += pre[k] + text.strip() + post[k]
        last = sent
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
        # (sentence number, text, removed): a sentence, or one segment of a
        # pipe tagline, where only the unbacked segments go ("Integrations
        # Engineer | Python" survives its invented clause).
        pieces: List[Tuple[int, str, bool]] = []
        for n, part in enumerate(parts):
            if normalize_text(part) in targets:
                removed.append(_MD_NOISE_RE.sub("", part).strip())
                pieces.append((n, part, True))
                continue
            segs = part.split("|")
            flags = [len(segs) > 1 and bool(s.strip()) and normalize_text(s) in targets
                     for s in segs]
            if not any(flags):
                pieces.append((n, part, False))
                continue
            removed.extend(_MD_NOISE_RE.sub("", s).strip() for s, f in zip(segs, flags) if f)
            pieces.extend((n, s, f) for s, f in zip(segs, flags))
        if any(gone for _, _, gone in pieces):
            text = _rejoin_kept(pieces)
            replaced[i] = (lead + text) if text else None
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


# ── word-level support: "does the other document say this?" ──────────────────
# One comparison for both directions: the fabrication guard asks it of the
# MASTER (is every word of this employer/title/degree there?), the Doctor asks
# it of the DRAFT (did the master's employer survive?). Words, not spellings:
# case, punctuation, bullets, line breaks, "B.S." vs "Bachelor of Science" and
# a legal suffix are formatting; a word that is not there is not.
_FILLER_WORDS = frozenset({
    "of", "in", "the", "and", "at", "for", "a", "an", "with", "on", "to", "from",
    # legal suffixes: "Acme Corp" and "Acme Inc." are one employer
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "pvt",
    "private", "plc", "gmbh", "llp", "lp",
})
_DEGREE_SPELLINGS = (
    (r"\bph\.?\s?d\b\.?", " doctor philosophy "),
    (r"\bm\.?\s?b\.?\s?a\b\.?", " master business administration "),
    (r"\bb\.?\s?tech\b\.?", " bachelor technology "),
    (r"\bm\.?\s?tech\b\.?", " master technology "),
    (r"\bb\.?\s?sc?\b\.?", " bachelor science "),
    (r"\bm\.\s?sc?\b\.?|\bmsc\b", " master science "),   # bare "ms" is milliseconds
    (r"\bb\.\s?a\.|\bb\.\s?e\.", " bachelor "),
    (r"\bm\.\s?a\.|\bm\.\s?e\.", " master "),
    (r"\b(bachelor|master|doctor|associate)(?:['’]?s)?\b", r" \1 "),
)
_WORD_RE = re.compile(r"[a-z0-9+#]+")
_MONTH_ONLY_RE = re.compile(_MONTH_WORDS)
_SEGMENT_SPLIT_RE = re.compile(r"[,()\[\]/;:|]+|\s[-–—]\s")
_MAX_GAP = 2          # words the master may carry between two of the claim's
_SEGMENT_WINDOW = 40  # how far apart a claim's comma-separated parts may sit


def _support_words(text: str) -> List[str]:
    t = (text or "").lower()
    for pattern, repl in _DEGREE_SPELLINGS:
        t = re.sub(pattern, repl, t)
    words = []
    for w in _WORD_RE.findall(t):
        w = w.strip("+#") or w
        if w in _FILLER_WORDS or w.isdigit() or _MONTH_ONLY_RE.fullmatch(w):
            continue          # dates and numbers are compared as values elsewhere
        words.append(w)
    return words


_LIST_GLYPH_RE = re.compile(r"^\s*(?:[-*+]\s|[•·–—‣▪◦●»✓➢►]\s?|\d+[.)]\s)")
_CREDENTIAL_LEAD_RE = re.compile(r"^(?:certified|licensed|registered|accredited)$", re.I)
_BARRIER = ("\x00",) * (_MAX_GAP + 1)


def _is_header_line(line: str) -> bool:
    """A line that NAMES things (role, employer, school, credential) rather
    than describing work. An employer is supported only where the master names
    one: "Integrated the Google Maps API" mentions Google, it does not make it
    an employer, and a draft that promotes it to one has invented a job."""
    s = line.strip()
    if not s or _LIST_GLYPH_RE.match(s):
        return False
    if date_ranges(s):
        return True
    core = re.sub(r"[#*_`>]", " ", s).strip()
    words = core.split()
    if not words or len(words) > 14 or core.endswith("."):
        return False
    lead = re.sub(r"[^A-Za-z]", "", words[0])
    if (_LEAD_VERB_RE.match(lead) and lead.lower() not in _ING_NOUNS
            and not _CREDENTIAL_LEAD_RE.match(lead)):
        return False          # "Integrated Google Maps for routing" — a bullet without a glyph
    return True


# Sections that LIST or DESCRIBE rather than name: "Languages: Python, SQL" is
# short enough to look like a header line, and a draft that turns "Python" or
# "OpenAI" into an employer must not find it there.
_LIST_SECTION_RE = re.compile(
    r"\b(?:skills?|technolog|tools?\b|competenc|stack|expertise|languages|summary|profile|"
    r"objective|about|interests|hobbies|coursework|highlights)", re.I)
_NAMING_SECTION_RE = re.compile(r"\b(?:experience|employment|work|education|projects?|certif)", re.I)


def _header_words(text: str) -> List[str]:
    from app.tailoring.inventory import _bare_section
    words: List[str] = []
    listing = False

    def barrier() -> None:
        if words and words[-1] != _BARRIER[-1]:
            words.extend(_BARRIER)      # a run never bridges a bullet or a section

    for line in (text or "").splitlines():
        heading = _SECTION_RE.match(line)
        name = heading.group(1) if heading else _bare_section(line)
        if name:
            listing = bool(_LIST_SECTION_RE.search(name)) and not _NAMING_SECTION_RE.search(name)
            barrier()
            continue
        if not listing and _is_header_line(line):
            words.extend(_support_words(line))
        else:
            barrier()
    return words


_STREAM_CACHE: Dict[str, Tuple[Tuple[str, ...], Dict[str, List[int]]]] = {}


def _support_stream(text: str) -> Tuple[Tuple[str, ...], Dict[str, List[int]]]:
    key = _hash(text or "")
    hit = _STREAM_CACHE.get(key)
    if hit is None:
        words = tuple(_header_words(text))
        index: Dict[str, List[int]] = {}
        for i, w in enumerate(words):
            index.setdefault(w, []).append(i)
        if len(_STREAM_CACHE) >= _EVIDENCE_CACHE_MAX:
            _STREAM_CACHE.clear()
        hit = _STREAM_CACHE[key] = (words, index)
    return hit


_RANGE_CACHE: Dict[str, list] = {}


def _master_ranges(text: str) -> list:
    key = _hash(text or "")
    if key not in _RANGE_CACHE:
        if len(_RANGE_CACHE) >= _EVIDENCE_CACHE_MAX:
            _RANGE_CACHE.clear()
        _RANGE_CACHE[key] = date_ranges(text, loose=True)
    return _RANGE_CACHE[key]


def _segment_spans(seg: List[str], index: Dict[str, List[int]]) -> List[Tuple[int, int]]:
    """Where ``seg``'s words occur in order with at most _MAX_GAP words between
    neighbours — (first, last) positions of each such run. Position lists are
    ascending, so each step is a bisect, never a scan."""
    spans = []
    for start in index.get(seg[0], ()):
        pos, ok = start, True
        for w in seg[1:]:
            positions = index.get(w, ())
            i = bisect_right(positions, pos)
            if i == len(positions) or positions[i] > pos + _MAX_GAP + 1:
                ok = False
                break
            pos = positions[i]
        if ok:
            spans.append((start, pos))
    return spans


def words_supported(value: str, stream) -> bool:
    """True when every word of ``value`` stands in the stream's document: each
    comma/bracket-separated part as an ordered run, the parts near each other."""
    _words, index = stream
    segments = [s for s in (_support_words(p) for p in _SEGMENT_SPLIT_RE.split(value or "")) if s]
    if not segments:
        return True           # nothing but dates, numbers and filler
    found = [_segment_spans(s, index) for s in segments]
    if any(not f for f in found):
        return False
    for first, _last in found[0]:
        if all(any(abs(a - first) <= _SEGMENT_WINDOW for a, _b in f) for f in found[1:]):
            return True
    return False


def text_supports(value: str, text: str) -> bool:
    """``words_supported`` against a document given as text."""
    return words_supported(value, _support_stream(text))
