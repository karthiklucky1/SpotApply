"""The outreach kit — a message the user can SEND in one click, and whether
anyone answered.

`referral.py` drafts the words (draft-only: the user sends from their own
account; nothing here connects to LinkedIn or a mailbox). This module turns a
draft into something actionable and then closes the loop:

  * a SUBJECT line for the email drafts (LinkedIn notes have none);
  * DIRECT LINKS: a LinkedIn people search for the recruiter / the reporting
    line at this company, and Gmail / Outlook / mail-app compose links with the
    subject and body already filled in — one click, paste nothing;
  * the people the POSTING itself names (hiring_context: recruiter, posting
    creator, reporting title, a contact email), each with its evidence quote,
    so a draft can be addressed to a real person when there is one;
  * REPLY TRACKING: `/api/sync-emails` already scans the inbox; `match_reply`
    says whether an email answers an outreach the user marked as sent
    (same subject under the Re:/Fwd: prefixes, the recipient's own address, or
    a person at the company's domain writing after the message went out).

Nothing here invents a name or an address: the LinkedIn links are SEARCHES
(the user picks the person), the mail links carry an address only when the
posting published one. Guard: `test_outreach`.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

#: Draft types that are emails; everything else is a LinkedIn note.
EMAIL_KINDS = frozenset({"hiring_manager", "recruiter_note", "cold_email"})

#: Measured reply-rate rules referral.py already writes to: a LinkedIn note
#: under 300 characters replies best; a connection request note is capped by
#: LinkedIn itself at 300 (200 for some accounts).
LINKEDIN_NOTE_LIMIT = 300

_REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|antw)\s*:\s*)+", re.I)
_NON_WORD_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")
_CORP_SUFFIX_RE = re.compile(
    r"\b(?:inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|sa|group|holdings|"
    r"technologies|technology|labs|software|systems|solutions)\b\.?", re.I)
_GENERIC_DOMAINS = frozenset({
    "gmail", "googlemail", "yahoo", "outlook", "hotmail", "live", "icloud", "me", "aol",
    "proton", "protonmail", "mail", "email", "msn", "ymail", "pm",
})
#: Mailboxes that are never a person answering.
_ROBOT_LOCAL_RE = re.compile(
    r"^(?:no-?reply|do-?not-?reply|noreply|notifications?|notification|mailer|alerts?|"
    r"jobs|careers|talent|recruiting|hr|info|support|news|newsletter|updates?|system|"
    r"donotreply|bounce|postmaster)[-_.+]?", re.I)


# ── subjects ─────────────────────────────────────────────────────────────────

def subject_for(kind: str, role: str, company: str) -> str:
    """A specific, un-salesy subject. Short enough to read whole on a phone."""
    role = (role or "the role").strip()
    company = (company or "your team").strip()
    if kind == "hiring_manager":
        return f"{role} at {company}: a quick note"
    if kind == "recruiter_note":
        return f"Applied for {role}: one question"
    return f"Quick question about the {role} role at {company}"


def subject_key(subject: str) -> str:
    """One spelling per thread: prefixes, case, punctuation and spacing gone.
    "Re: Quick question about the SRE role" and "RE: RE: quick question about
    the sre role" are the same conversation."""
    s = (subject or "").strip()
    for _ in range(6):
        stripped = _REPLY_PREFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = _NON_WORD_RE.sub(" ", s.lower())
    return _WS_RE.sub(" ", s).strip()[:160]


# ── direct links ─────────────────────────────────────────────────────────────

def linkedin_people_search(company: str, keywords: str = "") -> str:
    """A LinkedIn people SEARCH (the user picks the person) — never a guessed
    profile URL. Searching inside LinkedIn needs no API and breaks no terms."""
    q = " ".join(p for p in ((company or "").strip(), (keywords or "").strip()) if p)
    return ("https://www.linkedin.com/search/results/people/?keywords="
            + urllib.parse.quote(q) + "&origin=GLOBAL_SEARCH_HEADER")


