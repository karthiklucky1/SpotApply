"""Staffing-vendor posting detection — who is actually hiring you.

Roughly 75% of the Fortune 1000 buy contingent labour through staffing vendors,
so a vendor posting is NOT a scam signal and this module never treats it as one.
What it is, is a materially different transaction than applying to the end
employer, and the candidate is the only party in the chain who cannot see its
shape. This module makes that shape visible.

The chain, top to bottom:

    end client -> MSP -> VMS (Fieldglass/Beeline/VNDLY/Coupa) -> prime vendor
    (holds the MSA, the only entity that can invoice) -> sub-vendor (no MSA)
    -> "implementation partner" -> you

Four to six hops is common, and each layer's economics depend on you not knowing
how many layers there are: a $110/hr bill rate lands at a $57-64/hr effective W2
rate in a four-layer chain (52-58%), versus 65-70% in a two-layer one.

Why this matters most to OUR users. Two facts from the research make vendor
postings a specifically international-candidate problem:

  * STEM OPT requires an E-Verify-enrolled employer, and USCIS states plainly
    that a staffing agency "would not be permitted to hire the student and send
    him or her to work for a customer or client at the client's place of
    business." The entity signing your Form I-983 must be the entity that
    actually trains you.
  * When a vendor lies, the asymmetry is total: in every documented enforcement
    case the owners faced prison and forfeiture while the WORKERS faced status
    loss and inadmissibility. INA 212(a)(6)(C)(i) is a permanent bar judged
    against the applicant, and "my employer told me to" is not a defence.

And 2026 is the year this got enforced: DOL "Project Firewall" (Sep 2025), the
ICE/HSI OPT operation naming ~10,000 F-1 students (May 2026), the SEVP broadcast
naming IT recruitment and staffing firms specifically (Mar 2026), and a DOL OIG
H-1B/PERM fraud investigation (Jul 2026).

So: detect the posting type, say what it is neutrally, and hand the user the
short list of things to establish BEFORE their resume goes anywhere — plus the
STEM OPT caution when their own profile says it applies.

Deliberately pure: no DB, no LLM, no network. Regex over text we already store.

See docs/research/hiring-machine-2026-08.md §1.7.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional

# ── Fingerprints ─────────────────────────────────────────────────────────────
# Grouped by what they prove. Each group contributes independently so that one
# noisy pattern can never on its own label a posting a vendor post.

# The strongest single tell: the employer will not name who you would work for.
_UNNAMED_CLIENT_RE = re.compile(
    r"\b(?:our|a|one of our|my)\s+(?:valued\s+|premier\s+|fortune\s*\d*\s*|large\s+|major\s+|leading\s+|top\s+)*"
    r"client(?:s)?\b"
    r"|\bclient\s+is\s+a\b"
    r"|\bseeking\s+candidates\s+for\b"
    r"|\bon\s+behalf\s+of\s+(?:our|a)\b"
    r"|\bend\s*[- ]?client\b",
    re.IGNORECASE,
)

# Contract-labour engagement vocabulary. C2C and "corp to corp" are the sharpest
# because they are meaningless outside this market.
_ENGAGEMENT_RE = re.compile(
    r"\bc2c\b|\bcorp[\s-]*to[\s-]*corp\b|\bw2\b|\bw-2\b|\b1099\b"
    r"|\bcontract[\s-]*to[\s-]*hire\b|\bc2h\b|\bright\s+to\s+represent\b|\brtr\b"
    r"|\bthird[\s-]party\s+(?:agencies|vendors|recruiters)\b"
    r"|\bimplementation\s+partner\b|\bprime\s+vendor\b|\bsub[\s-]?vendor\b",
    re.IGNORECASE,
)

# Hourly billing instead of a salary — the contingent-labour signature.
_HOURLY_RATE_RE = re.compile(
    r"\$\s*\d{1,3}(?:\.\d{1,2})?\s*(?:-|to|–)?\s*(?:\$?\s*\d{1,3}(?:\.\d{1,2})?\s*)?"
    r"(?:/|\s+per\s+)\s*(?:hr|hour)\b"
    r"|\bhourly\s+(?:rate|pay)\b|\brate\s*:\s*\$?\d",
    re.IGNORECASE,
)

# Visa status used as a filter IN THE POSTING. This is a body-shop hallmark and
# is also how hotlists are written (name + visa status, publicly indexed).
# Note: "no sponsorship" alone is NOT here — that is an ordinary, lawful
# statement by a direct employer and is handled by sponsorship.py.
_VISA_FILTER_RE = re.compile(
    r"\b(?:gc|green\s*card)\s*(?:/|\s+or\s+|,\s*)\s*(?:usc|us\s*citizen)\b"
    r"|\busc\s*(?:/|\s+or\s+|,\s*)\s*gc\b"
    r"|\bh1b\s*(?:transfer|ok|welcome|candidates)\b"
    r"|\bh4[\s-]?ead\b"
    r"|\bopt\s*/\s*cpt\b|\bcpt\s*/\s*opt\b"
    r"|\bvisa\s+status\s*:\s*",
    re.IGNORECASE,
)

# Company-name shapes common to staffing firms. Weak on its own — plenty of real
# product companies are "X Technologies" — so this only ever adds corroboration.
_AGENCY_NAME_RE = re.compile(
    r"\b(?:staffing|recruit(?:ing|ers|ment)|consultancy|consultants|talent|"
    r"resourcing|manpower|workforce|placements?|technologies|technology\s+"
    r"solutions|infotech|softtech|it\s+solutions|global\s+solutions|systems\s+inc)\b",
    re.IGNORECASE,
)

# ── Genuine red flags (guide's "walk away" / "verify hard" lists) ─────────────
# These are NOT vendor-detection; they are misconduct detection, and they apply
# to any employer. Kept separate so a normal vendor post is never called a scam.
_RED_FLAGS = (
    (
        "asks_for_money",
        re.compile(
            r"\b(?:processing|placement|marketing|sponsorship|training|registration|security)\s+"
            r"(?:fee|deposit|charge)\b"
            r"|\bfee\s+will\s+be\s+deducted\b|\brefundable\s+deposit\b"
            r"|\bpay\s+(?:a\s+)?(?:fee|deposit)\b",
            re.IGNORECASE,
        ),
        "Asks you for money. No legitimate employer or staffing firm ever charges a candidate — walk away.",
    ),
    (
        "documents_before_offer",
        re.compile(
            r"\b(?:send|share|provide|submit|attach)\b[^.\n]{0,60}"
            r"\b(?:i-?20|i20|passport|visa\s+stamp|i-?94|ssn|social\s+security\s+number|ead\s+copy)\b",
            re.IGNORECASE,
        ),
        "Asks for immigration documents up front. I-9 happens AFTER you accept an offer, and demanding "
        "specific documents based on citizenship status is unlawful under 8 U.S.C. 1324b.",
    ),
    (
        "blanket_rtr",
        re.compile(
            r"\bblanket\s+(?:rtr|right\s+to\s+represent)\b"
            r"|\bexclusive\s+representation\b"
            r"|\bright\s+to\s+represent\b[^.\n]{0,40}\b(?:all|multiple|any)\s+client",
            re.IGNORECASE,
        ),
        "Wants blanket or multi-client representation. A right-to-represent should name ONE client and "
        "ONE requisition — a blanket one can lock you out of working with anyone else.",
    ),
    (
        "chat_only_interview",
        re.compile(
            r"\binterview\s+(?:will\s+be\s+)?(?:conducted\s+)?(?:via|on|through)\s+"
            r"(?:whatsapp|telegram|google\s*chat|skype\s+chat|text)\b"
            r"|\bchat[\s-]only\s+interview\b",
            re.IGNORECASE,
        ),
        "Chat-only interview with no video is a 2026 BBB-flagged scam pattern.",
    ),
    (
        "resume_modification",
        re.compile(
            r"\b(?:we\s+(?:will|can)\s+)?(?:modify|adjust|enhance|update|tweak)\s+your\s+"
            r"(?:resume|experience|profile)\b"
            r"|\badd(?:ing)?\s+(?:a\s+few\s+)?years\s+(?:of\s+)?experience\b",
            re.IGNORECASE,
        ),
        "Offers to alter your resume. If you knowingly let experience be inflated, that is wilful "
        "misrepresentation — a permanent immigration bar that lands on YOU, not them.",
    ),
)

# What to establish before your resume is submitted — the guide's eight things,
# compressed to the ones that are actionable at the posting stage.
BEFORE_YOU_APPLY = (
    "Get the end client's actual name in writing — not 'a large bank'. Without it you cannot tell "
    "whether two recruiters are pitching you the same job.",
    "Ask directly: are you the prime vendor, or going through another vendor? How many layers are "
    "between you and the client?",
    "Get the pay rate in writing, and ideally the bill rate or the markup.",
    "Insist any right-to-represent names ONE client and ONE requisition. Refuse blanket agreements.",
    "Ask for a copy of exactly what was submitted, and that your resume goes as you wrote it.",
    "Send no I-20, passport, visa stamp, SSN or I-94 at the submission stage. That comes after an offer.",
    "Keep your own log: date, vendor, recruiter, client, requisition, rate. Nobody else in the chain "
    "tracks this for you, and it is your only defence against a duplicate submission killing your candidacy.",
)

# Shown only to F-1 users, because it is specific to their status.
STEM_OPT_CAUTION = (
    "You are on OPT. USCIS states a staffing agency \"would not be permitted to hire the student and "
    "send him or her to work for a customer or client at the client's place of business\" — the employer "
    "signing your Form I-983 must be the one that actually trains you, and STEM OPT also requires an "
    "E-Verify-enrolled employer. DHS named IT staffing and consulting firms specifically in its March 2026 "
    "STEM OPT fraud broadcast. Confirm who your legal employer is, and where you would physically work, "
    "before you apply. If in doubt, ask your DSO first — not the recruiter."
)


@dataclass
class VendorAssessment:
    """What kind of posting this is, and what to do about it.

    `is_vendor_posting` is descriptive, never a verdict: staffing is a legitimate
    industry. `red_flags` is the separate, harsher axis — those are misconduct.
    """

    is_vendor_posting: bool
    confidence: str                                   # 'low' | 'medium' | 'high'
    score: int                                        # 0-100, for ranking/tiebreak only
    signals: List[str] = field(default_factory=list)  # machine keys
    red_flags: List[str] = field(default_factory=list)
    red_flag_notes: List[str] = field(default_factory=list)
    label: str = ""                                   # short badge text ("" = show nothing)
    summary: str = ""
    checklist: List[str] = field(default_factory=list)
    work_auth_caution: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def _needs_stem_opt_caution(profile) -> bool:
    """True when the user's own profile says they are on F-1/OPT."""
    if profile is None:
        return False
    blob = " ".join(
        str(getattr(profile, f, "") or "")
        for f in ("work_authorization", "visa_status", "visa_type")
    ).lower()
    if getattr(profile, "stem_opt", False):
        return True
    return any(t in blob for t in ("opt", "f-1", "f1", "stem"))


