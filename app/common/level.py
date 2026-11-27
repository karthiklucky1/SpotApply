"""Experience level: ONE answer to "is this posting at this person's level?".

WHY THIS EXISTS (2026-11-24). Level was judged in three places that each knew
part of it, and the largest group of users fell between them:

  * ``RuleFilter`` dropped a posting stating far more years than the person
    has, and Staff/Principal/Director titles for anyone under 6 years;
  * ``reranker.reconcile_with_profile`` held a posting stating 2+ more years
    under the board's bar;
  * both did so only when ``years_experience > 0``. Zero was read as "unknown"
    — and it is also what a new graduate's resume honestly says.

Production then (counts only): 31 of 35 profiles had 0-4 years, 14 of them 0.
A role paying $347K-$385K was delivered at 80 to a new graduate; "Lead
Engineer" and Senior roles with no years stated went to people with 3 years;
since 2026-08-15 a quarter of everything delivered (770 of 3,151) carried
"Senior" in its title, and people with 3-4 years who OPENED those acted on 23%
of them against 59% for postings with no level word.

THE RULE, in the order the evidence is trusted:

  1. The posting STATES years (``requirements.minimum_years_required``, read as
     written). That number governs, whatever the title says: "Senior ..., 4+
     years" asks for 4. Two or more years beyond the person is a STRETCH; four
     or more is OUT OF REACH.
  2. It states none, and its TITLE carries a level word. Senior is in reach
     from ``level_senior_min_years`` (4), Lead from 5, Staff / Principal /
     Director / VP / Head of from 6. One year short is a stretch; further short
     is out of reach. Someone whose own target roles or current title name the
     level gets Senior and Lead a year sooner — they asked for it, or hold it.
  3. It states none and has no level word, but its pay STARTS at
     ``level_senior_pay_floor_usd``: a senior-level band, a stretch below the
     senior threshold. Never "out of reach" — pay is the weakest evidence.

OUT OF REACH is decided before any paid call (the rule filter stamps it).
STRETCH is scored, then held at ``reranker.STRETCH_OVERALL_CAP``: off the
board, still in All Jobs with its reasons.

WHAT "ZERO YEARS" MEANS is decided here once (``known_years``): 0 on a profile
whose resume we have READ is someone who has not worked yet, and every rule
above applies to them. 0 on an empty profile is unknown, and only the Staff+
title rule applies — exactly as before.

A level word is read from the TITLE only, and only where it is one: "Member of
Technical Staff" is an ordinary engineer, "Staff Accountant" and "Staff Nurse"
are not senior, "Senior Living Coordinator" is about residents, and "Lead
Generation" is not a lead.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.config import settings

OUT_OF_REACH = "out_of_reach"   # not worth a paid score: stamped by the rule filter
STRETCH = "stretch"             # scored, then held under the board's bar

SENIOR, LEAD, STAFF = "senior", "lead", "staff"
_LABEL = {SENIOR: "senior", LEAD: "lead", STAFF: "senior/staff"}


@dataclass(frozen=True)
class Verdict:
    """What the level rule says about one posting for one person. Falsy when
    the posting is in reach (or nothing can be said)."""
    code: str = ""          # "" | STRETCH | OUT_OF_REACH
    basis: str = ""         # stated_years | title | pay
    reason: str = ""        # the rule filter's stamp when OUT_OF_REACH
    concern: str = ""       # the line the fit report shows, whichever code

    def __bool__(self) -> bool:
        return bool(self.code)


_IN_REACH = Verdict()


# ── The person ───────────────────────────────────────────────────────────────

def _has_rows(raw) -> bool:
    """A JSON list column with at least one entry."""
    if isinstance(raw, (list, tuple)):
        return len(raw) > 0
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(value, list) and len(value) > 0


def known_years(profile) -> Optional[float]:
    """Years of experience the person can be held to, or None when unknown.

    ``years_experience == 0`` is two different facts. On a profile whose resume
    was read (an upload stamp, or jobs / schools the read produced) it means
    the dates add up to no paid work yet: a student or new graduate, and 0 is
    the answer. On a profile with none of that, nobody has told us anything.
    """
    if profile is None:
        return None
    try:
        years = float(getattr(profile, "years_experience", 0) or 0)
    except (TypeError, ValueError):
        years = 0.0
    if years > 0:
        return years
    if (isinstance(getattr(profile, "resume_uploaded_at", None), datetime)
            or _has_rows(getattr(profile, "experience_json", None))
            or _has_rows(getattr(profile, "education_json", None))):
        return 0.0
    return None


# ── The title ────────────────────────────────────────────────────────────────

# Words that look like a level and are not one.
_NOT_A_LEVEL_RE = re.compile(
    r"\b(?:member of )?technical staff\b"
    r"|\bchief of staff\b"
    r"|\bleads?\s+gen(?:eration)?\b|\blead\s+time\b"
    r"|\b(?:senior|sr\.?)\s+(?:living|care|caregiver|center|centre|citizens?|services|"
    r"community|housing|homes?|adults?|residents?)\b"
    r"|\b(?:school|assistant|vice)\s+principal\b")

# "Staff" is a senior level beside these nouns and an ordinary word elsewhere
# (Staff Accountant, Staff Nurse, Staff Attorney, Staff Writer).
_STAFF_NOUN = (r"(?:engineer|developer|scientist|architect|designer|researcher|programmer|"
               r"sre|technologist|product manager|program manager)")
_STAFF_RE = re.compile(
    r"\b(?:principal|distinguished|director|vp|vice[- ]president|head of|chief)\b"
    r"|\bengineering manager\b|\blead software engineer\b"
    rf"|\bstaff\b(?=(?:[\s,/&()-]+[\w+#.]+){{0,4}}[\s,/&()-]+{_STAFF_NOUN}\b)"
    rf"|\b{_STAFF_NOUN}\b[\s,/&()-]+(?:[\w+#.]+[\s,/&()-]+){{0,2}}staff\b")

# "Lead" beside professional work; a Shift Lead or Lead Cashier is a supervisor
# of an hourly team, not five years of experience.
_LEAD_RE = re.compile(r"\blead\b")
_LEAD_NOUN_RE = re.compile(
    r"\b(?:engineer\w*|developer|scientist|architect|designer|analyst|researcher|programmer|"
    r"sre|devops|data|software|machine learning|ml|ai|qa|security|product|platform|backend|"
    r"back[- ]end|frontend|front[- ]end|full[- ]?stack|cloud|infrastructure|tech(?:nical)?|"
    r"team|consultant|manager)\b")
_SENIOR_RE = re.compile(r"\b(?:senior|sr|snr)\b")


def title_level(title: str) -> str:
    """The level a posting's TITLE names: SENIOR, LEAD, STAFF or ''. The highest
    one wins ("Senior Staff Engineer" is staff)."""
    t = _NOT_A_LEVEL_RE.sub(" ", (title or "").lower())
    if _STAFF_RE.search(t):
        return STAFF
    if _LEAD_RE.search(t) and _LEAD_NOUN_RE.search(_LEAD_RE.sub(" ", t)):
        return LEAD
    if _SENIOR_RE.search(t):
        return SENIOR
    return ""


def _min_years(level: str) -> int:
    return int(getattr(settings, f"level_{level}_min_years", 0) or 0)


def _names_level(profile, level: str) -> bool:
    """The person's own target roles or current title name this level: they
    asked for it, or already hold it."""
    for field in ("target_roles", "current_title"):
        for part in re.split(r"[,;\n]", str(getattr(profile, field, "") or "")):
            if title_level(part) == level:
                return True
    return False


# ── The pay ──────────────────────────────────────────────────────────────────

def pay_floor_usd(salary_text: str) -> Optional[float]:
    """Where the posting's annual USD pay range STARTS, or None.

    Read from ``Job.salary_text`` only — the ATS's own pay field, or the facet
    the posting was stamped with at write time (app/strategy/job_facets.py),
    both already limited to pay. Scanning the description here would read "a
    $300,000 - $500,000 budget" as a salary. A RANGE only: a single figure is
    as often a bonus or a ceiling as a floor; an hourly or monthly range is
    under the annual floor below and reads as nothing.
    """
    from app.matching.filters.rule_filter import _SALARY_RANGE_RE, _SYMBOL_CCY
    for m in _SALARY_RANGE_RE.finditer(salary_text or ""):
        if _SYMBOL_CCY.get(m.group(1)) != "USD":
            continue
        try:
            lo = float(m.group(2).replace(",", "")) * (1000 if m.group(3) else 1)
            hi = float(m.group(4).replace(",", "")) * (1000 if m.group(5) else 1)
        except ValueError:
            continue
        if lo >= 30_000 and hi >= lo:
            return lo
    return None


# ── The decision ─────────────────────────────────────────────────────────────

def _g(years: float) -> str:
    return f"{years:g}"


def judge(profile, title: str, description: str = "", salary_text: str = "") -> Verdict:
    """Is this posting above this person's level? See the module docstring for
    the rule. Never raises: a level it cannot read is a posting in reach."""
    if profile is None:
        return _IN_REACH
    try:
        return _judge(profile, title or "", description or "", salary_text or "")
    except Exception:                      # a reader's bug must not hide a job
        return _IN_REACH


def _judge(profile, title: str, description: str, salary_text: str) -> Verdict:
    cand = known_years(profile)
    level = title_level(title)

    # 1. The posting's own stated years govern — and far beyond the person is
    #    out of reach before anything else is read.
    req = None
    if cand is not None and description.strip():
        from app.matching.filters.rule_filter import _degree_level
        from app.tailoring.requirements import minimum_years_required
        req = minimum_years_required(
            description, degree_level=_degree_level(getattr(profile, "degree", "") or ""))
    stated = (f"Posting asks {_g(req[0])}+ years; resume shows about {_g(cand)}"
              if req else "")
    if req and req[0] - cand >= 4:
        return Verdict(OUT_OF_REACH, "stated_years",
                       f"Experience pre-filtered: requires {_g(req[0])}+ years (candidate has "
                       f"{_g(cand)}) — \"{req[1][:120]}\"", stated)

    # Staff / Principal / Director / VP: under the threshold, or unknown — the
    # rule filter's long-standing rule, whatever years the posting states.
    staff_min = _min_years(STAFF)
    if level == STAFF and staff_min > 0 and (cand is None or cand < staff_min):
        return Verdict(OUT_OF_REACH, "title",
                       f"Title pre-filtered: '{title}' is a senior/staff-level role",
                       "Senior/staff-level title" + (
                           f"; resume shows about {_g(cand)} years" if cand is not None else ""))
    if cand is None:
        return _IN_REACH

    if req:
        if req[0] - cand >= 2:
            return Verdict(STRETCH, "stated_years", stated, stated)
        return _IN_REACH

    # 2. No stated years: the title's level word.
    if level in (SENIOR, LEAD):
        need = _min_years(level)
        if need > 0 and _names_level(profile, level):
            need -= 1
        if need > 0 and cand < need:
            label = _LABEL[level]
            concern = f"{label.capitalize()}-level title; resume shows about {_g(cand)} years"
            if need - cand >= 2:
                return Verdict(OUT_OF_REACH, "title",
                               f"Title pre-filtered: '{title}' is a {label}-level role "
                               f"(candidate has {_g(cand)} years)", concern)
            return Verdict(STRETCH, "title", concern, concern)
        return _IN_REACH
    if level:
        return _IN_REACH

    # 3. Neither: a pay band that starts at senior level.
    floor = int(getattr(settings, "level_senior_pay_floor_usd", 0) or 0)
    senior_min = _min_years(SENIOR)
    if floor > 0 and senior_min > 0 and cand < senior_min:
        lo = pay_floor_usd(salary_text)
        if lo is not None and lo >= floor:
            concern = (f"Pay starts at ${lo:,.0f}, a senior-level band; resume shows about "
                       f"{_g(cand)} years")
            return Verdict(STRETCH, "pay", concern, concern)
    return _IN_REACH
