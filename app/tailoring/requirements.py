"""Experience requirements, read as written — and the review shown before download.

An employer's experience requirement is a sentence, not a number. "4-6 years",
"three (3) years", "2+", "at least 18 months" and "one year of professional
experience" are five different requirements, and rounding them to one integer
loses the thing the candidate needs to know: how far short they are, and whether
the shortfall is the kind a screening call survives.

So a requirement keeps its VERBATIM wording, its bound(s) in months, whether it
was stated as required or preferred, and which skill (if any) it attached to.
Assessment then compares it against `inventory.Inventory` — union-merged months
of paid employment — and says one of five things:

    supported     the résumé holds the time, in paid work, with the skill on it
    short         evidenced, but less time than the posting asks for
    project_only  demonstrated in academic or personal work; not employment
    listed_only   named in a skills list; nothing describes actually using it
    gap           no evidence on the résumé at all

Nothing here produces a score, a ranking or an estimated chance of an interview.
`review()`'s whole output is either a fact about the résumé and the posting, or a
question we are handing back because we cannot answer it honestly. The pre-
download review exists so the person clicking Download knows which requirements
their document actually answers BEFORE an employer tells them it does not.

A SUGGESTED project is not evidence. Skill-gap advice ("build a small project
using X") lives in `improvement_plan` and is checked AGAINST the document: if a
suggestion turns up in the tailored résumé as something already done, that is a
fabrication and `unconfirmed_project_claims` names it. When no advice is passed
the plan is derived from this posting's own unevidenced skills, because the check
is the point and it was dead while nothing supplied the list — and this class is
not covered elsewhere: `evidence.fabrication_violations` compares employers,
titles, dates, degrees and numbers, and a skill is none of those.

Nothing in this module calls an LLM or the network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from app.tailoring.inventory import (Inventory, SkillEvidence, humanize_months,
                                     kind_label)

# ── parsing an experience requirement ────────────────────────────────────────

_WORD_NUMBERS: Dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20,
}
_QTY = r"\d{1,2}(?:\.\d)?|" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))

# "at least three (3) years", "4-6 years", "2+ yrs", "18 months"
_REQ_RE = re.compile(
    rf"(?P<lead>at least|at a minimum|minimum(?:\s+of)?|min\.?|no less than|"
    rf"more than|over|upwards of|up to)?\s*"
    rf"(?P<qty>{_QTY})\s*"
    rf"(?:\(\s*(?P<paren>\d{{1,2}})\s*\))?\s*"
    rf"(?P<plus>\+)?\s*"
    rf"(?:(?:[-–—]|\bto\b)\s*(?P<qty2>{_QTY})\s*(?P<plus2>\+)?\s*)?"
    rf"(?P<unit>years?|yrs?\.?|months?|mos?\.?)\b",
    re.I,
)

_PREFERRED_RE = re.compile(
    r"\b(?:preferred|preferab\w*|prefer|nice[- ]to[- ]have|a plus|plus\b|bonus|"
    r"desirable|desired|ideally|ideal candidate|would be great|advantage|"
    r"good to have|not required)\b", re.I)
_REQUIRED_RE = re.compile(
    r"\b(?:required|requires|require|must have|must[- ]haves?|minimum|at least|"
    r"essential|mandatory|you have|you'll need|qualifications)\b", re.I)

# Words between "N years" and the skill that are grammar, not the skill.
_FILLER = (
    "of", "in", "with", "using", "the", "a", "an", "and", "or",
    "professional", "hands-on", "hands", "on", "relevant", "industry",
    "practical", "demonstrable", "proven", "combined", "total", "overall",
    "commercial", "full-time", "recent", "prior", "previous", "direct",
    "experience", "experiences", "work", "working", "working-level",
    "developing", "development", "building", "designing", "supporting",
    "programming", "engineering", "software", "hands­on",
)
#: The skill phrase ends here. "or" and "and" start a second requirement.
_SKILL_STOP = re.compile(
    r"[,.;:()\[\]]|\band\b|\bor\b|\bplus\b|\bincluding\b|\bsuch as\b|\bwith\b|"
    r"\bpreferred\b|\brequired\b|\bis\b|\bare\b|\bwould\b|\bwill\b", re.I)


def _qty(token: str) -> Optional[float]:
    t = (token or "").strip().lower()
    if not t:
        return None
    if t in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[t])
    try:
        return float(t)
    except ValueError:
        return None


_NOT_A_SKILL = frozenset(
    list(_WORD_NUMBERS) + [
        "experience", "years", "year", "months", "month", "software",
        "software development", "development", "engineering", "technology",
        "technologies", "tools", "systems", "solutions", "applications",
        "platforms", "services", "environment", "environments", "stack",
        "equivalent", "degree", "field", "domain", "role", "team", "teams",
        # Words from a JD's own STRUCTURE. "Minimum Qualifications" is a heading;
        # listing "minimum" as a skill the résumé lacks is noise that buries the
        # real gaps, and it reached the improvement plan as something to go and
        # learn.
        "minimum", "minimums", "qualification", "qualifications", "preferred",
        "requirement", "requirements", "responsibility", "responsibilities",
        "basic", "essential", "essentials", "must", "musts", "nice", "plus",
        "bonus", "desired", "duties", "benefits", "description", "summary",
        "overview", "about", "skill", "skills", "ability", "abilities",
        "knowledge", "understanding", "familiarity", "background", "expertise",
        "proficiency", "hands", "plusses",
    ])


def _plausible_skill(phrase: str) -> bool:
    """A concrete thing someone can have used, not a number or a domain noun.

    `extract_jd_phrases` is tuned for ATS phrase coverage, where "software
    development" is a legitimate search term. It is not a legitimate GAP: telling
    someone "software development: not present anywhere on your résumé" about a
    résumé headed "Software Engineer" is noise that buries the real gaps, and
    "three" arrived as a skill from "Three (3) years".
    """
    p = (phrase or "").strip().lower()
    if not p or p in _NOT_A_SKILL:
        return False
    if re.fullmatch(r"[\d.+\-/ ]+", p):
        return False
    return all(tok not in _WORD_NUMBERS for tok in p.split())


def _skill_after(tail: str) -> str:
    """The skill a requirement attaches to, or '' when it names none."""
    from app.tailoring.ats_keywords import is_skill_like

    text = (tail or "").strip()
    # Strip a leading run of grammar words, one at a time, so "of professional
    # experience with Kubernetes" reduces to "Kubernetes".
    while True:
        m = re.match(r"([A-Za-z][\w+#./-]*)\s*", text)
        if not m or m.group(1).lower() not in _FILLER:
            break
        text = text[m.end():]
    stop = _SKILL_STOP.search(text)
    if stop:
        text = text[:stop.start()]
    words = text.split()[:4]
    while words:
        candidate = " ".join(words).strip(" .,;:-/")
        if candidate and is_skill_like(candidate) and _plausible_skill(candidate):
            return candidate
        words.pop()          # try a shorter phrase: "Java Spring Boot" → "Java"
    return ""


@dataclass(frozen=True)
class ExperienceRequirement:
    """One stated experience requirement, with its wording intact."""
    verbatim: str
    months_min: int
    months_max: Optional[str | int] = None
    skill: str = ""
    required: bool = True

    @property
    def wording(self) -> str:
        return self.verbatim.strip()

    def describe(self) -> str:
        head = self.wording
        if self.skill:
            head = f"{head} ({self.skill})"
        return head if self.required else f"{head} — preferred, not required"


_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[-*•]\s*)?(?:\*\*)?\s*"
    r"(?P<body>[A-Za-z][A-Za-z /&'()-]{2,60})(?:\*\*)?\s*:?\s*$")
_REQUIRED_HEADING_RE = re.compile(
    r"\b(?:minimum|basic|required|requirements|must[- ]haves?|essential|"
    r"what you(?:'ll)? need|qualifications)\b", re.I)


def _is_heading(line: str) -> bool:
    """A section label, as opposed to a requirement.

    Only used to decide whether a "Preferred Qualifications" heading should make
    the bullets under it preferred. A line carrying a years phrase is never a
    heading, however short it is.
    """
    s = (line or "").strip()
    if not s or _REQ_RE.search(s):
        return False
    if s.startswith("#") or s.endswith(":"):
        return True
    return bool(_HEADING_RE.match(s)) and len(s.split()) <= 6


def _clauses(text: str) -> List[Tuple[str, Optional[bool]]]:
    """(clause, heading_preference) pairs in document order.

    Two scoping fixes live here. A preference word binds to its own CLAUSE, not
    to the whole line: in "5 years of experience; Kafka preferred" the preference
    is about Kafka, and reading it as optional five years understates what the
    posting asks for. And a "Preferred Qualifications:" HEADING makes the bullets
    under it preferred, which is the usual JD shape — calling those required
    invents blocking gaps and talks a candidate out of a job they could get.
    """
    out: List[Tuple[str, Optional[bool]]] = []
    heading_pref: Optional[bool] = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if _is_heading(line):
            if _PREFERRED_RE.search(line):
                heading_pref = False          # everything below is preferred
            elif _REQUIRED_HEADING_RE.search(line):
                heading_pref = True
            continue
        # Independent clauses: semicolons and sentence ends. Commas are NOT
        # boundaries — "three (3) years, Java" is one requirement.
        for clause in re.split(r";|(?<=[.!?])\s+|\s+[•·]\s+", line):
            clause = clause.strip()
            if clause:
                out.append((clause, heading_pref))
    return out


def parse_requirements(jd_text: str, *, limit: int = 12) -> List[ExperienceRequirement]:
    """Every experience requirement a posting states, in the order stated.

    De-duplicated on (months, skill): a posting that repeats "3+ years of Java"
    in the summary and again in the bullets states one requirement.
    """
    text = re.sub(r"<[^>]+>", " ", jd_text or "")
    out: List[ExperienceRequirement] = []
    seen: set[Tuple[int, str]] = set()

    for clause, heading_pref in _clauses(text):
        for m in _REQ_RE.finditer(clause):
            qty = _qty(m.group("paren") or m.group("qty"))
            if qty is None or qty <= 0 or qty > 40:
                continue
            if (m.group("lead") or "").strip().lower() == "up to":
                # "up to 5 years" is a ceiling, not a floor — it asks for nothing.
                continue
            unit_months = 1 if m.group("unit").lower().startswith(("month", "mo")) else 12
            months_min = int(round(qty * unit_months))
            qty2 = _qty(m.group("qty2") or "")
            months_max: Optional[int] = None
            if qty2 is not None and qty2 * unit_months >= months_min:
                months_max = int(round(qty2 * unit_months))
            if m.group("plus") or m.group("plus2"):
                months_max = None       # "2+" has no upper bound
            skill = _skill_after(clause[m.end():])

            # Clause wins over heading; an explicit "required" in the clause wins
            # over an explicit preference in the same clause.
            if _REQUIRED_RE.search(clause):
                required = True
            elif _PREFERRED_RE.search(clause):
                required = False
            elif heading_pref is not None:
                required = heading_pref
            else:
                required = True

            key = (months_min, skill.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(ExperienceRequirement(
                verbatim=m.group(0).strip(), months_min=months_min,
                months_max=months_max, skill=skill, required=required,
            ))
            if len(out) >= limit:
                return out
    return out


# ── assessing one requirement against the inventory ──────────────────────────

SUPPORTED = "supported"
SHORT = "short"
PROJECT_ONLY = "project_only"     # demonstrated in academic/personal work only
LISTED_ONLY = "listed_only"       # named in a skills list; nothing describes using it
GAP = "gap"


@dataclass(frozen=True)
class RequirementAssessment:
    """What the résumé can actually answer, and in what."""
    requirement: ExperienceRequirement
    status: str
    held_months: int
    backing: Tuple[str, ...] = ()       # engagement labels that back it
    note: str = ""
    approximate: bool = False

    @property
    def is_gap(self) -> bool:
        return self.status in (GAP, PROJECT_ONLY, LISTED_ONLY)

    def line(self) -> str:
        """One plain sentence. No score, no percentage, no prediction.

        The note is part of the sentence, not an optional extra a caller may
        forget: "résumé shows 3 months" without "…during an internship" is a
        number the reader will misread, and `line()` is what the UI renders.
        """
        req = self.requirement.describe()
        held = humanize_months(self.held_months)
        approx = " (approximate — the résumé dates some roles by year only)" \
            if self.approximate else ""
        note = f" {self.note}." if self.note else ""
        if self.status == SUPPORTED:
            return f"{req}: résumé shows {held}{approx}.{note}"
        if self.status == SHORT:
            return (f"{req}: résumé shows {held}{approx} — short of what the "
                    f"posting asks for.{note}")
        if self.status in (PROJECT_ONLY, LISTED_ONLY):
            return f"{req}: {self.note}."
        return f"{req}: nothing on the résumé evidences this."


def assess(req: ExperienceRequirement, inv: Inventory) -> RequirementAssessment:
    """Compare one requirement against the inventory. Facts only."""
    ev: Optional[SkillEvidence] = inv.skill(req.skill) if req.skill else None

    if req.skill and ev is None:
        return RequirementAssessment(req, GAP, 0, approximate=inv.approximate)

    if ev is not None and ev.project_only:
        where = ", ".join(sorted(kind_label(k) for k in ev.kinds)) or "a project"
        return RequirementAssessment(
            req, PROJECT_ONLY, 0,
            note=(f"demonstrated in {where}, not in paid employment — a completed "
                  f"project shows the skill but is not years of professional experience"),
            approximate=inv.approximate)

    if ev is not None and ev.internship_only:
        # Real work at a real employer, and most postings asking for "N years of
        # professional experience" do not mean an internship. Say both facts
        # rather than picking the flattering one.
        held = inv.internship_months
        return RequirementAssessment(
            req, SHORT if held < req.months_min else SUPPORTED, held,
            backing=tuple(inv.engagements[i].label for i in ev.engagements),
            note=(f"used during an internship ({humanize_months(held)}), which this "
                  f"résumé counts separately from professional experience"),
            approximate=inv.approximate)

    if ev is not None and ev.listed_only:
        return RequirementAssessment(
            req, LISTED_ONLY, 0,
            note=("in your skills list, but no role or project on the résumé "
                  "describes using it"),
            approximate=inv.approximate)

    # Skill-specific time when the requirement named a skill; otherwise the
    # résumé's overall employment time.
    held = ev.employment_months if ev is not None else inv.employment_months
    backing: Tuple[str, ...] = ()
    if ev is not None:
        backing = tuple(inv.engagements[i].label for i in ev.engagements
                        if inv.engagements[i].is_employment)
    else:
        backing = tuple(e.label for e in inv.engagements if e.is_employment)

    status = SUPPORTED if held >= req.months_min else SHORT
    note = ""
    if status == SHORT and inv.internship_months and ev is None:
        note = (f"{humanize_months(inv.internship_months)} of internship experience "
                f"is on the résumé and is counted separately")
    return RequirementAssessment(req, status, held, backing=backing, note=note,
                                 approximate=inv.approximate)


# ── the pre-download review ──────────────────────────────────────────────────

@dataclass(frozen=True)
class PreDownloadReview:
    """What this document answers, what it does not, and what only you can say."""
    supported: Tuple[str, ...] = ()
    gaps: Tuple[str, ...] = ()
    questions: Tuple[str, ...] = ()
    improvement_plan: Tuple[str, ...] = ()
    unconfirmed_claims: Tuple[str, ...] = ()
    employment_summary: str = ""

    @property
    def ok(self) -> bool:
        """Nothing in the document claims something unevidenced."""
        return not self.unconfirmed_claims

    def as_dict(self) -> dict:
        return {
            "employment_summary": self.employment_summary,
            "requirements_supported": list(self.supported),
            "genuine_gaps": list(self.gaps),
            "open_questions": list(self.questions),
            "improvement_plan": list(self.improvement_plan),
            "unconfirmed_claims": list(self.unconfirmed_claims),
        }

    def as_text(self) -> str:
        blocks = [f"Experience on the résumé: {self.employment_summary}"]
        for title, items in (("Requirements this résumé supports", self.supported),
                             ("Genuine gaps", self.gaps),
                             ("Open questions — only you can answer these", self.questions),
                             ("Improvement plan (not on the résumé)", self.improvement_plan),
                             ("Unverified claims found in the draft", self.unconfirmed_claims)):
            if items:
                blocks.append(title + ":\n" + "\n".join(f"  - {i}" for i in items))
        return "\n\n".join(blocks)


def unconfirmed_project_claims(tailored_md: str,
                               suggested: Sequence[str]) -> List[str]:
    """Suggested-but-unconfirmed work that turned up in the document as done.

    Skill-gap advice is a plan. If "build a small project using Kafka" becomes a
    Kafka bullet in the résumé, the document asserts something the person has not
    told us they did, which is the fabrication class this whole area exists to
    prevent.
    """
    from app.tailoring.inventory import _skill_pattern

    text = tailored_md or ""
    found: List[str] = []
    for name in suggested or []:
        name = (name or "").strip()
        if not name or name.lower() in [f.lower() for f in found]:
            continue
        if _skill_pattern(name).search(text):
            found.append(name)
    return found


def review(master_md: str, tailored_md: str, jd_text: str, *,
           suggested_projects: Sequence[str] = ()) -> PreDownloadReview:
    """The short review a user reads before downloading the document.

    Built from the MASTER résumé (what is true) and the POSTING (what is asked),
    then checked against the TAILORED draft (what we wrote).
    """
    from app.tailoring.ats_keywords import extract_jd_phrases, skill_phrases
    from app.tailoring.inventory import build_inventory

    reqs = parse_requirements(jd_text)
    jd_skills: List[str] = []
    try:
        jd_skills = [p for p in skill_phrases(extract_jd_phrases(jd_text, top_n=24))
                     if _plausible_skill(p)]
    except Exception:          # phrase extraction is a nicety, not a gate
        jd_skills = []
    extra = [r.skill for r in reqs if r.skill] + jd_skills
    inv = build_inventory(master_md, extra_skills=extra)

    supported: List[str] = []
    gaps: List[str] = []
    questions: List[str] = []
    # Skills the posting asks for that the résumé evidences NOWHERE. These are
    # the improvement plan, and they are also what the draft must not quietly
    # start claiming — see the `unconfirmed` check below.
    unevidenced: List[str] = []

    for req in reqs:
        a = assess(req, inv)
        line = a.line()

        if a.status == SUPPORTED:
            supported.append(line)
            continue

        # A PREFERRED requirement we fall short of is not a genuine gap. Filing
        # it as one invents a blocker and talks someone out of a job the posting
        # says they can have without it.
        if not req.required:
            questions.append(
                f"{line} The posting lists this as preferred, not required — "
                f"it is not a blocker on its own.")
            continue

        gaps.append(line)
        if a.status == GAP and req.skill and req.skill not in unevidenced:
            unevidenced.append(req.skill)

        shortfall = req.months_min - a.held_months
        if a.status == SHORT and 0 < shortfall <= 12:
            questions.append(
                f"The posting asks for {req.wording}; the résumé shows "
                f"{humanize_months(a.held_months)}. Whether that gap matters here "
                f"is the employer's call — decide if you want to apply.")
        elif a.status == PROJECT_ONLY:
            questions.append(
                f"{req.skill or req.wording} appears only outside paid employment. "
                f"If you used it in a role the résumé does not mention, add it; "
                f"otherwise leave it as project evidence and say so if asked.")
        elif a.status == LISTED_ONLY:
            questions.append(
                f"{req.skill or req.wording} is in your skills list but nothing on "
                f"the résumé describes using it. Add where you used it, or remove "
                f"it — a recruiter will ask.")

    # Skills the posting asks for that the résumé evidences nowhere at all. Kept
    # SHORT: this is a requirements review, not a keyword-coverage report, and
    # `ats_keywords.analyze` already covers the latter.
    named = {r.skill.lower() for r in reqs if r.skill}
    missing_entirely: List[str] = []
    for phrase in jd_skills:
        if len(missing_entirely) >= 5:
            break
        if phrase.lower() in named or inv.skill(phrase) is not None:
            continue
        from app.tailoring.inventory import _skill_pattern
        if _skill_pattern(phrase).search(master_md or ""):
            continue          # present in the document, just not as an attributed skill
        missing_entirely.append(phrase)
    for phrase in missing_entirely:
        gaps.append(f"{phrase}: not present anywhere on the résumé.")
        if phrase not in unevidenced:
            unevidenced.append(phrase)

    if inv.approximate:
        questions.append(
            "Some roles on the résumé are dated by year only, so the totals above "
            "are approximate. Adding months would make them exact.")
    if inv.internship_months:
        questions.append(
            f"{humanize_months(inv.internship_months)} of internship experience is "
            f"counted separately from professional experience. Employers differ on "
            f"whether they count it; say which you are quoting if asked.")

    # The improvement plan. A caller with real skill-gap advice passes it; with
    # nothing passed we derive it from THIS posting, so the plan is never empty
    # when there is something to say — and, more importantly, so the check below
    # actually runs. It was dead: nothing supplied `suggested_projects`, so a
    # draft that started claiming a skill the résumé has never evidenced passed
    # silently. `evidence.fabrication_violations` does not catch this class — a
    # skill is not an employer, a date or a number.
    to_check = list(suggested_projects or ()) or unevidenced[:6]
    if suggested_projects:
        plan = [f"{p} — suggested, not on the résumé until you confirm you have done it"
                for p in suggested_projects]
    else:
        plan = [f"{p} — no evidence on your résumé. Doing something real with it and "
                f"adding that is the fix; it does not belong on the résumé until then"
                for p in to_check]
    leaked = unconfirmed_project_claims(tailored_md, to_check)

    summary = humanize_months(inv.employment_months)
    if inv.internship_months:
        summary += (f" of paid employment, plus "
                    f"{humanize_months(inv.internship_months)} of internships")
    else:
        summary += " of paid employment"

    return PreDownloadReview(
        supported=tuple(dict.fromkeys(supported)),
        gaps=tuple(dict.fromkeys(gaps)),
        questions=tuple(dict.fromkeys(questions)),
        improvement_plan=tuple(plan),
        unconfirmed_claims=tuple(leaked),
        employment_summary=summary,
    )
