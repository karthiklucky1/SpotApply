"""Questions to answer once: what application forms REQUIRED that nothing could
answer (founder, 2026-11-15: "what data do we need to take from the user?").

Measured on 16 real forms: about 80% of the required fields left open are
questions only employers ask, and they repeat ("Are you willing to travel?",
"Do you have a valid driver's license?", "Are you subject to a non-compete?").
No catalog written in advance lists them all, so the forms themselves do: the
extension reports each required question still open after a fill (its wording,
kind and choices, never a value), and this module turns those reports into ONE
list per person. Answer a line once and it is saved as that person's own
answer (``AnswerMemory``), which the extension fills on every later form that
asks the same thing, with no model involved.

Storage is the existing ``PendingQuestion`` table ("a question the autofill
agent could not answer; the user answers it on the dashboard"), one row per
application and question. Rows written here carry ``field_type`` ``ext:<kind>``
so the server-side agent's own rows are never touched.

What may be SAVED for reuse is decided by ``classify``:

* work authorization, self-identification and "how did you hear" have their
  own place in the profile and their own rules: listed with a pointer there,
  never saved here;
* an agreement, a consent or a signature is accepted on the form it belongs
  to: listed, never saved (agreeing from a saved default is agreeing to
  something the person never read);
* a date of birth, an identity or bank number is not stored at all;
* a question about THIS job or THIS employer ("Have you worked here before?")
  has a different answer at every employer: listed, never saved;
* a contact detail or a link the profile already holds ("Phone", "Address"),
  and a document SpotApply writes for each job (a cover letter), are not
  missing data: the form's own widget did not take them. Listed with a pointer
  to the profile, never asked for a second time;
* everything else is the person's to answer once.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from app.autofill import field_guards, form_answers

log = logging.getLogger(__name__)

MAX_PER_REPORT = 40
MAX_QUESTION_CHARS = 300
MAX_ANSWER_CHARS = 300
LIST_WINDOW_DAYS = 45
KINDS = ("radio", "dropdown", "select", "yesno", "checkbox", "multi", "text", "textarea")
_PREFIX = "ext:"

# Never stored, by anyone, for any reason.
_NEVER_STORED_RE = re.compile(
    r"\b(?:date of birth|birth ?date|dob|social security|ssn|national (?:id|insurance)\w*|passport(?: number)?"
    r"|tax (?:id|number)|bank account|routing number|iban|credit card (?:number|details)|password|passcode)\b",
    re.I)

# The same pattern as the extension's _JOB_RELATIVE_RE (pinned equal by test).
_JOB_RELATIVE_RE = re.compile(
    r"\b(?:worked|work|employed|interviewed|applied) (?:here|for us|with us|at this)\b"
    r"|\b(?:this|our|the) (?:company|organi[sz]ation|firm|employer|role|position|job|opening|team|office"
    r"|location|site|facility|store|branch|hq|headquarters)\b"
    r"|\b(?:with|for|at|to|join) us\b|\bour (?:employees?|staff|clients?|customers?|products?)\b", re.I)

# ACCEPTING something: a statement in the first person, a "by clicking", a named
# policy or a signature. Narrower than form_answers._AGREEMENT_RE on purpose:
# "Are you subject to a non-compete?" or "Are you willing to take a drug test?"
# is a fact about the person, which they may well save; a model may not guess it.
_ACCEPTANCE_RE = re.compile(
    r"\bi (?:hereby |also |further )?(?:agree|consent|certify|acknowledge|attest|authori[sz]e|confirm"
    r"|understand|declare|accept)\b"
    r"|\bby (?:checking|clicking|submitting|signing|applying|typing|entering|selecting)\b"
    r"|\bdo you (?:agree|consent|accept|acknowledge|authori[sz]e|certify)\b"
    r"|\b(?:consent|agree) to (?:the|our|this|these|be)\b"
    r"|\bterms (?:and|&) conditions\b|\bterms of (?:use|service)\b"
    r"|\bprivacy (?:policy|notice|statement)\b|\be-?signature\b|\bsignature\b|\bsign(?:ed)? (?:here|below)\b"
    # A consent under its own heading ("CONSENT TO BACKGROUND CHECK: Section
    # 10-21.9 of The School Code…", a real Lever form): its legal text says
    # "authorize", which read as a work-authorization question.
    r"|^\W*consent\b|\bconsent to (?:an? )?(?:background|credit|reference|criminal|drug)\b",
    re.I)

# Answered from the profile itself: a contact detail, an address part, a link.
# Matched on the WHOLE question, so "What city would you relocate to?" is not one.
_PROFILE_FIELD_RE = re.compile(
    r"^\W*(?:(?:your |my )?(?:(?:first|last|full|legal|preferred|given|family|middle) ?name|name|surname"
    r"|e-?mail(?: address)?|(?:mobile |cell |home |contact )?(?:phone|telephone)(?: number)?|mobile(?: number)?"
    r"|(?:street |home |mailing |current |postal )?address(?: line ?[12])?|city|town|state|province|county"
    r"|zip(?: ?code)?|postal ?code|postcode|country"
    r"|linkedin(?: (?:profile|url))?|github(?: (?:profile|url))?|website|portfolio(?: (?:url|link))?))\W*$", re.I)
# Written for each job by SpotApply, or uploaded: never typed once and reused.
_DOCUMENT_RE = re.compile(r"\b(?:cover letter|motivation(?:al)? letter|resume|curriculum vitae|cv)\b", re.I)

_STAR_REQUIRED_RE = re.compile(r"\s*[*✱]\s*\(?\s*required\s*\)?", re.I)
_IS_REQUIRED_TAIL_RE = re.compile(r"\s*(?:[*✱]\s*)?\b(?:an?|this|the)\s+([\w ]{1,40}?)\s+is required\.?\s*$", re.I)
_TAILS = tuple(re.compile(p, re.I) for p in (
    r"\s*\(\s*required\s*\)\s*$", r"\s*[*✱]+\s*$", r"\s*\d+\s*/\s*\d*\s*$"))
_LEAD_REQUIRED_RE = re.compile(r"^\s*\(\s*required\s*\)\s*", re.I)

def display(question: Optional[str]) -> str:
    """The question as a person reads it: without the form's own decoration
    ("*Required", "A response is required", a character counter). The KEY a
    saved answer is recalled by is never changed, only what the list shows and
    what ``classify`` reads."""
    t = " ".join(str(question or "").split())
    # "Gender*Required We collect this…": the question ends at the form's marker.
    m = _STAR_REQUIRED_RE.search(t)
    if m and m.start() >= 3:
        t = t[:m.start()]
    for _ in range(4):
        before = t
        t = _LEAD_REQUIRED_RE.sub("", t)
        for tail in _TAILS:
            t = tail.sub("", t)
        m = _IS_REQUIRED_TAIL_RE.search(t)
        if m:
            head = t[:m.start()].rstrip()
            noun = m.group(1).strip().casefold()
            # Only the form's validation line: after the question's own end, a
            # star, or repeating the label ("cover letter a cover letter is required").
            if head and (head[-1] in "?*✱.:" or "*" in m.group(0) or "✱" in m.group(0)
                         or head.casefold().endswith(noun)):
                t = head
        t = t.strip()
        if t == before:
            break
    if t and t == t.lower():
        t = t[0].upper() + t[1:]                    # a field's lower-cased signature
    return t or " ".join(str(question or "").split())


_WHERE = {
    "work_authorization": "Answered from the work authorization status in your profile.",
    "self_identification": "Set once under Self-identification in your profile.",
    "applicant_fact": "Set your \"How did you hear\" answer in your profile.",
    "agreement": "An agreement is yours to accept on the form itself.",
    "never_stored": "SpotApply does not store this. Enter it on the form.",
    "job_specific": "The answer depends on the job, so it is asked on each form.",
    "profile": "This comes from your profile. Check it is filled in there; this form's field did not take it.",
    "document": "SpotApply writes this for each job when you tailor your resume.",
    "pick_many": "Tick every choice that applies on the form itself.",
}


def is_job_relative(question: Optional[str]) -> bool:
    return bool(_JOB_RELATIVE_RE.search(str(question or "")))


def classify(question: Optional[str], kind: str = "text") -> tuple[bool, str]:
    """``(saveable, why)``: may the person's answer be saved for reuse, and if
    not, which of ``_WHERE`` explains it."""
    q = display(question)
    # An acceptance first: a consent's legal text names what it authorizes, and
    # must not be read as the question that word usually belongs to.
    if kind == "checkbox" or _ACCEPTANCE_RE.search(q):
        return False, "agreement"
    why = field_guards.user_only_reason(q)
    if why:
        return False, why
    if _NEVER_STORED_RE.search(q):
        return False, "never_stored"
    if kind == "multi":
        return False, "pick_many"                 # several boxes may apply: no saved answer fills those yet
    if _PROFILE_FIELD_RE.match(q):
        return False, "profile"
    if kind in ("text", "textarea") and _DOCUMENT_RE.search(q) and len(q.split()) <= 6:
        return False, "document"
    if is_job_relative(q):
        return False, "job_specific"
    return True, ""


def clean_report(asked: Iterable[Any]) -> list[dict]:
    """The extension's report, bounded and de-duplicated by key."""
    out, seen = [], set()
    for raw in asked or []:
        if not isinstance(raw, dict):
            continue
        question = " ".join(str(raw.get("question") or "").split())[:MAX_QUESTION_CHARS]
        key = " ".join(str(raw.get("key") or "").split())[:MAX_QUESTION_CHARS]
        if len(question) < 3 or not key or key.casefold() in seen:
            continue
        if field_guards.not_a_question_reason(question):
            continue                              # a captcha, a field's NAME: not a question for anyone
        if len(re.findall(r"\bis required\b", question, re.I)) >= 2 or len(re.findall(r"[*✱]", question)) >= 2:
            continue                              # several fields read as one (a repeating section): not a question
        seen.add(key.casefold())
        kind = str(raw.get("kind") or "text").lower()
        options = raw.get("options")
        options = [" ".join(str(o).split())[:160] for o in options if str(o or "").strip()][:30] \
            if isinstance(options, list) else []
        out.append({"key": key, "question": question, "kind": kind if kind in KINDS else "text",
                    "options": options})
        if len(out) >= MAX_PER_REPORT:
            break
    return out


