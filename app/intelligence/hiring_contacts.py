"""Job-specific people & organisation assertions from PUBLIC job data.

Research: docs/research/hiring-contacts-2026-09.md. This module is the
"cheap extraction before paid research" layer: it reads text SpotApply
already holds (Job.description) plus the raw ATS payload the scrapers fetch,
and emits *assertions* — never a single `hiring_manager` column.

Every assertion carries:
  relationship   what the evidence establishes (reporting_manager, recruiter,
                 posting_creator, job_contact, hiring_company, recruiting_agency,
                 team, contact_email …)
  evidence_type  named_for_this_job | title_only_for_this_job |
                 structured_field | self_identified | generic_mailbox
  field / quote  where in the source it came from, verbatim
  origin_key     hash of the normalised quote so five syndicated mirrors of
                 one sentence count as ONE evidence origin, not five

Design rules (from the research):
  * a title ("reports to the Engineering Manager") is NOT a person — most
    reporting lines in the wild are title-only, and the extractor must say so
  * "direct reports", "reporting tools", "report to work", "generate reports"
    are NOT reporting lines
  * a teammate's or author's own reporting line is not the vacancy's
  * nothing here invents a surname, resolves an identity, or calls an LLM;
    identity resolution is a separate, later step with its own provenance
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable

# ── assertion record ──────────────────────────────────────────────────────────

@dataclass
class Assertion:
    relationship: str
    evidence_type: str
    quote: str
    field: str = "description"
    name: str | None = None
    title: str | None = None
    organization: str | None = None
    source_url: str = ""
    qualifier: str | None = None          # "initially", "may later move to …"
    origin_key: str = ""

    def __post_init__(self) -> None:
        if not self.origin_key:
            self.origin_key = origin_key(self.quote)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def origin_key(quote: str) -> str:
    """Stable key for 'the same sentence' across mirrors/whitespace/case."""
    norm = re.sub(r"\s+", " ", (quote or "").strip().lower())
    norm = re.sub(r"[^a-z0-9@ ]", "", norm)
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


# ── text patterns ─────────────────────────────────────────────────────────────

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_NAME = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?:\s+(?:[A-Z]\.|[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?)){0,3}"
_TITLE_WORDS = (
    r"(?:chief|cto|ceo|coo|cfo|cpo|vp|svp|evp|vice president|head|director|manager|lead|"
    r"founder|co-founder|cofounder|principal|president|officer|partner|owner|supervisor|"
    r"architect|engineer|scientist|staff)"
)

# The advertised role reports to X.  Subject must be the role/reader, not a
# team or a teammate, so we anchor on the common role subjects.
_REPORTS_TO = re.compile(
    r"(?:(?:you|you'll|you will|this (?:role|position)|the (?:role|position)|"
    r"the successful candidate|the ideal candidate|this person|the hire|the new hire|"
    r"this individual|the incumbent)\s+(?:will\s+|would\s+|initially\s+)?"
    r"reports?\s+(?:directly\s+|initially\s+)?(?:in)?to|"
    r"\breporting\s+(?:directly\s+|initially\s+)?(?:in)?to|"
    r"\breports?\s+to:|"
    r"^\s*reports?\s+(?:directly\s+)?to)\s*"
    r"(?:the\s+|our\s+|a\s+|an\s+)?"
    r"(?P<target>[^.;\n]{3,120}?)"
    r"(?=[.;\n]|,\s*(?:and|who|you|where|with|in)\b|\s+(?:and|who|where)\b|"
    r"\s+(?:Location|Department|Team|Type|Salary|Schedule|Employment Type|Job Type|Level|"
    r"Status|Compensation|Hours|Travel|Posted|FLSA|Classification|Pay|Grade|Job ID|Req(?:uisition)? ID):|$)",
    re.IGNORECASE,
)

# Labelled fields that many templates (Workday, SuccessFactors, agencies) emit.
_LABELLED = re.compile(
    r"(?P<label>hiring manager|recruiter|recruitment consultant|recruiting contact|"
    r"contact person|contact|posted by|talent partner|talent acquisition partner|"
    r"point of contact|questions\?? contact)\s*[:\-–]\s*"
    r"(?P<value>[^\n|•]{2,120})",
    re.IGNORECASE,
)
_LABEL_TO_REL = {
    "hiring manager": "reporting_manager",
    "recruiter": "recruiter",
    "recruitment consultant": "recruiter",
    "recruiting contact": "recruiter",
    "talent partner": "recruiter",
    "talent acquisition partner": "recruiter",
    "contact person": "job_contact",
    "contact": "job_contact",
    "point of contact": "job_contact",
    "questions? contact": "job_contact",
    "questions contact": "job_contact",
    "posted by": "job_poster",
}

# First-person author who says the role reports to them.
_AUTHOR_INTRO = re.compile(
    r"\b(?i:I(?:'m|\s+am))\s+(?P<name>" + _NAME + r")\b[^.\n]{0,80}",
)
_REPORTS_TO_ME = re.compile(
    r"\b(?:report(?:s|ing)?\s+(?:directly\s+)?to\s+me|work(?:ing)?\s+directly\s+(?:with|for)\s+me|"
    r"I(?:'ll| will)\s+be\s+your\s+(?:manager|direct manager|hiring manager))\b",
    re.IGNORECASE,
)

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_GENERIC_MAILBOX = re.compile(
    r"^(?:careers?|jobs?|recruit(?:ing|ment)?|talent|hr|hiring|people|apply|applications?|"
    r"info|hello|contact|team|work|join|noreply|no-reply|support)\b",
    re.IGNORECASE,
)

_ON_BEHALF = re.compile(
    r"(?:on behalf of|for our client,?|our client,?|recruiting for|hiring for)\s+"
    r"(?:a\s+|an\s+|the\s+)?(?P<client>[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,4})",
)
_AGENCY_SELF = re.compile(
    r"\b(?P<agency>[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})\s+is\s+(?:a|an)\s+"
    r"(?:recruit(?:ing|ment)|staffing|talent|search)\s+(?:agency|firm|partner|company|consultancy)",
)

_TEAM = re.compile(
    r"\b(?:join|part of|member of|within|on)\s+(?:the|our)\s+"
    r"(?P<team>[A-Z][\w&/+-]*(?:\s+[A-Z&][\w&/+-]*){0,4})\s+(?:team|org|organization|organisation|group|squad)\b",
)

# Things that look like reporting lines and are not.
_NEGATIVE = re.compile(
    r"direct reports|reports? to work|report to the office|reporting tools?|"
    r"reporting (?:system|dashboard|pipeline|infrastructure|solution|framework|line[s]? of business)|"
    r"generate reports?|build(?:ing)? reports?|financial report|status report|bug report|"
    r"incident report|expense report|report(?:s|ing)? (?:on|about|of)\b",
    re.IGNORECASE,
)
_TEAM_SUBJECT = re.compile(
    r"\b(?:the team|this team|our team|the group|the department|the function)\s+reports?\s+to",
    re.IGNORECASE,
)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,.;:-–")


def _split_named_target(target: str) -> tuple[str | None, str | None]:
    """'our CTO, Jane Doe' / 'Jane Doe, VP Engineering' / 'Jane Doe (CTO)' /
    'the Engineering Manager' → (name|None, title|None)."""
    t = _clean(target)
    m = re.match(r"^(?P<name>" + _NAME + r")\s*[,(]\s*(?P<title>[^)]{2,60})\)?$", t)
    if m and _looks_like_title(m.group("title")):
        return m.group("name"), _clean(m.group("title"))
    m = re.match(r"^(?P<title>[^,]{2,60}),\s*(?P<name>" + _NAME + r")$", t)
    if m and _looks_like_title(m.group("title")):
        return m.group("name"), _clean(m.group("title"))
    if re.fullmatch(_NAME, t) and not _looks_like_title(t):
        return t, None
    return None, t


def _looks_like_title(s: str) -> bool:
    return bool(re.search(_TITLE_WORDS, s, re.IGNORECASE))


# ── public API ────────────────────────────────────────────────────────────────

def extract_from_text(text: str, *, source_url: str = "", field_name: str = "description",
                      max_assertions: int = 40) -> list[Assertion]:
    """Regex-only first pass. Returns assertions in document order, deduplicated
    on origin_key. Never returns a person the text did not name."""
    out: list[Assertion] = []
    seen: set[str] = set()
    text = text or ""

    def add(a: Assertion) -> None:
        if a.origin_key in seen or len(out) >= max_assertions:
            return
        seen.add(a.origin_key)
        out.append(a)

    # 1. Labelled fields ("Hiring Manager: Jane Doe", "Recruiter: …")
    for m in _LABELLED.finditer(text):
        label = m.group("label").lower()
        label = re.sub(r"\s+", " ", label)
        rel = _LABEL_TO_REL.get(label) or _LABEL_TO_REL.get(label.rstrip("?")) or "job_contact"
        value = _clean(m.group("value"))
        if _EMAIL.fullmatch(value):
            continue  # handled by the email pass
        name, title = _split_named_target(value)
        add(Assertion(relationship=rel,
                      evidence_type="named_for_this_job" if name else "title_only_for_this_job",
                      quote=_clean(m.group(0)), field=field_name, name=name, title=title,
                      source_url=source_url))

    # 2. Reporting lines
    for sent in _SENT_SPLIT.split(text):
        if not sent or _TEAM_SUBJECT.search(sent):
            continue
        for m in _REPORTS_TO.finditer(sent):
            window = sent[max(0, m.start() - 40): m.end() + 40]
            if _NEGATIVE.search(window):
                continue
            name, title = _split_named_target(m.group("target"))
            qual = None
            if re.search(r"\binitially\b", sent, re.IGNORECASE):
                qual = "initially"
            if re.search(r"\b(?:may|might|will) (?:later|eventually|in future) (?:move|transition|report)", sent, re.IGNORECASE):
                qual = (qual + "; " if qual else "") + "later move stated"
            add(Assertion(relationship="reporting_manager",
                          evidence_type="named_for_this_job" if name else "title_only_for_this_job",
                          quote=_clean(sent)[:300], field=field_name, name=name, title=title,
                          qualifier=qual, source_url=source_url))

    # 3. First-person author who says the role reports to them
    if _REPORTS_TO_ME.search(text):
        intro = _AUTHOR_INTRO.search(text)
        if intro:
            name = intro.group("name")
            title = None
            tail = text[intro.end("name"): intro.end("name") + 80]
            tm = re.match(r"\s*,?\s*(?:the\s+|our\s+|and I\s+|I\s+)?(?:lead|head|run|manage)?\s*(?P<t>[^.,;\n]{3,60})", tail)
            if tm and _looks_like_title(tm.group("t")):
                title = _clean(tm.group("t"))
            add(Assertion(relationship="reporting_manager", evidence_type="self_identified",
                          quote=_clean(text[intro.start(): min(len(text), intro.start() + 200)]),
                          field=field_name, name=name, title=title, source_url=source_url,
                          qualifier="author self-identification; first name may be all the source gives"))

    # 4. Agency / client split
    for m in _AGENCY_SELF.finditer(text):
        add(Assertion(relationship="recruiting_agency", evidence_type="named_for_this_job",
                      quote=_clean(m.group(0)), field=field_name,
                      organization=_clean(m.group("agency")), source_url=source_url))
    for m in _ON_BEHALF.finditer(text):
        add(Assertion(relationship="hiring_company_named_in_ad", evidence_type="named_for_this_job",
                      quote=_clean(text[max(0, m.start() - 30): m.end() + 30]), field=field_name,
                      organization=_clean(m.group("client")), source_url=source_url))

    # 5. Team names
    for m in _TEAM.finditer(text):
        add(Assertion(relationship="team", evidence_type="named_for_this_job",
                      quote=_clean(m.group(0)), field=field_name,
                      organization=_clean(m.group("team")), source_url=source_url))

    # 6. Emails (never a person by themselves)
    for m in _EMAIL.finditer(text):
        addr = m.group(0)
        local = addr.split("@", 1)[0]
        generic = bool(_GENERIC_MAILBOX.match(local))
        add(Assertion(relationship="contact_email",
                      evidence_type="generic_mailbox" if generic else "named_for_this_job",
                      quote=addr, field=field_name, name=None if generic else local,
                      source_url=source_url,
                      qualifier=None if generic else "personal mailbox; identity not resolved"))
    return out


# ── structured payloads ───────────────────────────────────────────────────────

_PERSON_KEY = re.compile(
    r"(hiring[_ -]?manager|recruiter|creator|poster|posted[_ -]?by|contact|owner|"
    r"hiring[_ -]?team|author|dc:creator|applicationContact|contactPerson|contact_?list|"
    r"application_?contacts)",
    re.IGNORECASE,
)
_TEAM_KEY = re.compile(r"^(department|departments|team|teams|categories|function|hiringOrganization|"
                       r"jobRequisitionLocation|refNumber|requisition_?id|jobReqId|internal_job_id|"
                       r"reference|metadata)$", re.IGNORECASE)


def scan_person_like_keys(payload: Any, *, path: str = "") -> list[tuple[str, Any]]:
    """Walk any JSON payload; return (path, value) for non-empty values under a
    person-ish key. This is the probe's 'what did the ATS actually expose' pass —
    it does NOT assert a relationship, it just reports the field exists."""
    found: list[tuple[str, Any]] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            p = f"{path}.{k}" if path else str(k)
            if _PERSON_KEY.search(str(k)) and v not in (None, "", [], {}):
                found.append((p, v))
            found.extend(scan_person_like_keys(v, path=p))
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            found.extend(scan_person_like_keys(v, path=f"{path}[{i}]"))
    return found


def scan_org_keys(payload: Any, *, path: str = "") -> list[tuple[str, Any]]:
    """Department / team / requisition keys — the join keys for exact-req matching."""
    found: list[tuple[str, Any]] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            p = f"{path}.{k}" if path else str(k)
            if _TEAM_KEY.match(str(k)) and v not in (None, "", [], {}):
                found.append((p, v))
            found.extend(scan_org_keys(v, path=p))
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            found.extend(scan_org_keys(v, path=f"{path}[{i}]"))
    return found


def extract_from_structured(source: str, payload: dict, *, source_url: str = "") -> list[Assertion]:
    """Known, DOCUMENTED person/org fields per ATS. Anything else is left to
    scan_person_like_keys so the probe can tell us what we're missing."""
    out: list[Assertion] = []
    src = (source or "").lower()
    if not isinstance(payload, dict):
        return out

    if src == "smartrecruiters":
        c = payload.get("creator") or {}
        name = c.get("name") or " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x)
        if name:
            out.append(Assertion(relationship="posting_creator", evidence_type="structured_field",
                                 quote=str(name), field="creator", name=name, source_url=source_url,
                                 qualifier="SmartRecruiters 'creator' = employee who created the posting; "
                                           "usually a recruiter/coordinator, not the manager"))
        dep = (payload.get("department") or {}).get("label")
        if dep:
            out.append(Assertion(relationship="team", evidence_type="structured_field",
                                 quote=str(dep), field="department.label", organization=dep,
                                 source_url=source_url))
    elif src == "greenhouse":
        for m in payload.get("metadata") or []:
            nm = str(m.get("name") or "")
            val = m.get("value")
            if val and _PERSON_KEY.search(nm):
                rel = "reporting_manager" if "manager" in nm.lower() else "recruiter"
                out.append(Assertion(relationship=rel, evidence_type="structured_field",
                                     quote=f"{nm}: {val}", field=f"metadata[{nm}]",
                                     name=str(val) if isinstance(val, str) else None,
                                     source_url=source_url,
                                     qualifier="employer-exposed custom field; verify it names a person"))
        for d in payload.get("departments") or []:
            if d.get("name"):
                out.append(Assertion(relationship="team", evidence_type="structured_field",
                                     quote=d["name"], field="departments[].name",
                                     organization=d["name"], source_url=source_url))
    elif src == "lever":
        cats = payload.get("categories") or {}
        for k in ("team", "department"):
            if cats.get(k):
                out.append(Assertion(relationship="team", evidence_type="structured_field",
                                     quote=cats[k], field=f"categories.{k}", organization=cats[k],
                                     source_url=source_url))
    elif src == "ashby":
        for k in ("team", "department"):
            if payload.get(k):
                out.append(Assertion(relationship="team", evidence_type="structured_field",
                                     quote=payload[k], field=k, organization=payload[k],
                                     source_url=source_url))
    elif src in ("jobtech", "arbetsformedlingen"):
        for c in payload.get("application_contacts") or []:
            nm = c.get("name") or c.get("description")
            if nm:
                out.append(Assertion(relationship="job_contact", evidence_type="structured_field",
                                     quote=str(c), field="application_contacts[]", name=nm,
                                     title=c.get("contact_type"), source_url=source_url))
        emp = payload.get("employer") or {}
        if emp.get("workplace") and emp.get("workplace") != emp.get("name"):
            out.append(Assertion(relationship="hiring_company_named_in_ad", evidence_type="structured_field",
                                 quote=f"employer.name={emp.get('name')} employer.workplace={emp.get('workplace')}",
                                 field="employer.workplace", organization=emp["workplace"],
                                 source_url=source_url,
                                 qualifier="workplace != posting org; agency relationship must be confirmed in text"))
    elif src == "nav":
        for c in payload.get("contactList") or []:
            if c.get("name"):
                out.append(Assertion(relationship="job_contact", evidence_type="structured_field",
                                     quote=str({k: c.get(k) for k in ("name", "title", "role")}),
                                     field="contactList[]", name=c["name"],
                                     title=c.get("title") or c.get("role"), source_url=source_url))
    elif src == "usajobs":
        details = ((payload.get("MatchedObjectDescriptor") or payload).get("UserArea") or {}).get("Details") or {}
        for k in ("AgencyContactEmail", "AgencyContactPhone"):
            if details.get(k):
                out.append(Assertion(relationship="job_contact", evidence_type="structured_field",
                                     quote=str(details[k]), field=f"UserArea.Details.{k}",
                                     organization=details.get("AgencyMarketingStatement") and None,
                                     source_url=source_url,
                                     qualifier="agency HR contact for the announcement, not the selecting official"))
    return out


def summarize(assertions: Iterable[Assertion]) -> dict[str, int]:
    """Counts by (relationship, evidence_type) — the shape of the probe report."""
    counts: dict[str, int] = {}
    for a in assertions:
        key = f"{a.relationship}/{a.evidence_type}"
        counts[key] = counts.get(key, 0) + 1
    return counts