def compose_links(to: str, subject: str, body: str) -> dict[str, str]:
    """Compose URLs with everything filled in. ``to`` may be empty: the user
    adds the address they found. Body is plain text; mail clients wrap it."""
    to = (to or "").strip()
    q = {"su": subject or "", "body": body or ""}
    if to:
        q["to"] = to
    gmail = "https://mail.google.com/mail/?view=cm&fs=1&" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
    oq = {"subject": subject or "", "body": body or ""}
    if to:
        oq["to"] = to
    outlook = "https://outlook.office.com/mail/deeplink/compose?" + urllib.parse.urlencode(oq, quote_via=urllib.parse.quote)
    mq = {"subject": subject or "", "body": body or ""}
    mailto = "mailto:" + urllib.parse.quote(to) + "?" + urllib.parse.urlencode(mq, quote_via=urllib.parse.quote)
    return {"gmail": gmail, "outlook": outlook, "mailto": mailto}


def channel_of(draft: dict) -> str:
    """'email' or 'linkedin' for a referral.py draft, from its type and channel text."""
    kind = (draft.get("type") or "").strip()
    if kind in EMAIL_KINDS:
        return "email"
    text = (draft.get("channel") or "").lower()
    if "linkedin" in text:
        return "linkedin"
    if "email" in text or "mail" in text:
        return "email"
    return "linkedin"


# ── who the posting names ────────────────────────────────────────────────────

@dataclass
class Person:
    label: str                    # "Recruiter", "Posting creator", "Reports to", "Contact"
    name: Optional[str]
    title: Optional[str]
    email: Optional[str]
    evidence: str                 # the posting's own words
    search_url: str               # LinkedIn people search for this person at the company

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "name": self.name, "title": self.title,
                "email": self.email, "evidence": self.evidence, "search_url": self.search_url}


def people_from_context(fields: dict, company: str) -> list[Person]:
    """The people a `hiring_context` payload established, with their evidence.
    A key with no value is not a person; a title with no name is still a lead
    ("Reports to: Director of Platform" is a search the user can run)."""
    out: list[Person] = []
    fields = fields or {}

    def val(key):
        f = fields.get(key) or {}
        return (f.get("value") or "").strip(), (f.get("quote") or "").strip()

    name, q = val("recruiter_name")
    if name:
        out.append(Person("Recruiter", name, None, None, q, linkedin_people_search(company, name)))
    name, q = val("reporting_manager_name")
    title, q2 = val("reporting_title")
    if name:
        out.append(Person("Reports to", name, title or None, None, q or q2,
                          linkedin_people_search(company, name)))
    elif title:
        out.append(Person("Reports to", None, title, None, q2, linkedin_people_search(company, title)))
    name, q = val("posting_creator_name")
    if name and all(p.name != name for p in out):
        out.append(Person("Posting creator", name, None, None, q, linkedin_people_search(company, name)))
    email, q = val("contact_email")
    if email and "@" in email:
        out.append(Person("Contact", None, None, email, q, linkedin_people_search(company, "recruiter")))
    return out


# ── the kit ──────────────────────────────────────────────────────────────────

def build_kit(drafts: Iterable[dict], *, company: str, role: str, job_url: str = "",
              context_fields: Optional[dict] = None, reporting_title: str = "") -> dict:
    """Decorate referral.py drafts with channel, subject, length and links, and
    add the searches that find the right people at this company."""
    company = (company or "").strip()
    role = (role or "").strip()
    out_drafts: list[dict] = []
    for d in drafts or ():
        d = dict(d)
        ch = channel_of(d)
        d["channel_kind"] = ch
        body = d.get("body") or ""
        d["chars"] = len(body)
        if ch == "email":
            subj = d.get("subject") or subject_for(d.get("type") or "", role, company)
            d["subject"] = subj
            contact = (d.get("suggested_contact") or {}).get("email") or ""
            d["links"] = compose_links(contact, subj, body)
        else:
            d["subject"] = None
            d["over_limit"] = len(body) > LINKEDIN_NOTE_LIMIT
            sc = d.get("suggested_contact") or {}
            d["links"] = {
                "profile": sc.get("profile_url") or "",
                "search": linkedin_people_search(
                    company, _search_terms_for(d.get("type") or "", role, reporting_title)),
            }
        out_drafts.append(d)
    people = people_from_context(context_fields or {}, company)
    searches = [
        {"label": f"Recruiters at {company or 'this company'}",
         "url": linkedin_people_search(company, "recruiter OR \"talent acquisition\"")},
        {"label": (f"{reporting_title} at {company}" if reporting_title
                   else f"Hiring managers for {role or 'this role'}"),
         "url": linkedin_people_search(company, reporting_title or _manager_terms(role))},
        {"label": f"People in similar roles at {company or 'this company'}",
         "url": linkedin_people_search(company, _role_terms(role))},
    ]
    return {
        "drafts": out_drafts,
        "people": [p.to_dict() for p in people],
        "searches": searches,
        "job_url": job_url or "",
        "linkedin_note_limit": LINKEDIN_NOTE_LIMIT,
    }