def record(application_id: int, asked: Iterable[Any]) -> int:
    """Replace what this application's form still needs with the latest report.
    A question no longer reported was answered on the form: its open row goes.
    Answered rows (history) and the server-side agent's rows are left alone."""
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import PendingQuestion
    questions = clean_report(asked)
    with get_session() as session:
        rows = session.exec(select(PendingQuestion).where(
            PendingQuestion.application_id == application_id,
            PendingQuestion.field_type.like(f"{_PREFIX}%"))).all()
        by_key = {(r.field_selector or "").casefold(): r for r in rows}
        reported = set()
        for q in questions:
            k = q["key"].casefold()
            reported.add(k)
            row = by_key.get(k)
            if row is None:
                session.add(PendingQuestion(
                    application_id=application_id, field_label=q["question"], field_selector=q["key"],
                    field_type=_PREFIX + q["kind"], options=json.dumps(q["options"]) if q["options"] else None))
            elif row.answered_at is None:
                row.field_label, row.field_type = q["question"], _PREFIX + q["kind"]
                if q["options"]:
                    row.options = json.dumps(q["options"])
                row.asked_at = datetime.utcnow()
                session.add(row)
        for k, row in by_key.items():
            if row.answered_at is None and k not in reported:
                session.delete(row)
        session.commit()
    return len(questions)


