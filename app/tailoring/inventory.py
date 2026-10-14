"""The evidence inventory — what this person has actually done, and for how long.

`evidence.py` answers "does this sentence appear in the master résumé?" That is
enough to catch a fabricated sentence and not nearly enough to answer the two
questions a recruiter screening call turns on:

    Is this skill backed by PAID WORK, or by a weekend project?
    How much time is behind it, once you stop counting the same months twice?

Neither was computable before this module. `resume_basic_extract._years_experience`
took `min(start) → max(end)` — the SPAN of a career, not the time worked — so a
2016 summer internship plus a job started in 2024 reported **10 years of
experience** (guard: `test_evidence_inventory`). That number is not cosmetic: it
reaches `reranker`'s seniority rules ("candidate has ~{yoe} years"),
`RuleFilter.cand_years` and the UserCard, so an inflated total spends the day's
finals budget on Staff and Principal postings the user will not be screened for.
Both errors — counting a gap as employment, and counting two concurrent roles
twice — are the same mistake: tenure is the measure of a UNION of intervals, not
a span and not a sum.

FIVE KINDS OF WORK, KEPT APART. A résumé's "experience" is not one substance:

    professional   paid employment                     counts as experience
    freelance      paid client/contract work           counts as experience
    internship     real, paid, and routinely discounted  reported SEPARATELY
    academic       coursework, research/teaching assistant   never employment
    personal       side projects, hackathons, self-study     never employment

A completed project is genuine evidence that someone can do the thing. It is not
years of professional employment, and the difference is exactly what a screening
call exposes. So a skill whose only evidence is a personal project is reported as
`project_only`, never folded into a months total, and never written up as though
it were a job.

WORDING IS HONOURED, NOT ROUNDED. "three (3) years", "4-6 years", "2+", "at
least 18 months" and "one year" are different requirements and are read as
written. Nothing here scores, ranks or estimates a chance of being interviewed:
every output is a statement of fact about the résumé and the posting, or an
explicit admission that we do not know.

Nothing in this module calls an LLM or the network.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from app.tailoring.evidence import normalize_text

# ── kinds of work ────────────────────────────────────────────────────────────

PROFESSIONAL = "professional"
FREELANCE = "freelance"
INTERNSHIP = "internship"
ACADEMIC = "academic"
PERSONAL = "personal"
VOLUNTEER = "volunteer"

#: The kinds that answer "years of professional experience". Freelance is paid
#: client work and counts; an internship is real work that employers discount,
#: so it is reported on its own line rather than silently folded in.
EMPLOYMENT_KINDS: FrozenSet[str] = frozenset({PROFESSIONAL, FREELANCE})

#: Kinds that demonstrate a skill but are never employment.
PROJECT_KINDS: FrozenSet[str] = frozenset({ACADEMIC, PERSONAL})

_KIND_LABELS = {
    PROFESSIONAL: "professional experience",
    FREELANCE: "freelance/contract work",
    INTERNSHIP: "internship",
    ACADEMIC: "academic work",
    PERSONAL: "personal project",
    VOLUNTEER: "volunteer work",
}


def kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind or "work")


# ── section headers → a default kind ─────────────────────────────────────────

_SECTION_KINDS: Tuple[Tuple[str, str], ...] = (
    # Most specific first: "professional experience" must not be read as
    # "experience" by a looser rule, and "internship experience" is an
    # internship section even though it contains the word "experience".
    ("internship", INTERNSHIP),
    ("co-op", INTERNSHIP),
    ("freelance", FREELANCE),
    ("consulting", FREELANCE),
    ("contract", FREELANCE),
    ("volunteer", VOLUNTEER),
    ("community", VOLUNTEER),
    ("education", ACADEMIC),
    ("academic", ACADEMIC),
    ("coursework", ACADEMIC),
    ("research", ACADEMIC),
    ("publication", ACADEMIC),
    ("project", PERSONAL),
    ("portfolio", PERSONAL),
    ("open source", PERSONAL),
    ("open-source", PERSONAL),
    ("side", PERSONAL),
    ("experience", PROFESSIONAL),
    ("employment", PROFESSIONAL),
    ("work history", PROFESSIONAL),
    ("career", PROFESSIONAL),
    ("positions", PROFESSIONAL),
)

# A title or employer can override its section. "Software Engineering Intern"
# under a plain "Experience" heading is an internship, and reading it as three
# years of professional employment is the inflation this module exists to stop.
_TITLE_KINDS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(?:intern|internship|co-?op|trainee|apprentice)\b", re.I), INTERNSHIP),
    (re.compile(r"\b(?:freelance|freelancer|contract(?:or)?|self[- ]employed|consultant|"
                r"independent)\b", re.I), FREELANCE),
    (re.compile(r"\b(?:research|teaching|graduate|lab)\s+assistant\b|\b(?:ta|ra)\b|"
                r"\bthesis\b|\bcapstone\b|\bcoursework\b", re.I), ACADEMIC),
    (re.compile(r"\bvolunteer\b", re.I), VOLUNTEER),
)

_SECTION_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")

# "**Senior Backend Engineer** | Acme | Jun 2022 - Mar 2024 | Remote" — the
# master-résumé shape `evidence.py` already parses. Kept in step with it by
# `test_evidence_inventory`, which asserts both modules read the same lines.
_PIPE_HEADER_RE = re.compile(r"^\s*(?:[-*]\s*)?\*\*(?P<title>[^*|]+)\*\*\s*\|(?P<rest>.+)$")

_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_MONTH_NUMS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_PRESENT = ("present", "current", "now", "ongoing", "to date", "today")

_RANGE_RE = re.compile(
    rf"(?P<start>(?:{_MONTHS})[a-z]*\.?\s*\d{{4}}|\d{{4}})"
    rf"\s*(?:[-–—]|\bto\b|\buntil\b)\s*"
    rf"(?P<end>(?:{_MONTHS})[a-z]*\.?\s*\d{{4}}|\d{{4}}|"
    rf"present|current|now|ongoing|to date|today)",
    re.I,
)
_LOCATION_TAIL_RE = re.compile(
    r"^(?:remote|hybrid|on-?site|[a-z .'-]+,\s*[a-z]{2}|[a-z .'-]+,\s*[a-z ]+)$", re.I)

# ── headers in every shape a résumé actually arrives in ─────────────────────
# A PDF or DOCX upload is read back as PLAIN TEXT (pipeline._fetch_resume_from_
# storage): no `#`, no `**`. Measured 2026-10-10 on the three founder résumés
# rendered to PDF/DOCX and read back the way Storage returns them: every one
# parsed to ZERO roles and "none on the resume" for tenure, while the same text
# as markdown read 3 years 2 months. The rules below recognise the shapes that
# text has — a bare "EXPERIENCE" line, "Title | Company | dates" without bold,
# a title on one line and the dates on the next — without loosening what counts
# as a claim.
_BULLET_GLYPHS = "-*•·–—‣▪◦●» \t"
_SKILLS_SECTION_RE = re.compile(
    r"\b(?:skill|technolog|tool|competenc|stack|expertise|languages)", re.I)
#: A line that IS a section name (optionally decorated: "EXPERIENCE:", "— Skills —").
#: Exact names only: "Software Engineering Intern" and "Project Manager" contain
#: section words and must never be read as sections, or every role under them
#: inherits the wrong kind.
_CANONICAL_SECTION_RE = re.compile(
    r"^(?:"
    r"(?:professional|work|relevant|industry|employment|research|teaching|leadership|"
    r"volunteer|internship|clinical|military|other|additional|related)\s+experience"
    r"|experience|employment(?:\s+history)?|work\s+history|career\s+(?:history|summary)"
    r"|internships?|positions?\s+held"
    r"|(?:technical|personal|academic|selected|key|notable|side|open[- ]source|software)"
    r"\s+projects?|projects?"
    r"|education(?:\s*(?:&|and)\s*(?:training|certifications?))?|academic\s+background"
    r"|(?:relevant\s+)?coursework|research|publications?|thesis"
    r"|(?:technical|core|key|relevant|professional)\s+skills"
    r"|skills(?:\s*(?:&|and)\s*(?:tools|technologies|interests|expertise))?"
    r"|technologies|tools|core\s+competencies|competencies|languages|technical\s+summary"
    r"|areas\s+of\s+expertise|summary\s+of\s+qualifications"
    r"|(?:professional\s+|career\s+|executive\s+)?(?:summary|profile|objective)|about(?:\s+me)?"
    r"|highlights|(?:licenses?\s*(?:&|and)\s*)?certifications?(?:\s*(?:&|and)\s*licenses?)?"
    r"|certificates|licenses?|training"
    r"|awards(?:\s*(?:&|and)\s*(?:honors|recognition))?|honors(?:\s*(?:&|and)\s*awards)?"
    r"|achievements|accomplishments"
    r"|volunteer(?:ing|\s+work|\s+activities)?|community\s+(?:service|involvement)"
    r"|leadership(?:\s*(?:&|and)\s*activities)?|activities|extracurricular(?:\s+activities)?"
    r"|interests|hobbies|references|additional\s+information|affiliations|memberships"
    r"|professional\s+(?:affiliations|memberships|development)"
    r")$", re.I)
_HEADER_SMALL_WORDS = frozenset({
    "at", "of", "and", "the", "for", "in", "a", "an", "&", "to", "via", "with",
    "de", "la", "du", "von", "van", "inc", "llc", "ltd", "co", "corp", "plc", "gmbh"})
_SENTENCE_END = (".", "!", "?", ";")
_CITY_STATE_RE = re.compile(r"^[a-z .'-]+,\s*[a-z]{2}$", re.I)
_CONTACT_LINE_RE = re.compile(r"@|https?://|www\.|linkedin\.|github\.|\d{3}[-.) ]\d{3}[-. ]\d{4}")
_DEGREE_LINE_RE = re.compile(
    r"\b(?:b\.s\.|m\.s\.|b\.a\.|m\.a\.|b\.e\.|m\.e\.|ph\.?d|bsc|msc|b\.?tech|m\.?tech|mba"
    r"|bachelor(?:'s)?|master(?:'s)?\s+(?:of|in|degree)|doctor(?:ate)?\s+of"
    r"|associate(?:'s)?\s+(?:of|degree)|(?:bs|ms|ba|ma|be|me)\s+in\s+|gpa\b"
    r"|high\s+school\s+diploma)", re.I)


def _bare_section(line: str) -> Optional[str]:
    """'EXPERIENCE', 'Work Experience:', '— Skills —' → the section name, else None."""
    s = (line or "").strip()
    if not s or _RANGE_RE.search(s) or s.startswith("#"):
        return None
    core = s.strip(" :|—–-_[]()*#•·\t")
    if re.fullmatch(r"(?:[A-Za-z]\s)+[A-Za-z]", core):
        core = core.replace(" ", "")          # "E X P E R I E N C E" (small caps)
    if not core or len(core.split()) > 5:
        return None
    return core if _CANONICAL_SECTION_RE.match(core) else None


def _lowercase_share(words: Sequence[str]) -> float:
    cands = [w for w in words if w[:1].isalpha()]
    if not cands:
        return 0.0
    low = sum(1 for w in cands
              if w[0].islower() and w.lower().strip(".,") not in _HEADER_SMALL_WORDS)
    return low / len(cands)


def _header_like(raw: str) -> bool:
    """Does a line carrying a date range read as a ROLE HEADER rather than a
    bullet that happens to mention dates? Headers are names: title case or
    caps, pipe- or comma-separated, no sentence punctuation. A bullet reads as
    a sentence ("Led the migration from Jan 2020 - Mar 2021 ...")."""
    core = (raw or "").strip().lstrip(_BULLET_GLYPHS).strip()
    rest = _RANGE_RE.sub(" ", core).strip(" ,|()–—-:\t")
    if not rest:
        return True
    if "|" in rest:
        return True
    if rest.endswith(_SENTENCE_END):
        return False
    words = rest.split()
    if len(words) > 12:
        return False
    return _lowercase_share(words) <= 0.34


def _short_header_line(line: str) -> bool:
    """A title or employer line above a date line: short, name-like, not a
    section, not a bullet, not contact details, not a bare location."""
    s = (line or "").strip()
    if not s or _bare_section(s) or _RANGE_RE.search(s) or _CONTACT_LINE_RE.search(s):
        return False
    core = s.lstrip("#").strip().lstrip(_BULLET_GLYPHS).strip()
    if s.startswith("#") and _CANONICAL_SECTION_RE.match(core):
        return False
    if not core or core.endswith(_SENTENCE_END) or len(core.split()) > 8:
        return False
    # A bare place ("Columbus, OH", "Remote") is not a title or an employer.
    # Only the City, ST shape is excluded: `_LOCATION_TAIL_RE`'s "City, Country"
    # alternative also matches "B.S. in Statistics, University of Illinois".
    if _CITY_STATE_RE.match(core) or core.lower() in ("remote", "hybrid", "onsite", "on-site"):
        return False
    return _lowercase_share(core.split()) <= 0.34


def _preceding_header_lines(lines: Sequence[str], i: int, limit: int = 2) -> List[str]:
    """Up to ``limit`` short header-like lines directly above line ``i``
    (nearest first), stopping at the first line that is not one."""
    out: List[str] = []
    back = i - 1
    while back >= 0 and len(out) < limit:
        prev = lines[back].strip()
        back -= 1
        if not prev:
            continue
        if not _short_header_line(prev):
            break
        out.append(prev.lstrip("#").strip().lstrip(_BULLET_GLYPHS).strip())
    return out


def _span_id(text: str) -> str:
    """Content address for one claim line.

    Deliberately the same formula `evidence.extract_spans` uses, so an id from
    either module names the same line. `test_evidence_inventory` asserts the two
    agree rather than trusting the comment.
    """
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:16]


# ── dates and durations ──────────────────────────────────────────────────────

def _abs_month(token: str) -> Tuple[Optional[int], bool]:
    """'Mar 2023' → an absolute month index; also whether the month was GUESSED.

    A bare year carries no month, so it is read as mid-year — the expected value
    rather than the flattering one. The caller is told it was a guess so the
    review can say the total is approximate instead of quoting it to the month.
    """
    tok = (token or "").strip().lower().rstrip(".")
    if not tok:
        return None, False
    if any(tok.startswith(p) for p in _PRESENT):
        return _current_month_index(), False
    m = re.match(rf"({_MONTHS})[a-z]*\.?\s*(\d{{4}})", tok)
    if m:
        mon = _MONTH_NUMS.get(m.group(1)[:3])
        if mon:
            return int(m.group(2)) * 12 + mon, False
    if re.fullmatch(r"\d{4}", tok):
        return int(tok) * 12 + 6, True     # mid-year; uncertainty is reported
    return None, False


def _current_month_index() -> int:
    """This month as year*12 + month: what "Present" means on a resume.
    One function so tests can pin the date instead of breaking every month."""
    from datetime import datetime
    now = datetime.utcnow()
    return now.year * 12 + now.month


def merged_months(intervals: Iterable[Tuple[int, int]]) -> int:
    """Months covered by a UNION of inclusive [start, end] month ranges.

    The whole point of the module in six lines. Two roles held at once contribute
    their overlap ONCE; a gap between roles contributes nothing. Summing the
    parts double-counts concurrency, and measuring first-start to last-end counts
    unemployment as employment — production did the second and reported ten years
    for a career containing under three.
    """
    spans = sorted((s, e) for s, e in intervals if s is not None and e is not None and e >= s)
    total = 0
    cur_start: Optional[int] = None
    cur_end: Optional[int] = None
    for start, end in spans:
        if cur_start is None:
            cur_start, cur_end = start, end
            continue
        # `<= cur_end + 1` also merges CONTIGUOUS ranges: Dec and the following
        # Jan are continuous employment, not two runs with a zero-month gap.
        if start <= (cur_end or start) + 1:
            cur_end = max(cur_end or end, end)
        else:
            total += (cur_end - cur_start) + 1
            cur_start, cur_end = start, end
    if cur_start is not None and cur_end is not None:
        total += (cur_end - cur_start) + 1
    return total


def humanize_months(months: int) -> str:
    """'2 years 11 months'. Never rounded up — 35 months is not "3 years"."""
    months = max(int(months or 0), 0)
    if months == 0:
        return "none on the resume"
    years, rem = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if rem:
        parts.append(f"{rem} month{'s' if rem != 1 else ''}")
    return " ".join(parts)


# ── the inventory ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Engagement:
    """One dated thing the person did, and what kind of thing it was."""
    kind: str
    title: str
    org: str
    start: Optional[int]          # absolute month index, or None
    end: Optional[int]
    dates_verbatim: str
    section: str
    approximate: bool = False     # a year-only date was involved
    span_ids: Tuple[str, ...] = ()

    @property
    def months(self) -> int:
        if self.start is None or self.end is None or self.end < self.start:
            return 0
        return (self.end - self.start) + 1

    @property
    def is_employment(self) -> bool:
        return self.kind in EMPLOYMENT_KINDS

    @property
    def label(self) -> str:
        bits = [b for b in (self.title.strip(), self.org.strip()) if b]
        return " — ".join(bits) or kind_label(self.kind)


@dataclass(frozen=True)
class SkillEvidence:
    """Where one skill is actually demonstrated, and by what kind of work."""
    skill: str                       # normalized
    display: str                     # as the résumé/JD writes it
    kinds: FrozenSet[str]
    engagements: Tuple[int, ...]     # indices into Inventory.engagements
    span_ids: Tuple[str, ...]
    # Months of paid employment the résumé DATES for this skill — a bullet that
    # carries its own dates, or a job title that names the skill. A skill merely
    # mentioned somewhere inside a six-year job is NOT six years of the skill
    # (audit 2026-09-25: one month of Python in Dec 2025 read as 72 months and
    # "supported" a five-year requirement). 0 = the duration is not stated.
    employment_months: int
    listed_only: bool = False        # only in a skills list; no engagement backs it
    # The union of the EMPLOYMENT engagements the skill appears in. Context for
    # the reader ("used at Acme, a 6-year role") and an upper bound — never a
    # skill duration, and never compared with a requirement.
    role_months: int = 0
    internship_dated_months: int = 0  # same rule as employment_months, for internships

    @property
    def paid_work(self) -> bool:
        """Used in professional or freelance work at all (dated or not)."""
        return bool(self.kinds & EMPLOYMENT_KINDS)

    @property
    def duration_unknown(self) -> bool:
        """Used in paid work, but the résumé never says for how long."""
        return self.paid_work and not self.employment_months

    @property
    def project_only(self) -> bool:
        """Demonstrated ONLY in academic or personal work.

        An internship is deliberately not in here. It is paid work in a real
        team, and telling someone their Python is "a personal project" when they
        used it at an employer is both wrong and insulting; it gets its own case
        (`internship_only`) because the sentence shown to the user differs.
        """
        return bool(self.kinds) and bool(self.kinds & PROJECT_KINDS) \
            and not (self.kinds & EMPLOYMENT_KINDS) and INTERNSHIP not in self.kinds

    @property
    def internship_only(self) -> bool:
        """Used at an employer, but only during an internship."""
        return INTERNSHIP in self.kinds and not (self.kinds & EMPLOYMENT_KINDS)

    @property
    def strength(self) -> int:
        """Sort key: strongest evidence first. Not a score shown to anyone."""
        base = self.employment_months * 4 + len(self.engagements)
        if self.kinds & EMPLOYMENT_KINDS:
            base += 1000
        if INTERNSHIP in self.kinds:
            base += 200
        if self.kinds & PROJECT_KINDS:
            base += 100
        if self.listed_only:
            base -= 50
        return base + len(self.span_ids)


@dataclass(frozen=True)
class Inventory:
    """One reusable, content-addressed inventory of a master résumé."""
    evidence_id: str
    engagements: Tuple[Engagement, ...]
    skills: Dict[str, SkillEvidence]
    employment_months: int
    internship_months: int
    approximate: bool

    def by_kind(self, kind: str) -> Tuple[Engagement, ...]:
        return tuple(e for e in self.engagements if e.kind == kind)

    def skill(self, name: str) -> Optional[SkillEvidence]:
        return self.skills.get(normalize_text(name))

    def ranked_skills(self, names: Sequence[str]) -> List[SkillEvidence]:
        """The named skills we can evidence, strongest evidence first.

        Selection order for the tailored document: an employer reading the top of
        a résumé should meet the best-backed claim first, and a claim we cannot
        back should not be there at all.
        """
        # De-duplicate by the normalized skill: the caller usually concatenates
        # the JD's phrase list with the requirements' skills, so the same skill
        # arrives twice under two spellings and would be listed twice.
        picked: Dict[str, SkillEvidence] = {}
        for name in names:
            ev = self.skill(name)
            if ev is not None:
                picked.setdefault(ev.skill, ev)
        out = list(picked.values())
        out.sort(key=lambda s: (-s.strength, s.display.lower()))
        return out


def _section_kind(header: str) -> Optional[str]:
    h = (header or "").strip().lower()
    if not h:
        return None
    for needle, kind in _SECTION_KINDS:
        if needle in h:
            return kind
    return None


def _refine_kind(kind: str, title: str, org: str) -> str:
    """A title/employer override beats the section it sits under."""
    blob = f"{title} {org}"
    if _DEGREE_LINE_RE.search(title or ""):
        # "B.S. Computer Science, Ohio State University | 2018 - 2022" in a résumé
        # whose sections were lost to PDF extraction is a degree, not four years
        # of employment. Judged on the TITLE only: an engineer employed BY a
        # university is still employed.
        return ACADEMIC
    for pattern, refined in _TITLE_KINDS:
        if pattern.search(blob):
            # An academic section stays academic; a title override cannot promote
            # coursework into employment.
            if kind in PROJECT_KINDS and refined in EMPLOYMENT_KINDS:
                return kind
            return refined
    return kind


def _pipe_org(rest: str) -> str:
    """The employer in '| Acme | Jun 2016 - Aug 2016 | Columbus, OH'.

    Pipe-delimited fields are positional, so the org is the first field that is
    neither a date range nor a place. Splitting the whole string on commas as
    well (which `_split_header` must do for unpunctuated résumés) turned
    "Columbus, OH" into a candidate employer and picked the city.
    """
    for seg in (rest or "").split("|"):
        seg = seg.strip(" ,|–—-\t")
        if not seg or _RANGE_RE.search(seg):
            continue
        if _LOCATION_TAIL_RE.match(seg) or seg.lower() in ("remote", "hybrid", "onsite", "on-site"):
            continue
        return seg
    return ""


def _split_header(header: str) -> Tuple[str, str]:
    """'Senior Engineer | Acme | Remote' → ('Senior Engineer', 'Acme')."""
    parts = [p.strip() for p in re.split(r"\||—|–| at | @ |,", header or "") if p.strip()]
    parts = [p for p in parts if not _RANGE_RE.search(p)]
    if not parts:
        return "", ""
    title = parts[0]
    org = ""
    for p in parts[1:]:
        if _LOCATION_TAIL_RE.match(p) or p.lower() in ("remote", "hybrid", "onsite", "on-site"):
            continue
        org = p
        break
    return title, org


def _is_claim_line(line: str) -> bool:
    """A bullet that asserts something, as opposed to structure.

    Positional counterpart to `evidence._is_structural`: this module needs to
    know WHICH engagement a claim sits under, which the content-addressed span
    list cannot say.
    """
    s = (line or "").strip()
    if not s or s.startswith("#") or s.endswith(":"):
        return False
    if _PIPE_HEADER_RE.match(s):
        return False
    core = s.lstrip("-*•·–—‣▪◦●» \t").strip()
    if not core or _RANGE_RE.fullmatch(core.strip()):
        return False
    return len(core.split()) >= 4


def build_inventory(master_md: str, *, extra_skills: Sequence[str] = ()) -> Inventory:
    """Parse a master résumé into engagements and skill evidence.

    ``extra_skills`` are phrases to look for on top of the ones the résumé's own
    skills section names — normally the JD's phrases, so a requirement can be
    answered even when the résumé never lists the term as a skill.
    """
    text = master_md or ""
    lines = text.splitlines()

    engagements: List[Engagement] = []
    # line index → engagement index, so a bullet can be attributed to the role
    # it sits under.
    owner: Dict[int, int] = {}
    section = ""
    section_kind: Optional[str] = None
    section_level = 0
    current: Optional[int] = None
    pending_spans: Dict[int, List[str]] = {}
    skills_section_lines: List[str] = []
    in_skills = False

    def _enter_section(name: str, level: int) -> None:
        nonlocal section, section_kind, section_level, current, in_skills
        section = name
        section_kind = _section_kind(name)
        section_level = level
        current = None
        in_skills = bool(_SKILLS_SECTION_RE.search(name))

    for i, raw in enumerate(lines):
        header = _SECTION_RE.match(raw)
        if header:
            htext = header.group(1).strip()
            level = len(raw.strip()) - len(raw.strip().lstrip("#"))
            if _RANGE_RE.search(htext):
                # "### Senior Engineer | Acme | 2019 - 2022": a role written as a
                # sub-heading. Read as a role line; it is not a section.
                raw = htext
                current = None
            elif (section_level and level > section_level
                  and not _CANONICAL_SECTION_RE.match(htext)):
                # "### B.S. Computer Science, Ohio State" under "## Education",
                # "### Acme Corp" under "## Experience": a sub-heading naming a
                # role or school, whose dates follow on the next line. It is the
                # title source for that date line (below) and never replaces
                # the section it sits in — doing so made the degree PROFESSIONAL.
                current = None
                continue
            else:
                _enter_section(htext, level)
                continue
        else:
            bare = _bare_section(raw)
            if bare is not None:
                # "EXPERIENCE" / "Technical Skills:" — the section names a PDF
                # or DOCX upload keeps once its markup is gone.
                _enter_section(bare, section_level or 2)
                continue

        if in_skills:
            # Collect every line until the next header — a skills block is
            # normally several labelled rows ("Languages:", "Tools:"), and
            # stopping after the first one dropped most of the list.
            if raw.strip():
                skills_section_lines.append(raw)
            continue

        rng = _RANGE_RE.search(raw)
        pipe = _PIPE_HEADER_RE.match(raw)
        if rng and (pipe or _header_like(raw)):
            if pipe:
                title = pipe.group("title").strip()
                org = _pipe_org(pipe.group("rest"))
            else:
                stripped = (raw[:rng.start()] + " " + raw[rng.end():]).strip(" ,|()–—-\t")
                title, org = _split_header(stripped.lstrip(_BULLET_GLYPHS))
                if not title or not org:
                    # The date sits on its own line, or shares a line with only
                    # the employer: the title (and maybe the employer) is the
                    # short line or two just above it.
                    #   Software Engineer            Software Engineer
                    #   Acme Corp, Columbus, OH      Acme Corp | Jan 2020 - Present
                    #   Jan 2020 - Present
                    above = _preceding_header_lines(lines, i)
                    if not title and above:
                        if len(above) > 1:
                            # title line, then employer line ("Globex Corporation,
                            # Chicago, IL" — the employer is its FIRST field).
                            title, _ = _split_header(above[1])
                            org, _ = _split_header(above[0])
                        else:
                            title, org = _split_header(above[0])
                    elif title and not org and above:
                        org = title
                        title, _ = _split_header(above[0])
            s_abs, s_guess = _abs_month(rng.group("start"))
            e_abs, e_guess = _abs_month(rng.group("end"))
            kind = _refine_kind(section_kind or PROFESSIONAL, title, org)
            engagements.append(Engagement(
                kind=kind, title=title, org=org, start=s_abs, end=e_abs,
                dates_verbatim=rng.group(0).strip(), section=section,
                approximate=bool(s_guess or e_guess),
            ))
            current = len(engagements) - 1
            pending_spans[current] = []
            continue

        if current is not None and _is_claim_line(raw):
            owner[i] = current
            pending_spans[current].append(_span_id(raw.strip().lstrip("-*•·–—‣▪◦●» \t").strip()))

    engagements = [
        Engagement(**{**e.__dict__, "span_ids": tuple(pending_spans.get(idx, ()))})
        for idx, e in enumerate(engagements)
    ]

    employment_months = merged_months(
        (e.start, e.end) for e in engagements if e.is_employment and e.start and e.end)
    internship_months = merged_months(
        (e.start, e.end) for e in engagements if e.kind == INTERNSHIP and e.start and e.end)

    listed = _listed_skills("\n".join(skills_section_lines))
    wanted: List[str] = list(listed) + [s for s in extra_skills if s]
    skills = _attribute_skills(lines, engagements, owner, wanted, listed)

    from app.tailoring.evidence import build_evidence
    return Inventory(
        evidence_id=build_evidence(text).evidence_id,
        engagements=tuple(engagements),
        skills=skills,
        employment_months=employment_months,
        internship_months=internship_months,
        # Only the engagements that FEED a total can make a total approximate.
        # "2021 - 2023" under Education is not a claim about employment dates.
        approximate=any(e.approximate for e in engagements
                        if e.is_employment or e.kind == INTERNSHIP),
    )


def _listed_skills(skills_md: str) -> List[str]:
    """Phrases from a skills section — comma/pipe/bullet separated."""
    out: List[str] = []
    for line in (skills_md or "").splitlines():
        body = line.strip().lstrip("-*•·–—‣▪◦●» \t")
        body = re.sub(r"^\*{0,2}[A-Za-z /&+#.-]{2,30}\*{0,2}\s*:\s*", "", body)  # "Languages:"
        for tok in re.split(r"[,;|/•]|\s{2,}", body):
            tok = tok.strip(" .*`()")
            if 1 < len(tok) <= 40 and not tok.lower().startswith(("http", "www")):
                if tok not in out:
                    out.append(tok)
    return out


#: What separates the parts of a skill name in the wild: "ci/cd", "ci-cd",
#: "CI CD", "node.js", "nodejs" are one skill written five ways.
_SKILL_JOIN = r"[\s/._+-]*"


def _skill_pattern(skill: str) -> re.Pattern:
    """Word-boundary match tolerant of the punctuation skills carry.

    `\b` breaks on ci/cd, node.js, c++ and .net, so the boundaries are spelled
    out and the internal punctuation is allowed to vary. Built by SPLITTING the
    name into its alphanumeric runs and joining them with one separator class —
    chaining `str.replace` calls instead corrupted the class the previous call
    had just inserted, so every multi-word skill compiled to a pattern that
    matched nothing (guard: `test_punctuated_skills_still_match`).
    """
    parts = [p for p in re.split(r"[^\w+#]+", (skill or "").strip()) if p]
    if not parts:
        return re.compile(r"(?!x)x")        # matches nothing
    body = _SKILL_JOIN.join(re.escape(p) for p in parts)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.I)


def _attribute_skills(lines: Sequence[str], engagements: Sequence[Engagement],
                      owner: Dict[int, int], wanted: Sequence[str],
                      listed: Sequence[str]) -> Dict[str, SkillEvidence]:
    listed_norm = {normalize_text(s) for s in listed}
    out: Dict[str, SkillEvidence] = {}
    seen: Dict[str, str] = {}
    for raw_skill in wanted:
        key = normalize_text(raw_skill)
        if not key or key in out:
            continue
        seen.setdefault(key, raw_skill.strip())
        pattern = _skill_pattern(raw_skill)
        hit_engagements: List[int] = []
        span_ids: List[str] = []
        for i, line in enumerate(lines):
            idx = owner.get(i)
            if idx is None or not pattern.search(line):
                continue
            if idx not in hit_engagements:
                hit_engagements.append(idx)
            sid = _span_id(line.strip().lstrip("-*•·–—‣▪◦●» \t").strip())
            if sid not in span_ids:
                span_ids.append(sid)
        # Engagements whose TITLE names the skill ("Python Developer") are
        # evidence for the whole engagement — the title is a claim about the
        # entire role. Such a role counts as hit even with no matching bullet.
        titled = [i for i, e in enumerate(engagements) if e.title and pattern.search(e.title)]
        for i in titled:
            if i not in hit_engagements:
                hit_engagements.append(i)
        kinds = frozenset(engagements[i].kind for i in hit_engagements)
        role_months = merged_months(
            (engagements[i].start, engagements[i].end)
            for i in hit_engagements
            if engagements[i].is_employment and engagements[i].start and engagements[i].end
        )
        dated = _dated_skill_intervals(lines, engagements, owner, pattern, titled)
        months = merged_months(iv for idx, iv in dated if engagements[idx].is_employment)
        intern_months = merged_months(iv for idx, iv in dated
                                      if engagements[idx].kind == INTERNSHIP)
        if not hit_engagements and key not in listed_norm:
            continue          # not evidenced anywhere: not in the inventory at all
        out[key] = SkillEvidence(
            skill=key, display=seen[key], kinds=kinds,
            engagements=tuple(hit_engagements), span_ids=tuple(span_ids),
            employment_months=months, listed_only=not hit_engagements,
            role_months=role_months, internship_dated_months=intern_months,
        )
    return out


#: One "Mon YYYY" token that is not part of a range ("in Dec 2025").
#: Month names spelled exactly — `(mar)[a-z]*` would read "market 2024" as a date.
_SINGLE_MONTH_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{4})\b", re.I)


def _dated_skill_intervals(lines: Sequence[str], engagements: Sequence[Engagement],
                           owner: Dict[int, int], pattern: re.Pattern,
                           titled: Sequence[int]) -> List[Tuple[int, Tuple[int, int]]]:
    """(engagement index, (start, end)) for every span of time the résumé
    itself DATES for this skill.

    Three sources, nothing else:
      * a job title naming the skill → the whole engagement;
      * a bullet naming the skill and carrying a date range → that range;
      * a bullet naming the skill and a single month ("in Dec 2025") → that month.
    Each is clipped to its engagement, so a typo'd bullet date cannot extend a
    role. A bare year in a bullet ("in 2023") dates nothing — it bounds the use
    to some part of a year, and quoting 12 months from it would be the same
    inflation this exists to stop.
    """
    out: List[Tuple[int, Tuple[int, int]]] = []
    for i in titled:
        e = engagements[i]
        if e.start and e.end:
            out.append((i, (e.start, e.end)))
    for li, line in enumerate(lines):
        idx = owner.get(li)
        if idx is None or not pattern.search(line):
            continue
        e = engagements[idx]
        if not (e.start and e.end):
            continue
        consumed: List[Tuple[int, int]] = []
        for m in _RANGE_RE.finditer(line):
            s_abs, _ = _abs_month(m.group("start"))
            e_abs, _ = _abs_month(m.group("end"))
            consumed.append(m.span())
            if s_abs and e_abs:
                s_abs, e_abs = max(s_abs, e.start), min(e_abs, e.end)
                if e_abs >= s_abs:
                    out.append((idx, (s_abs, e_abs)))
        for m in _SINGLE_MONTH_RE.finditer(line):
            if any(a <= m.start() < b for a, b in consumed):
                continue
            mon, _ = _abs_month(m.group(0))
            if mon and e.start <= mon <= e.end:
                out.append((idx, (mon, mon)))
    return out


# ── acronyms ─────────────────────────────────────────────────────────────────

#: Expansions we are sure of. A recruiter's boolean search and an ATS parser hit
#: on one form or the other, not both, so the tailored document should carry the
#: pair once. Nothing is invented: a term absent from this table is left exactly
#: as the résumé and the posting write it.
ACRONYMS: Dict[str, str] = {
    "ci/cd": "continuous integration and continuous delivery",
    "ci": "continuous integration",
    "cd": "continuous delivery",
    "etl": "extract, transform, load",
    "elt": "extract, load, transform",
    "ml": "machine learning",
    "nlp": "natural language processing",
    "llm": "large language model",
    "api": "application programming interface",
    "rest": "representational state transfer",
    "sql": "structured query language",
    "orm": "object-relational mapping",
    "oop": "object-oriented programming",
    "tdd": "test-driven development",
    "sre": "site reliability engineering",
    "iac": "infrastructure as code",
    "k8s": "Kubernetes",
    "eks": "Elastic Kubernetes Service",
    "gke": "Google Kubernetes Engine",
    "aks": "Azure Kubernetes Service",
    "s3": "Simple Storage Service",
    "ec2": "Elastic Compute Cloud",
    "rds": "Relational Database Service",
    "gcp": "Google Cloud Platform",
    "aws": "Amazon Web Services",
    "sdk": "software development kit",
    "ui": "user interface",
    "ux": "user experience",
    "qa": "quality assurance",
    "saas": "software as a service",
    "rbac": "role-based access control",
    "sso": "single sign-on",
    "jwt": "JSON Web Token",
    "grpc": "gRPC remote procedure call",
    "crud": "create, read, update, delete",
    "eda": "exploratory data analysis",
    "cv": "computer vision",
    "rag": "retrieval-augmented generation",
    "mlops": "machine learning operations",
    "dr": "disaster recovery",
    "slo": "service level objective",
    "sla": "service level agreement",
}


def expand_acronym(term: str) -> Optional[str]:
    """The expansion for a term we KNOW, else None. Never a guess."""
    return ACRONYMS.get((term or "").strip().lower().rstrip("."))


def acronym_pairs(terms: Iterable[str]) -> List[Tuple[str, str]]:
    """(term, expansion) for the terms in this list we can expand, de-duplicated."""
    out: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for t in terms or []:
        key = (t or "").strip().lower().rstrip(".")
        if not key or key in seen:
            continue
        exp = ACRONYMS.get(key)
        if exp:
            seen.add(key)
            out.append((t.strip(), exp))
    return out