def _role_terms(role: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z+#.]+", role or "") if w]
    drop = {"senior", "sr", "jr", "junior", "staff", "principal", "lead", "ii", "iii", "iv",
            "remote", "hybrid", "contract", "the", "and", "of", "a", "an"}
    kept = [w for w in words if w.lower() not in drop][:4]
    return " ".join(kept)


def _manager_terms(role: str) -> str:
    base = _role_terms(role)
    if not base:
        return "engineering manager"
    first = base.split()[0]
    return f"{first} manager"


def _search_terms_for(kind: str, role: str, reporting_title: str) -> str:
    if kind == "hiring_manager":
        return reporting_title or _manager_terms(role)
    if kind in ("recruiter_note",):
        return "recruiter"
    if kind == "university_alumni":
        return ""
    return _role_terms(role)


# ── replies ──────────────────────────────────────────────────────────────────

def company_domain(addr: str) -> str:
    """'jane@mail.acme-corp.co.uk' → 'acme-corp'; a freemail address → ''."""
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return ""
    local, _, host = addr.rpartition("@")
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return ""
    # Drop the public suffix; a two-part ccTLD ("co.uk") takes two labels.
    core = labels[-2]
    if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "ac", "gov", "edu"} and len(labels[-1]) == 2:
        core = labels[-3]
    if core in _GENERIC_DOMAINS:
        return ""
    return core


def _norm_company(company: str) -> str:
    s = _CORP_SUFFIX_RE.sub(" ", (company or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def domain_matches_company(addr: str, company: str) -> bool:
    core = company_domain(addr).replace("-", "")
    comp = _norm_company(company)
    if len(core) < 4 or len(comp) < 4:
        return False
    return core in comp or comp in core


def is_robot_sender(addr: str) -> bool:
    local = (addr or "").split("@", 1)[0].lower()
    return bool(local) and bool(_ROBOT_LOCAL_RE.match(local))


def _parse_when(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).replace(tzinfo=None)
    except (TypeError, ValueError, IndexError):
        return None


def match_reply(email: dict, open_rows: Iterable[Any], *, company_of: dict,
                now: Optional[datetime] = None) -> Optional[tuple[Any, str]]:
    """Does this inbox email answer one of the user's SENT outreach messages?

    ``open_rows`` are OutreachMessage rows with ``sent_at`` set and no reply
    yet; ``company_of`` maps application_id → company name. Returns
    ``(row, how)`` for the first match, strongest evidence first:

      email_subject  the thread's subject is the one we sent (prefixes stripped)
      email_sender   the sender is the address the message went to
      email_domain   a PERSON at the company's domain wrote after we did

    An automated mailbox (noreply@, jobs@, notifications@) never counts as a
    reply, and nothing dated before the message was sent does either.
    """
    subject = str(email.get("subject") or "")
    sender = str(email.get("sender") or "").strip().lower()
    addr = sender if "@" in sender else ""
    when = _parse_when(email.get("date"))
    key = subject_key(subject)
    rows = list(open_rows)
    if addr and is_robot_sender(addr):
        return None

    def _after_sent(row) -> bool:
        sent = getattr(row, "sent_at", None)
        if sent is None or when is None:
            return True                    # no date to compare: the subject/sender decides
        return when >= sent.replace(tzinfo=None) if sent.tzinfo else when >= sent

    if key:
        for row in rows:
            if getattr(row, "subject_key", None) and row.subject_key == key and _after_sent(row):
                return row, "email_subject"
    if addr:
        for row in rows:
            rcpt = (getattr(row, "recipient_email", None) or "").strip().lower()
            if rcpt and rcpt == addr and _after_sent(row):
                return row, "email_sender"
        for row in rows:
            company = company_of.get(getattr(row, "application_id", None)) or ""
            if company and domain_matches_company(addr, company) and _after_sent(row):
                return row, "email_domain"
    return None