def _options(raw: Optional[str]) -> list:
    try:
        val = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    return [str(o) for o in val if str(o or "").strip()] if isinstance(val, list) else []


def _open_rows(session, user_id: Optional[str], key: Optional[str] = None):
    """The person's open questions with the employer that asked: newest first.
    ``key`` narrows it to one question IN SQL (a click on one line read up to
    600 rows and their choices to find it). ``user_id`` None is the one local
    user; in multi-tenant mode it reads nothing rather than everyone's."""
    from sqlalchemy import func, or_
    from sqlmodel import select

    from app.config import settings
    from app.db.models import Application, Job, PendingQuestion
    if user_id is None and settings.use_supabase:
        return []
    stmt = (select(PendingQuestion, Job.company)
            .join(Application, Application.id == PendingQuestion.application_id)
            .join(Job, Job.id == Application.job_id)
            .where(PendingQuestion.field_type.like(f"{_PREFIX}%"),
                   PendingQuestion.answered_at.is_(None),
                   PendingQuestion.asked_at >= datetime.utcnow() - timedelta(days=LIST_WINDOW_DAYS))
            .order_by(PendingQuestion.asked_at.desc()).limit(600))
    if user_id is not None:
        stmt = stmt.where(Application.user_id == user_id)
    if key is not None:
        stmt = stmt.where(or_(PendingQuestion.field_selector == key,
                              func.lower(PendingQuestion.field_selector) == key.lower()))
    return session.exec(stmt).all()