def assess(job, profile=None) -> VendorAssessment:
    """Classify a posting as vendor-sourced or direct, and flag misconduct.

    Pure: reads only ``job.company``/``job.title``/``job.description`` and the
    profile's work-authorisation fields. Safe to call on a hot path.
    """
    company = str(getattr(job, "company", "") or "")
    title = str(getattr(job, "title", "") or "")
    desc = str(getattr(job, "description", "") or "")
    body = f"{title}\n{desc}"

    signals: List[str] = []
    score = 0

    # Strongest tell: they will not say who you would actually work for.
    if _UNNAMED_CLIENT_RE.search(body):
        score += 40
        signals.append("unnamed_client")

    if _ENGAGEMENT_RE.search(body):
        score += 30
        signals.append("contract_engagement_terms")

    if _HOURLY_RATE_RE.search(body):
        score += 15
        signals.append("hourly_rate")

    if _VISA_FILTER_RE.search(body):
        score += 20
        signals.append("visa_status_filter")

    # Name shape corroborates only — never enough on its own.
    if _AGENCY_NAME_RE.search(company):
        score += 10
        signals.append("agency_style_name")

    red_flags: List[str] = []
    red_flag_notes: List[str] = []
    for key, pattern, note in _RED_FLAGS:
        if pattern.search(body):
            red_flags.append(key)
            red_flag_notes.append(note)

    score = min(score, 100)

    # A single weak signal is not a vendor posting. Require either the unnamed
    # client, or two independent corroborating signals.
    is_vendor = "unnamed_client" in signals or len(signals) >= 2

    if not is_vendor:
        confidence = "low"
    elif score >= 60:
        confidence = "high"
    elif score >= 35:
        confidence = "medium"
    else:
        confidence = "low"

    label = ""
    summary = ""
    checklist: List[str] = []
    caution: Optional[str] = None

    if is_vendor:
        label = "Staffing vendor"
        summary = (
            "This looks like a staffing vendor's posting rather than the employer's own. That is normal "
            "and legitimate — most large companies hire contractors this way — but you are entering a "
            "chain, and each layer between you and the client takes a cut you cannot see. Establish the "
            "basics in writing before your resume goes anywhere."
        )
        checklist = list(BEFORE_YOU_APPLY)
        if _needs_stem_opt_caution(profile):
            caution = STEM_OPT_CAUTION

    if red_flags:
        label = "Red flags"
        summary = (
            "This posting contains language that the FBI, FTC and DHS all flag as predatory-recruiter "
            "behaviour. Do not send documents or money. Verify the company independently first."
        ) + (" " + summary if summary else "")
        if not checklist:
            checklist = list(BEFORE_YOU_APPLY)

    return VendorAssessment(
        is_vendor_posting=is_vendor,
        confidence=confidence,
        score=score,
        signals=signals,
        red_flags=red_flags,
        red_flag_notes=red_flag_notes,
        label=label,
        summary=summary,
        checklist=checklist,
        work_auth_caution=caution,
    )
