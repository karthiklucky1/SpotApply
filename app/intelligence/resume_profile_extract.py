"""Resume -> profile: everything the resume says, not a summary of it.

Founder, 2026-11-14: "my resume has a lot of skills and data but only half is
extracted". Four separate cuts added up to that, all in the read that fills the
profile (`POST /api/resume/extract-profile`):

* the prompt asked for "1-2 sentences" per job, so every job's bullets were
  replaced by a summary, and it never asked for projects, certifications or
  languages at all (the profile page has a section for each);
* it asked for `key_skills` with nothing saying ALL of them, so the model
  returned a short "top skills" list;
* the reply was capped at 2,500 tokens. A long resume's JSON was cut mid-way,
  failed to parse, and the route silently fell back to the free line parser,
  which kept 40 skills / 500 characters and ONE bullet per job;
* the resume itself was cut to 16,000 characters (head + tail), dropping the
  middle of a long one.

This module is the one place the read is defined: the prompt (`build_prompt`),
the reply (`parse_reply`), its shape (`normalise`) and what reaches the profile
(`skills_backstop`, `autofill_update`). Rules it keeps:

* copy, never summarise; a value the resume does not state stays empty;
* the Skills section is also read deterministically and merged in, so a skill
  written there is never lost because a model shortened its list;
* an answer the user already saved in Auto-fill data is never overwritten by
  the resume (only the lists the resume owns are replaced).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

#: Characters of resume sent to the model. A five-page resume is ~15,000; this
#: is a ceiling against a pasted book, not a budget. Past it the head and the
#: tail are kept (education and skills usually sit at the end) and the caller
#: is told, so the cut is never silent.
MAX_RESUME_CHARS = 120_000
_HEAD_CHARS = 90_000
_TAIL_CHARS = 30_000

#: Reply ceiling. The JSON holds the resume's own words, so it is about as long
#: as the resume; 16,000 is far above any real one and still under the SDK's
#: non-streaming limit. 2,500 cut long resumes mid-object.
MAX_OUTPUT_TOKENS = 16_000

#: The read is a person waiting on a button, not a lane: give it longer than
#: `llm_request_timeout` (45 s), which a full word-for-word reply can exceed.
REQUEST_TIMEOUT_S = 120.0

MAX_SKILLS = 200
_SKILL_MAX_CHARS = 60
_BULLETS_MAX_CHARS = 4000

LANGUAGE_LEVELS = ("Native or bilingual", "Fluent", "Professional", "Conversational", "Basic")


def resume_for_prompt(resume_text: str) -> tuple:
    """``(text, truncated)``: the whole resume unless it is absurdly long."""
    text = resume_text or ""
    if len(text) <= MAX_RESUME_CHARS:
        return text, False
    return (text[:_HEAD_CHARS] + "\n\n...[middle of a very long file left out]...\n\n"
            + text[-_TAIL_CHARS:]), True


def build_prompt(resume_text: str) -> str:
    body, _cut = resume_for_prompt(resume_text)
    return f"""Read this resume and return its contents as ONE JSON object.

Copy what the resume says. Do not summarise, shorten, merge, reword or leave anything out.
Use null (or an empty array) for anything the resume does not state. Never invent a value.

Return these exact keys:

first_name, last_name, email, phone,
location ("City, State" as written in the header),
address (an object with street, city, state, postal_code, country: only the parts the header states, null for the rest),
current_title (the most recent job title),
years_experience (integer: the years of paid work the dates add up to),
linkedin_url, github_url, portfolio_url,
professional_summary (the resume's own summary or objective paragraph, in full; if it has none, 2-3 sentences describing the background),

key_skills (an ARRAY of strings with EVERY skill, tool, programming language, framework, library,
  platform, database, cloud service and method the resume names ANYWHERE: first the whole Skills
  section, item by item, then any others that appear only in the experience or project bullets.
  One item per entry, spelled as in the resume, no duplicates, no category labels such as
  "Languages:". Do not stop at a "top" list: a resume that names 60 skills returns 60 entries),

experience (an array, one object for EVERY job, most recent first, with keys:
  title, company, location,
  employment_type ("Full-time", "Part-time", "Contract", "Internship" or "Freelance" if the resume says so or the title makes it plain, else null),
  start (e.g. "Jun 2024"), end (e.g. "Mar 2026" or "Present"), current (true or false),
  bullets (an array with EVERY bullet or sentence written under that job, word for word, in order)),

education (an array, one object for EVERY school, most recent first, with keys:
  degree (e.g. "Master of Engineering"), field (the major or subject, e.g. "Computer Science";
  do NOT repeat the degree name here), university, country (if stated, else null),
  start_year (integer or null), end_year (integer or null), gpa (string or ""), gpa_scale (string or "")),

projects (an array, one object per project, with keys: title, bullets (every bullet or sentence
  written under that project, word for word, in order)),

certifications (an array, one object per certification or license, with keys: name, issuer, year),

languages (spoken or written human languages only, never programming languages; an array of
  objects with keys: language, proficiency (as the resume states it, else null)),

suggested_target_roles (an array of 4-6 specific job titles this candidate is genuinely
  well-qualified for and should apply to right now, ordered best-fit first. Use real,
  searchable titles like "Senior Backend Engineer" or "Data Scientist", based on the
  candidate's actual experience, seniority and skills, not generic guesses),

degree, university, graduation_year (integer): the most recent education entry again.

IMPORTANT: read the ENTIRE resume, including the Education, Projects and Skills sections (often at the end).
- "university" = the school, college or institution name (e.g. "University of Cincinnati"). Do not leave it null if any school is listed.
- "degree" = the highest or most recent degree (e.g. "Master of Science in Computer Science").
- "graduation_year" = the most recent graduation year as a 4-digit integer.
- List EVERY school, EVERY job, EVERY project and EVERY skill found in the resume, not just the latest or the most important.

Resume:
{body}

Return only valid JSON, no markdown, no explanation."""


def parse_reply(raw: str) -> Optional[dict]:
    """The reply as a dict, or None when it is not a complete JSON object."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        start = text.find("{")
        if start < 0:
            return None
        text = text[start:]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


# ── shape ────────────────────────────────────────────────────────────────────

def _s(v: Any, limit: int = 200) -> str:
    if v is None or isinstance(v, (dict, list, tuple, set, bool)):
        return ""
    return " ".join(str(v).split())[:limit]


def _lines(v: Any, limit: int = _BULLETS_MAX_CHARS) -> str:
    """Bullets as one string, one per line. Accepts a list or a string."""
    if isinstance(v, (list, tuple)):
        parts = [_s(x, 1000) for x in v]
    elif isinstance(v, str):
        parts = [" ".join(p.split()) for p in v.replace("\r\n", "\n").split("\n")]
    else:
        parts = []
    parts = [re.sub(r"^[\-*•·▪]\s*", "", p).strip() for p in parts]
    return "\n".join(p for p in parts if p)[:limit]


def _year(v: Any) -> Optional[int]:
    m = re.search(r"\b(19|20)\d{2}\b", str(v)) if v not in (None, "") else None
    return int(m.group(0)) if m else None


def split_skills(v: Any) -> list:
    """Skills as a clean list, order kept, duplicates (any case) dropped."""
    if isinstance(v, str):
        items = re.split(r"[,;\n]+", v)
    elif isinstance(v, (list, tuple)):
        items = [x for x in v if isinstance(x, (str, int, float))]
    else:
        items = []
    out, seen = [], set()
    for item in items:
        skill = _s(item, _SKILL_MAX_CHARS).strip(" .;:-")
        key = skill.lower()
        if skill and key not in seen:
            seen.add(key)
            out.append(skill)
        if len(out) >= MAX_SKILLS:
            break
    return out


def language_level(stated: Any) -> str:
    """A resume's wording for how well a language is spoken, as one of the
    profile's five levels; "" when the resume does not say (never guessed)."""
    t = _s(stated, 80).lower()
    if not t:
        return ""
    for words, level in (
        (("native", "mother tongue", "bilingual", "first language"), "Native or bilingual"),
        (("limited working", "conversational", "intermediate"), "Conversational"),
        (("full professional", "fluent", "proficient"), "Fluent"),
        (("professional", "working", "advanced", "business"), "Professional"),
        (("basic", "elementary", "beginner"), "Basic"),
    ):
        if any(w in t for w in words):
            return level
    return ""


def normalise(extracted: Any) -> dict:
    """One shape whatever produced the read (the model or the free parser).

    ``experience[].summary`` holds every bullet, one per line: the profile page
    shows them as bullets and the fill pack types them as the description."""
    src = extracted if isinstance(extracted, dict) else {}
    out = dict(src)
    out["key_skills"] = ", ".join(split_skills(src.get("key_skills")))

    jobs = []
    for e in src.get("experience") or []:
        if not isinstance(e, dict):
            continue
        end = _s(e.get("end"), 40)
        current = bool(e.get("current")) or end.lower() in ("present", "current", "now")
        job = {
            "title": _s(e.get("title")), "company": _s(e.get("company")),
            "location": _s(e.get("location")), "employment_type": _s(e.get("employment_type"), 40),
            "start": _s(e.get("start"), 40), "end": "Present" if current else end,
            "current": current,
            "summary": _lines(e.get("bullets")) or _lines(e.get("summary")),
        }
        if job["title"] or job["company"] or job["summary"]:
            jobs.append(job)
    out["experience"] = jobs[:15]

    schools = []
    for e in src.get("education") or []:
        if not isinstance(e, dict):
            continue
        school = {
            "degree": _s(e.get("degree")), "field": _s(e.get("field")),
            "university": _s(e.get("university")), "country": _s(e.get("country"), 60),
            "start_year": _year(e.get("start_year")), "end_year": _year(e.get("end_year")),
            "gpa": _s(e.get("gpa"), 12), "gpa_scale": _s(e.get("gpa_scale"), 12),
        }
        if school["degree"] or school["university"]:
            schools.append(school)
    out["education"] = schools[:10]

    projects = []
    for e in src.get("projects") or []:
        if not isinstance(e, dict):
            continue
        title = _s(e.get("title") or e.get("name"), 120)
        if title:
            projects.append({"title": title,
                             "description": _lines(e.get("bullets")) or _lines(e.get("description"))})
    out["projects"] = projects[:10]

    certs = []
    for e in src.get("certifications") or []:
        if isinstance(e, str):
            e = {"name": e}
        if not isinstance(e, dict):
            continue
        name = _s(e.get("name"), 100)
        if name:
            year = _year(e.get("year"))
            certs.append({"name": name, "issuer": _s(e.get("issuer"), 80),
                          "year": str(year) if year else ""})
    out["certifications"] = certs[:15]

    langs = []
    for e in src.get("languages") or []:
        if isinstance(e, str):
            e = {"language": e}
        if not isinstance(e, dict):
            continue
        name = _s(e.get("language"), 40)
        if name:
            langs.append({"language": name, "proficiency": language_level(e.get("proficiency"))})
    out["languages"] = langs[:8]

    addr = src.get("address") if isinstance(src.get("address"), dict) else {}
    out["address"] = {k: _s(addr.get(k), 120) for k in ("street", "city", "state", "postal_code", "country")}
    return out


# ── the Skills section, read without a model ─────────────────────────────────

def skills_backstop(resume_text: str) -> list:
    """Every item written in the resume's Skills section, in order.

    Deterministic (the free parser's `skill_items`), so a model that shortens
    its list cannot lose a skill the resume plainly lists. "CI/CD" and "AI/ML"
    stay whole (a slash is not a separator); "AWS (SageMaker, EC2)" yields AWS,
    SageMaker, EC2; a category label ("Languages:") is dropped; anything that
    reads as a sentence is not a skill."""
    from app.intelligence.resume_basic_extract import _split_sections, skill_items
    return split_skills(skill_items(_split_sections(resume_text or "").get("skills", "")))


def merge_skills(primary: Any, backstop: Any) -> str:
    """``primary`` (the model's list) with the backstop's missing items added."""
    return ", ".join(split_skills(split_skills(primary) + split_skills(backstop)))


# ── what the resume may write into Auto-fill data ────────────────────────────

_EDU_LEVELS = (
    (r"\b(m\.?d\.?|j\.?d\.?|juris doctor|doctor of medicine)\b", "Professional degree (MD, JD)"),
    (r"\b(ph\.?\s?d|doctor(ate)? of|doctoral)\b", "Doctorate (PhD)"),
    (r"\b(master|m\.?\s?s\.?c?|m\.?\s?eng|m\.?\s?tech|m\.?b\.?a|m\.?\s?a\.?)\b", "Master's degree"),
    (r"\b(bachelor|b\.?\s?s\.?c?|b\.?\s?eng|b\.?\s?tech|b\.?\s?a\.?|b\.?\s?e\.?)\b", "Bachelor's degree"),
    (r"\bassociate\b", "Associate degree"),
    (r"\b(high school|secondary school|ged)\b", "High school or equivalent"),
)


def highest_education(schools: list) -> str:
    """The highest level among the degrees the resume lists, or ""."""
    rank = [level for _rx, level in _EDU_LEVELS]
    best = None
    for s in schools or []:
        degree = (s.get("degree") or "") if isinstance(s, dict) else ""
        for rx, level in _EDU_LEVELS:
            if re.search(rx, degree, re.I):
                if best is None or rank.index(level) < rank.index(best):
                    best = level
                break
    return best or ""


def autofill_update(read: dict, saved: dict) -> dict:
    """The part of a normalised read that belongs in `UserProfile.autofill_json`.

    Projects, certifications and languages are the resume's own lists and are
    replaced when the resume has any (like jobs and schools). A single answer
    (city, state, ZIP, country, street, highest education) is filled ONLY where
    the user has saved nothing: what they typed is never overwritten."""
    update: dict = {}
    for key in ("projects", "certifications", "languages"):
        if read.get(key):
            update[key] = read[key]
    addr = read.get("address") or {}
    city, state = addr.get("city") or "", addr.get("state") or ""
    if not (city and state):
        # "Cincinnati, OH" in the header is a city and a state.
        m = re.fullmatch(r"\s*([^,]{2,40}),\s*([A-Za-z .]{2,30})\s*", read.get("location") or "")
        if m:
            city, state = city or m.group(1).strip(), state or m.group(2).strip()
    answers = {"city": city, "state": state, "postal_code": addr.get("postal_code") or "",
               "country": addr.get("country") or "", "address_line1": addr.get("street") or "",
               "highest_education": highest_education(read.get("education") or [])}
    for key, value in answers.items():
        if value and not (saved or {}).get(key):
            update[key] = value
    return update


def counts(read: dict) -> dict:
    """How much was read, for the message the user sees after the read."""
    return {
        "skills": len(split_skills(read.get("key_skills"))),
        "jobs": len(read.get("experience") or []),
        "bullets": sum(len([l for l in (j.get("summary") or "").split("\n") if l.strip()])
                       for j in read.get("experience") or []),
        "schools": len(read.get("education") or []),
        "projects": len(read.get("projects") or []),
        "certifications": len(read.get("certifications") or []),
        "languages": len(read.get("languages") or []),
    }