def listing(user_id: Optional[str]) -> dict:
    """One line per distinct question: how many forms asked it, who, its
    choices, and whether an answer may be saved for reuse."""
    from app.db.init_db import get_session
    groups: dict = {}
    with get_session() as session:
        for row, company in _open_rows(session, user_id):
            k = (row.field_selector or "").casefold()
            g = groups.get(k)
            if g is None:
                kind = (row.field_type or "")[len(_PREFIX):] or "text"
                saveable, why = classify(row.field_label, kind)
                g = groups[k] = {"key": row.field_selector, "question": display(row.field_label), "kind": kind,
                                 "options": _options(row.options), "forms": set(), "companies": [],
                                 "last_asked": row.asked_at.isoformat() if row.asked_at else None,
                                 "saveable": saveable, "why": why, "where": _WHERE.get(why, "")}
            g["forms"].add(row.application_id)
            if not g["options"]:
                g["options"] = _options(row.options)
            if company and company not in g["companies"] and len(g["companies"]) < 3:
                g["companies"].append(company)
    items = []
    for g in groups.values():
        g["forms"] = len(g["forms"])
        items.append(g)
    items.sort(key=lambda g: (not g["saveable"], -g["forms"], g["question"].casefold()))
    return {"questions": items[:120], "to_answer": sum(1 for g in items if g["saveable"]),
            "total": len(items)}


def answer(user_id: Optional[str], key: str, value: str) -> dict:
    """Save the person's answer to one listed question as their own, for every
    later form that asks it. ``{"ok": False, "reason": ...}`` when it may not
    be saved or does not fit the question's choices."""
    from app.autofill.answer_pack import _save_memory
    from app.db.init_db import get_session
    key = " ".join(str(key or "").split())
    key_cf = key.casefold()
    value = " ".join(str(value or "").split())[:MAX_ANSWER_CHARS]
    if not key_cf or not value:
        return {"ok": False, "reason": "empty"}
    with get_session() as session:
        mine = [row for row, _ in _open_rows(session, user_id, key)
                if (row.field_selector or "").casefold() == key_cf]
        if not mine:
            return {"ok": False, "reason": "not_found"}
        first = mine[0]
        kind = (first.field_type or "")[len(_PREFIX):] or "text"
        saveable, why = classify(first.field_label, kind)
        if not saveable:
            return {"ok": False, "reason": why}
        options = next((o for o in (_options(r.options) for r in mine) if o), [])
        if options:
            idx = form_answers.option_index(options, value)
            if idx < 0:
                return {"ok": False, "reason": "not_an_option"}
            value = options[idx]
        elif field_guards.is_model_only_reply(value):
            return {"ok": False, "reason": "not_an_answer"}
        label = first.field_selector
        now = datetime.utcnow()
        for row in mine:
            row.answer, row.answered_at = value, now
            session.add(row)
        session.commit()
    # The key is what the extension asks /api/recall-answers with, lower-cased
    # exactly as /api/save-answer stores it.
    _save_memory(label.lower().strip(), label, value, user_id=user_id)
    return {"ok": True, "answer": value, "forms": len(mine)}


def dismiss(user_id: Optional[str], key: str) -> dict:
    """Take a line off the list without answering it."""
    from app.db.init_db import get_session
    key = " ".join(str(key or "").split())
    key_cf = key.casefold()
    with get_session() as session:
        mine = [row for row, _ in _open_rows(session, user_id, key)
                if (row.field_selector or "").casefold() == key_cf] if key_cf else []
        now = datetime.utcnow()
        for row in mine:
            row.answered_at = now
            session.add(row)
        session.commit()
    return {"ok": True, "forms": len(mine)}
