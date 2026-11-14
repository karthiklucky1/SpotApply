"""Auto-fill data: the answers application forms ask for that no resume holds.

ONE catalog (``SECTIONS``) drives three things, so they cannot drift:

* the profile page's "Auto-fill data" editor (``catalog_for_ui``),
* what ``PUT /api/profile`` will store (``clean``), and
* which saved answer belongs to which form question (``answer_for_label``).

A field either lives in an existing ``UserProfile`` column (``column=...``: the
editor shows it, the column stays the one place it is stored) or in the
``UserProfile.autofill_json`` object under its ``key``.

What is deliberately NOT here, and why (owner request 2026-11-14 listed them):

* a password for job boards: SpotApply never stores a password;
* legal agreements and signatures (arbitration, NDA, "I certify", consents,
  an electronic signature): each is an agreement with ONE employer, and
  agreeing from a saved default is agreeing to something the user never read;
* self-identification beyond gender / race / veteran / disability (those four
  keep their own columns and their own rule, ``eeo_confirmed``): the extension
  has no fill path for more, and special-category data is not collected to sit
  unused;
* references: other people's contact details, with no fill path yet;
* separate work-authorization answers: those questions have ONE interpreter
  (``work_auth.assess_profile``), fed by the status the user already gives.

``answer_for_label`` is strict on purpose. A blank field costs the user one
answer; a wrong one is typed into an employer's form. It never answers an
anti-bot field, a work-authorization, self-identification or "how did you
hear" question (``field_guards``), and ``drop_borrowable`` withholds an answer
the extension's own fuzzy matcher would copy into a neighbouring field.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional

from app.autofill import field_guards

# ── kinds ────────────────────────────────────────────────────────────────────
TEXT, TEXTAREA, SELECT, YESNO, DATE, NUMBER, URL, TEL = (
    "text", "textarea", "select", "yesno", "date", "number", "url", "tel")

_DEFAULT_MAX = {TEXT: 120, TEXTAREA: 1200, URL: 200, TEL: 40, NUMBER: 6}
YES_NO = ("Yes", "No")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = TEXT
    options: tuple = ()
    column: Optional[str] = None      # stored in this UserProfile column instead
    required: bool = False            # counts toward profile completion
    hint: str = ""
    placeholder: str = ""
    max_len: int = 0
    span: int = 1                     # grid columns the input takes (3 = full row)
    # Regexes, searched in the field's visible label, that say "this form
    # field asks for this value".
    ask: tuple = ()
    # Any part of the field's signature matching one of these: never this field.
    deny: tuple = ()
    # Longest label (characters) the ask patterns may match in. Short facts
    # ("City") only match short labels; a paragraph that mentions a city is a
    # different question.
    max_label: int = 160


@dataclass(frozen=True)
class ListSpec:
    key: str
    label: str
    item_fields: tuple                # Field tuples for one entry
    max_items: int = 10
    add_label: str = "Add"


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    blurb: str
    fields: tuple = ()
    lists: tuple = ()
    icon: str = "i-sliders"


_NOT_ADDRESS = (r"e-?mail", r"\bweb", r"\burl\b", r"\bip\b", r"school", r"universit",
                r"college", r"employer", r"company", r"reference", r"emergency")
_NOT_HOME = (r"school", r"universit", r"college", r"employer", r"company", r"\bjob\b",
             r"birth", r"reference", r"emergency", r"work location", r"office",
             r"prefer", r"willing", r"relocat", r"interested", r"desired")

LANGUAGE_LEVELS = ("Native or bilingual", "Fluent", "Professional", "Conversational", "Basic")
EDUCATION_LEVELS = ("High school or equivalent", "Associate degree", "Bachelor's degree",
                    "Master's degree", "Doctorate (PhD)", "Professional degree (MD, JD)",
                    "Vocational / trade certificate", "Some college, no degree")

SECTIONS: tuple = (
    Section("personal", "Personal information", "Your name as forms ask for it, and how you introduce yourself.",
            icon="i-star", fields=(
        Field("name_prefix", "Prefix / title", SELECT, ("Mr.", "Ms.", "Mrs.", "Mx.", "Dr.", "Prof."),
              ask=(r"^(name\s+)?prefix$", r"\bname prefix\b", r"\bsalutation\b", r"\bhonorific\b",
                   r"^prefix\s*/\s*title$", r"^title\s*/\s*prefix$"),
              deny=(r"phone", r"country", r"dial"), max_label=40),
        Field("first_name", "First name", column="first_name", required=True),
        Field("middle_name", "Middle name", max_len=60,
              ask=(r"\bmiddle\s*name\b", r"\bmiddle\s*initial\b"), max_label=60),
        Field("last_name", "Last name", column="last_name", required=True),
        Field("name_suffix", "Suffix", SELECT, ("Jr.", "Sr.", "II", "III", "IV"),
              ask=(r"^(name\s+)?suffix$", r"\bname suffix\b"), deny=(r"phone",), max_label=40),
        Field("preferred_name", "Preferred name", max_len=60, placeholder="What people call you",
              ask=(r"\bpreferred (first )?name\b", r"\bnickname\b", r"\bname you (go by|prefer)\b",
                   r"what should we call you"), max_label=80),
        Field("former_names", "Former / other names used", max_len=120, span=2,
              ask=(r"\b(former|previous|maiden|other) (legal )?names?\b", r"\bnames? previously used\b",
                   r"\bother names? (you have )?used\b"), max_label=100),
        Field("name_pronunciation", "Name pronunciation", max_len=80, placeholder="e.g. KAR-thik",
              ask=(r"\bpronunciation\b", r"\bpronounce your name\b", r"\bphonetic\b"), max_label=100),
        Field("current_title", "Headline / current title", column="current_title", required=True,
              placeholder="e.g. Machine Learning Engineer", span=3),
        Field("professional_summary", "About", TEXTAREA, column="professional_summary", required=True,
              hint="Shown on your profile and used when a form asks you to describe yourself.",
              placeholder="A short summary of your background, strengths and what you want next.", span=3),
    )),
    Section("contact", "Contact", "How employers reach you.", icon="i-mail", fields=(
        Field("email", "Email", column="email", required=True),
        Field("phone", "Phone", TEL, column="phone", required=True),
        Field("phone_extension", "Phone extension", max_len=10, placeholder="Optional",
              ask=(r"\bphone ext", r"^ext(ension)?\.?$", r"\bext(ension)?\.?$"),
              deny=(r"browser", r"chrome", r"file"), max_label=40),
        Field("phone_type", "Phone type", SELECT, ("Mobile", "Home", "Work"),
              ask=(r"\bphone (device )?type\b", r"\btype of phone\b", r"\bphone number type\b"),
              max_label=50),
        Field("secondary_phone", "Secondary phone", TEL,
              ask=(r"\b(secondary|alternate|alternative|additional|other) (phone|number|telephone)\b",
                   r"\bsecond phone\b"), deny=(r"reference", r"emergency"), max_label=60),
        Field("preferred_contact_method", "Preferred contact method", SELECT, ("Email", "Phone", "Text message"),
              ask=(r"\b(preferred|best) (method|way|means|mode) (of|to) (contact|communication|reach)",
                   r"\bpreferred contact( method)?\b", r"\bcontact preference\b",
                   r"how (would|do|should) (you|we) (prefer|like)[^|]*\bcontact"), max_label=120),
        Field("sms_ok", "OK to receive text messages from employers?", YESNO,
              # A question, not the consent boilerplate printed under a phone box.
              ask=(r"\b(do you|would you|may we|can we)\b[^|]*\b(text|sms)\b",
                   r"\bok(ay)? to (receive )?(text|sms)", r"\bopt[- ]?in\b[^|]*\b(text|sms)\b",
                   r"\breceive (text|sms) messages\?"), max_label=140),
        Field("time_zone", "Time zone", SELECT, (
            "Eastern Time (ET)", "Central Time (CT)", "Mountain Time (MT)", "Pacific Time (PT)",
            "Alaska Time (AKT)", "Hawaii Time (HT)", "UTC / GMT", "Central European Time (CET)",
            "India Standard Time (IST)", "Other"),
              ask=(r"\btime ?zone\b",), max_label=60),
    )),
    Section("address", "Address", "Where you live. Your city and state also decide which jobs count as local.",
            icon="i-building", fields=(
        Field("address_line1", "Street address", max_len=120, span=2,
              ask=(r"^(home |mailing |street |current |permanent )?address$",
                   r"\bstreet address\b", r"\baddress( line)? ?(1|one)\b", r"\bmailing address\b"),
              deny=_NOT_ADDRESS + (r"\b(2|two)\b", r"\bcity\b", r"\bzip\b", r"\bpostal\b"), max_label=50),
        Field("address_line2", "Apt / unit / suite", max_len=60,
              ask=(r"\baddress( line)? ?(2|two)\b", r"\b(apt|apartment|suite|unit)\b"),
              deny=_NOT_ADDRESS + (r"business unit",), max_label=50),
        Field("city", "City", max_len=60, required=True,
              ask=(r"\bcity\b", r"\btown\b"),
              deny=_NOT_HOME + (r"state", r"province", r"\bzip\b", r"postal", r"country", r"ethnic"),
              max_label=30),
        Field("state", "State / province", max_len=60, required=True, placeholder="e.g. Ohio",
              ask=(r"^state$", r"^province$", r"^state\s*/\s*province$", r"^state or province$",
                   r"^state\s*/\s*region$", r"^(home |current )?state( of residence)?$", r"^region$",
                   r"^state\s*/\s*province\s*/\s*region$"),
              deny=_NOT_HOME + (r"city", r"\bzip\b", r"postal", r"country", r"united"), max_label=36),
        Field("county", "County", max_len=60,
              ask=(r"^county( of residence)?$",), deny=(r"country", r"court"), max_label=30),
        Field("postal_code", "ZIP / postal code", max_len=12, required=True,
              ask=(r"\b(zip|postal|post) ?code\b", r"^zip$", r"^postcode$", r"^postal$"),
              deny=_NOT_HOME, max_label=40),
        Field("country", "Country", max_len=60, required=True, placeholder="e.g. United States",
              ask=(r"^country$", r"^country\s*/\s*region$", r"^country of residence$",
                   r"\bcountry (where|in which) you (currently )?(live|reside)\b",
                   r"^(home |current )country$"),
              deny=_NOT_HOME + (r"citizen", r"phone", r"\bcode\b", r"authori", r"national"),
              max_label=60),
    )),
    Section("online", "Online presence", "Links forms ask for.", icon="i-link", fields=(
        Field("linkedin_url", "LinkedIn profile URL", URL, column="linkedin_url", required=True),
        Field("github_url", "GitHub URL", URL, column="github_url"),
        Field("portfolio_url", "Website / portfolio", URL, column="portfolio_url"),
        Field("twitter_url", "X / Twitter", URL, placeholder="https://x.com/you",
              ask=(r"\btwitter\b", r"\bx\.com\b", r"^x( profile| url| handle| account)$",
                   r"^x\s*/\s*twitter", r"\bx \(formerly twitter\)"), max_label=60),
    )),
    Section("work", "Work experience", "The jobs themselves are edited on your profile. These are the questions asked about them.",
            icon="i-briefcase", fields=(
        Field("years_experience", "Years of experience", NUMBER, column="years_experience"),
        Field("currently_employed", "Currently employed?", YESNO,
              ask=(r"\b(are you )?currently employed\b", r"\bpresently employed\b",
                   r"\bcurrent employment status\b"), deny=(r"\bby\b", r"\bhere\b", r"relative"), max_label=120),
        Field("reason_for_leaving", "Reason for leaving current / last job", max_len=200, span=2,
              # Only the CURRENT / LAST job: a bare "Reason for leaving" repeats
              # under every job in a work history, and this is one answer.
              ask=(r"\breasons? for leaving (your )?(current|last|most recent|present)\b",
                   r"\bwhy (are|did) you (leaving|leave) your (current|last|most recent|present)\b",
                   r"\bwhy are you looking (for|to)\b")),
        Field("key_skills", "Top skills (comma-separated)", column="key_skills", span=3, max_len=1200,
              hint="Also edited as tags on your profile."),
        Field("proud_project", "A project you are proud of", TEXTAREA, span=3,
              placeholder="What it was, what you did, what changed because of it.",
              ask=(r"\bproject\b[^|]*\bproud\b", r"\bproud(est)?\b[^|]*\bproject\b",
                   r"\bproudest (accomplishment|achievement)\b")),
    )),
    Section("education", "Education", "Schools are edited on your profile. These are the questions asked about them.",
            icon="i-scan", fields=(
        Field("highest_education", "Highest level of education", SELECT, EDUCATION_LEVELS, required=True,
              ask=(r"\bhighest (level of )?(education|degree|qualification)\b",
                   r"\b(level of education|education level)\b"), max_label=120),
        Field("currently_enrolled", "Currently enrolled in school / university?", YESNO,
              ask=(r"\bcurrently (enrolled|attending|studying)\b", r"\bare you (a )?(current|full[- ]time) student\b",
                   r"\bcurrently a student\b"), max_label=140),
    )),
    Section("abilities", "Languages & abilities", "Languages you can work in, and a driver's license.",
            icon="i-message",
            fields=(
        Field("drivers_license", "Valid driver's license?", YESNO,
              ask=(r"\bdriver'?s?'? ?licen[cs]e\b", r"\bdriving licen[cs]e\b"),
              deny=(r"number", r"\bstate\b", r"expir", r"upload", r"class"), max_label=140),
            ),
            lists=(
        ListSpec("languages", "Languages", add_label="Add language", max_items=8, item_fields=(
            Field("language", "Language", max_len=40, required=True, placeholder="e.g. English"),
            Field("proficiency", "Proficiency", SELECT, LANGUAGE_LEVELS, required=True),
            Field("reading", "Reading", SELECT, LANGUAGE_LEVELS),
            Field("writing", "Writing", SELECT, LANGUAGE_LEVELS),
            Field("speaking", "Speaking", SELECT, LANGUAGE_LEVELS),
        )),
            )),
    Section("authorization", "Work authorization", "Decides which jobs you see and how authorization questions are answered.",
            icon="i-check-circle", fields=(
        Field("citizenship_country", "Country of citizenship", max_len=60,
              ask=(r"\bcountry of citizenship\b", r"\bcountry of nationality\b", r"^nationality$",
                   r"^citizenship$", r"\bwhat is your (citizenship|nationality)\b"),
              deny=(r"status", r"dual", r"second"), max_label=80),
        Field("security_clearance", "Security clearance level", SELECT,
              ("None", "Public Trust", "Confidential", "Secret", "Top Secret", "Top Secret / SCI"),
              ask=(r"^(security )?clearance$", r"\bclearance (level|type|held)\b",
                   r"\blevel of (security )?clearance\b",
                   r"\b(current|active|highest) (security )?clearance\b",
                   r"\bdo you (currently )?(have|hold|possess)[^|]*\bclearance\b"),
              deny=(r"obtain", r"eligible", r"willing")),
        Field("clearance_eligible", "Eligible to obtain a security clearance?", YESNO,
              ask=(r"\b(eligible|able|ability|willing)\b[^|]*\bobtain\b[^|]*\bclearance\b",
                   r"\bobtain (and maintain )?(a |an )?(u\.?s\.? )?(government )?(security )?clearance\b")),
    )),
    Section("availability", "Availability", "When you can start and what hours you can work.",
            icon="i-bell", fields=(
        Field("earliest_start_date", "Earliest start date", DATE,
              # Never a bare "Start date": that is the date a job or a degree began.
              ask=(r"\b(earliest|available|possible|desired|preferred|anticipated|expected) start\b",
                   r"\bwhen (can|could|would) you (be able to )?start\b",
                   r"\bdate (you are |you're )?available\b"),
              deny=(r"school", r"employment start", r"job start", r"position start"),
              max_label=120),
        Field("shifts_available", "Shifts available", SELECT,
              ("Any shift", "Day", "Evening", "Night", "Rotating", "Weekends only"),
              ask=(r"\b(available|preferred|desired) shifts?\b", r"\bshifts? (are you |you are )?(available|able|willing)",
                   r"\bwhich shifts?\b", r"\bshift (availability|preference)\b"), max_label=120),
        Field("hours_per_week", "Hours per week available", NUMBER,
              ask=(r"\bhours (per|a|each) week\b", r"\bweekly hours\b", r"\bhours\b[^|]*\bweek(ly)?\b"),
              deny=(r"currently work", r"did you work", r"worked"), max_label=120),
        Field("weekends_holidays", "Able to work weekends and holidays?", YESNO,
              ask=(r"\bwork (on )?(weekends|holidays)\b", r"\bweekends? (and|or|/) holidays?\b",
                   r"\bavailable (on )?weekends\b")),
        Field("overtime", "Willing to work overtime?", YESNO, ask=(r"\bovertime\b",),
              deny=(r"exempt", r"paid", r"eligible for")),
        Field("unavailable_times", "Days / shifts / hours you are unavailable", max_len=200, span=2,
              placeholder="Leave empty if none",
              ask=(r"\b(not available|unavailable) (to|for) work\b", r"\bdays?[^|]*(cannot|can't|unable to) work\b",
                   r"\bscheduling (conflicts|restrictions)\b", r"\bhours you are (not available|unavailable)\b")),
    )),
    Section("compensation", "Compensation", "What you expect to be paid.", icon="i-gem", fields=(
        Field("salary_min", "Desired salary (minimum)", NUMBER, column="salary_min", max_len=9),
        Field("salary_max", "Desired salary (maximum)", NUMBER, column="salary_max", max_len=9),
        Field("salary_currency", "Currency", SELECT, ("USD", "CAD", "EUR", "GBP", "INR", "AUD", "SGD"),
              column="salary_currency"),
        Field("pay_period", "Pay period", SELECT, ("Yearly", "Monthly", "Weekly", "Hourly"),
              ask=(r"\bpay (period|frequency)\b", r"\bsalary (period|basis|type)\b", r"\bpay rate type\b"),
              max_label=60),
    )),
    Section("preferences", "Job preferences", "What you are looking for, as forms ask it.",
            icon="i-target", fields=(
        Field("employment_type_sought", "Employment type sought", SELECT,
              ("Full-time", "Part-time", "Contract", "Temporary", "Internship"),
              ask=(r"\b(desired|preferred) (employment|job) type\b", r"\btype of employment\b[^|]*(seek|desir|look|interest|prefer)",
                   r"\bemployment type (sought|desired|preferred)\b", r"\bfull[- ]time or part[- ]time\b"),
              max_label=120),
        Field("ideal_job_title", "My ideal job title", max_len=80,
              ask=(r"\bideal (job|role|position)\b", r"\bdesired (job )?title\b", r"\bdream (job|role)\b"),
              max_label=100),
        Field("preferred_work_arrangement", "Preferred work arrangement", SELECT,
              ("Remote", "Hybrid", "On-site", "No preference"),
              ask=(r"\b(preferred|desired|ideal) work (arrangement|environment|model|setting|style)\b",
                   r"\bwork arrangement preference\b", r"\bprefer (to work )?(remote|on-?site|hybrid)"),
              max_label=140),
        Field("reliable_transportation", "Reliable transportation?", YESNO,
              ask=(r"\breliable (means of |form of )?transportation\b",)),
        Field("commute_distance", "How far will you travel to work each day?", max_len=40,
              placeholder="e.g. 25 miles",
              ask=(r"\bhow far\b[^|]*\b(travel|commute)\b", r"\bcommut(e|ing) (distance|radius)\b",
                   r"\bmaximum commute\b")),
        Field("willing_to_travel", "Willing to travel?", YESNO,
              ask=(r"\b(willing|able|open|comfortable) to travel\b", r"\bwillingness to travel\b"),
              deny=(r"how much", r"percent", r"%", r"how far", r"each day", r"commute")),
        Field("travel_percentage", "How much travel are you willing to do?", SELECT,
              ("None", "Up to 10%", "Up to 25%", "Up to 50%", "Up to 75%", "Up to 100%"),
              ask=(r"\b(how much|percentage of|percent of|amount of) travel\b",
                   r"\btravel\b[^|]*(percent|%)", r"\btravel (percentage|amount)\b"),
              # "Are you willing to travel up to 25%?" wants Yes or No, not a range.
              deny=(r"^(are|do|would|can|will) you\b",)),
        Field("relocation_assistance", "Relocation assistance needed?", YESNO,
              ask=(r"\brelocation (assistance|support|package|help|benefits?)\b",)),
    )),
    Section("heard", "How you heard about us", "Used only when a form asks how you found the job.",
            icon="i-search", fields=(
        Field("referred_by_agency", "Referred by a recruiting agency?", YESNO,
              ask=(r"\b(referred|submitted|represented|sent) by (a |an )?(recruit\w+|staffing|employment|third[- ]party) (agency|firm)\b",
                   r"\b(recruiting|recruitment|staffing) agency\b[^|]*\b(refer|represent|submit|working with)")),
        Field("referring_employee", "Referring employee name", max_len=80,
              ask=(r"\b(referring employee|employee referral)( name)?\b", r"\bname of (the )?(employee|person) who referred\b",
                   r"\bwho referred you\b", r"\breferr(al|er)('s)? name\b", r"\breferred by\b"),
              deny=(r"agency", r"firm", r"were you", r"have you been", r"are you"), max_label=120),
    )),
    Section("screening", "Screening questions", "Yes / No questions many forms repeat. Empty means SpotApply leaves the question for you.",
            icon="i-check-circle", fields=(
        Field("over_18", "18 years or older?", YESNO,
              ask=(r"\b(18|eighteen)\b[^|]*\b(years|age|older|over)\b", r"\bat least (18|eighteen)\b",
                   r"\bover (the age of )?(18|eighteen)\b", r"\b(older|over|above)[^|]*\b(18|eighteen)\b"),
              deny=(r"\b21\b", r"under (the age of )?18", r"younger")),
        Field("over_21", "21 years or older?", YESNO,
              ask=(r"\b(21|twenty[- ]one)\b[^|]*\b(years|age|older|over)\b", r"\bat least (21|twenty[- ]one)\b",
                   r"\bover (the age of )?(21|twenty[- ]one)\b", r"\b(older|over|above)[^|]*\b21\b"),
              deny=(r"\b18\b", r"eighteen", r"under (the age of )?21", r"younger")),
        Field("essential_functions", "Able to perform the essential functions of the job?", YESNO,
              ask=(r"\bessential (job )?(functions|duties)\b",)),
        Field("drug_test", "Consent to a drug test?", YESNO,
              ask=(r"\bdrug (test|screen)",)),
        Field("felony_conviction", "Any felony conviction?", YESNO,
              # A felony, nothing broader: "a criminal record" or "any crime"
              # also covers a misdemeanor, which this answer says nothing about.
              ask=(r"\bfelon(y|ies)\b",), deny=(r"misdemeanou?r", r"\bcrime\b", r"\boffen[cs]e")),
        Field("dismissed", "Ever dismissed or asked to resign?", YESNO,
              ask=(r"\b(terminated|dismissed|discharged|fired)\b[^|]*\b(employ|job|position|resign|cause)",
                   r"\b(asked|forced|requested) to resign\b", r"\binvoluntarily (terminated|separated)\b")),
        Field("non_compete", "Bound by a non-compete or non-solicitation agreement?", YESNO, required=True,
              ask=(r"\bnon-?compet", r"\bnon-?solicit", r"\brestrictive covenants?\b")),
        Field("bankruptcy", "Ever declared bankruptcy?", YESNO, ask=(r"\bbankrupt",)),
        Field("ccj", "Any County Court Judgments (UK)?", YESNO,
              ask=(r"\bcounty court judge?ments?\b", r"\bccjs?\b")),
        Field("anything_else", "Anything else to share", TEXTAREA, span=3, max_len=800,
              placeholder="Typed when a form asks if there is anything else you want to add.",
              ask=(r"\banything else\b[^|]*\b(share|know|add|tell|like)", r"\bis there anything (else|more)\b")),
    )),
)

#: Lists kept in the same JSON object that are shown on the profile page itself.
PROFILE_LISTS: tuple = (
    ListSpec("certifications", "Certifications", add_label="Add certification", max_items=15, item_fields=(
        Field("name", "Certification", max_len=100, required=True),
        Field("issuer", "Issued by", max_len=80),
        Field("year", "Year", NUMBER, max_len=4),
    )),
    ListSpec("projects", "Projects", add_label="Add project", max_items=10, item_fields=(
        Field("title", "Project title", max_len=120, required=True),
        Field("description", "Description", TEXTAREA, max_len=4000),
    )),
)


def _all_fields() -> Iterable[Field]:
    for s in SECTIONS:
        yield from s.fields


def _all_lists() -> Iterable[ListSpec]:
    for s in SECTIONS:
        yield from s.lists
    yield from PROFILE_LISTS


FIELDS: dict = {f.key: f for f in _all_fields()}
JSON_FIELDS: dict = {k: f for k, f in FIELDS.items() if not f.column}
COLUMN_FIELDS: dict = {k: f for k, f in FIELDS.items() if f.column}
LISTS: dict = {l.key: l for l in _all_lists()}


# ── validation ───────────────────────────────────────────────────────────────

def _one_line(v: Any) -> str:
    return " ".join(str(v if v is not None else "").split())


def _clean_scalar(f: Field, v: Any) -> Optional[str]:
    """The stored form of ``v`` for field ``f``; "" clears it; None = refuse
    (not a value this field can hold, so whatever is stored stays)."""
    if v is None:
        return None
    if isinstance(v, bool):
        v = "Yes" if v else "No"
    if isinstance(v, (dict, list, tuple, set)):
        return None
    limit = f.max_len or _DEFAULT_MAX.get(f.kind, 120)
    if f.kind == TEXTAREA:
        s = "\n".join(line.rstrip() for line in str(v).replace("\r\n", "\n").split("\n")).strip()
        return s[:limit]
    s = _one_line(v)
    if not s:
        return ""
    if f.kind in (SELECT, YESNO):
        opts = f.options or YES_NO
        for o in opts:
            if o.lower() == s.lower():
                return o
        return None
    if f.kind == DATE:
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        return d.isoformat() if 1950 <= d.year <= date.today().year + 10 else None
    if f.kind == NUMBER:
        digits = re.sub(r"[,\s]", "", s)
        return digits[:limit] if re.fullmatch(r"\d+", digits) else None
    if f.kind == URL:
        return s[:limit] if " " not in s else None
    return s[:limit]


def _clean_list(spec: ListSpec, v: Any) -> Optional[list]:
    if not isinstance(v, list):
        return None
    out = []
    for item in v:
        if not isinstance(item, dict):
            continue
        row = {}
        for f in spec.item_fields:
            c = _clean_scalar(f, item.get(f.key))
            if c:
                row[f.key] = c
        # An entry needs its name (the first field): a row of nothing but a
        # proficiency is not an entry, but "Hindi" with no level stated is one
        # (a resume often lists languages without saying how well).
        if row and row.get(spec.item_fields[0].key):
            out.append(row)
        if len(out) >= spec.max_items:
            break
    return out


def clean(payload: Any) -> dict:
    """The part of a submitted ``autofill`` object that may be stored.

    Unknown keys, column-backed keys (they have their own column) and values a
    field cannot hold are dropped. "" stays: it means "clear this answer"."""
    if not isinstance(payload, dict):
        return {}
    out: dict = {}
    for key, v in payload.items():
        if key in JSON_FIELDS:
            c = _clean_scalar(JSON_FIELDS[key], v)
            if c is not None:
                out[key] = c
        elif key in LISTS:
            c = _clean_list(LISTS[key], v)
            if c is not None:
                out[key] = c
    return out


def merge(stored: Any, update: Any) -> dict:
    """``stored`` with a cleaned ``update`` laid over it. Keys the update does
    not send are kept (a client that edits three answers must not wipe the
    rest); a "" or an empty list removes the key."""
    base = load(stored)
    for key, v in clean(update).items():
        if v == "" or v == []:
            base.pop(key, None)
        else:
            base[key] = v
    return base


def load(raw: Any) -> dict:
    """The stored object, cleaned again on the way out (a row written by an
    older catalog never hands out a value today's catalog would refuse)."""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    else:
        return {}
    return {k: v for k, v in clean(data).items() if v not in ("", [])}


def dumps(data: dict) -> Optional[str]:
    return json.dumps(data, sort_keys=True) if data else None


# ── the editor's description ─────────────────────────────────────────────────

def _field_ui(f: Field) -> dict:
    d = {"key": f.key, "label": f.label, "kind": f.kind}
    if f.kind == YESNO:
        d["options"] = list(YES_NO)
    elif f.options:
        d["options"] = list(f.options)
    for attr in ("column", "hint", "placeholder"):
        if getattr(f, attr):
            d[attr] = getattr(f, attr)
    if f.required:
        d["required"] = True
    if f.span != 1:
        d["span"] = f.span
    d["max"] = f.max_len or _DEFAULT_MAX.get(f.kind, 120)
    return d


def _list_ui(l: ListSpec) -> dict:
    return {"key": l.key, "label": l.label, "add": l.add_label, "max": l.max_items,
            "fields": [_field_ui(f) for f in l.item_fields]}


def catalog_for_ui() -> dict:
    """Everything the profile page needs to draw the editor. No user data."""
    return {
        "sections": [{"key": s.key, "title": s.title, "blurb": s.blurb, "icon": s.icon,
                      "fields": [_field_ui(f) for f in s.fields],
                      "lists": [_list_ui(l) for l in s.lists]} for s in SECTIONS],
        "profile_lists": {l.key: _list_ui(l) for l in PROFILE_LISTS},
    }


# ── which saved answer a form question asks for ──────────────────────────────

_COMPILED: dict = {}


def _patterns(f: Field) -> tuple:
    got = _COMPILED.get(f.key)
    if got is None:
        got = (tuple(re.compile(p, re.I) for p in f.ask),
               tuple(re.compile(p, re.I) for p in f.deny))
        _COMPILED[f.key] = got
    return got


def _segments(label: str) -> list:
    """A field's signature is "label | aria-label | placeholder | name"
    (extension/content.js getFieldSignature). Each part is judged on its own:
    a long question and a short name attribute are different evidence."""
    out = []
    for part in str(label or "").split("|"):
        s = _one_line(part.replace("_", " ").replace("*", " ")).strip(" :?.").lower()
        if s:
            out.append(s)
    return out


def _format_date(iso: str) -> str:
    """A saved date as a person writes it in a text box; "" once it has passed
    (an earliest start date in the past is no longer the answer)."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return ""
    if d < date.today():
        return ""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _languages_text(rows: list) -> str:
    parts = []
    for r in rows or []:
        lang = (r.get("language") or "").strip()
        if lang:
            lvl = (r.get("proficiency") or "").strip()
            parts.append(f"{lang} ({lvl})" if lvl else lang)
    return ", ".join(parts)


_LANGUAGES_ASK = tuple(re.compile(p, re.I) for p in (
    r"^languages?( spoken| known)?$", r"\b(what|which) languages\b[^|]*\b(speak|know|fluent)\b",
    r"\blanguages (do )?you speak\b", r"^language skills$", r"^languages you speak$"))
_CERTS_ASK = tuple(re.compile(p, re.I) for p in (
    r"^(professional )?certifications?$", r"^certifications?( and | & |/)licen[cs]es?$",
    r"^licen[cs]es?( and | & |/)certifications?$", r"\blist (any|your)[^|]*certifications?\b"))


_OPTIONAL_RE = re.compile(r"\((optional|required|if applicable|if any)\)")
# A box that wants an explanation, not a Yes or a No.
_NOT_YESNO_RE = re.compile(
    r"\b(if yes|if so|if no|if you answered|explain|describe|provide details|give details|"
    r"reason|list (them|any|all))\b")


def _head(segs: list) -> str:
    """The label a person reads: the first part of the signature, without
    "(optional)" and the like."""
    return _OPTIONAL_RE.sub("", segs[0]).strip(" :?.") if segs else ""


def _matches(f: Field, segs: list) -> bool:
    """Does the field's visible label ask for ``f``?

    Only the first part of the signature (the label a person reads) can say
    yes: a ``name="city"`` attribute under "Which city would you prefer to
    work in?" is not a home-city question. ANY part can say no."""
    ask, deny = _patterns(f)
    if any(p.search(s) for p in deny for s in segs):
        return False
    head = _head(segs)
    if not head or len(head) > f.max_label:
        return False
    if f.kind == YESNO and _NOT_YESNO_RE.search(head):
        return False
    return any(p.search(head) for p in ask)


def field_for_label(label: str) -> Optional[Field]:
    """The ONE catalog field ``label`` asks for, or None.

    None when no field's patterns match, when two different fields both match
    (an ambiguous label is nobody's), or when the question is one a saved
    default must never answer (``field_guards``)."""
    if not label or field_guards.not_a_question_reason(label):
        return None
    if field_guards.user_only_reason(label):
        return None
    segs = _segments(label)
    hits = [f for f in JSON_FIELDS.values() if f.ask and _matches(f, segs)]
    return hits[0] if len(hits) == 1 else None


def answer_for_label(label: str, data: dict) -> Optional[str]:
    """The saved answer for the form field ``label``, or None to leave it.

    ``data`` is the user's stored auto-fill object (``load``)."""
    if not label or not data:
        return None
    if field_guards.not_a_question_reason(label) or field_guards.user_only_reason(label):
        return None
    segs = _segments(label)
    head = _head(segs)
    f = field_for_label(label)
    if f is not None:
        v = data.get(f.key)
        if not v:
            return None
        if f.key == "middle_name" and "initial" in head:
            return str(v)[:1].upper()
        if f.key == "security_clearance" and re.search(
                r"\bdo you (currently )?(have|hold|possess)\b", head):
            return "No" if v == "None" else "Yes"
        if f.kind == DATE:
            return _format_date(str(v)) or None
        return str(v)
    if not head or len(head) > 70:
        return None
    if data.get("languages") and any(p.search(head) for p in _LANGUAGES_ASK):
        return _languages_text(data["languages"]) or None
    if data.get("certifications") and any(p.search(head) for p in _CERTS_ASK):
        names = [c.get("name", "") for c in data["certifications"] if c.get("name")]
        return ", ".join(names) or None
    return None


# The extension's own fuzzy matcher (content.js keywordOverlap), restated so
# the server can tell which of its answers that matcher would hand to another
# field. Keep the two in step.
_STOP = frozenset("do you want to the a an is are your this that at for in of and or why what how "
                  "tell us about".split())


def _keywords(text: str) -> list:
    return [w for w in re.split(r"\W+", str(text or "").lower()) if len(w) > 2 and w not in _STOP]


def keyword_overlap(a: str, b: str) -> float:
    wa, wb = _keywords(a), _keywords(b)
    if not wa or not wb:
        return 0.0
    sa = set(wa)
    return sum(1 for w in wb if w in sa) / max(len(wa), len(wb))


def drop_borrowable(answers: dict, all_labels: Iterable[str], other_answered: Iterable[str] = ()) -> dict:
    """``answers`` minus any the extension would copy into a DIFFERENT field.

    The extension fills a field with no exact answer from any answered label
    whose words overlap it by 80% ("Address line 1" and "Address line 2" are
    the same two words to it). So an answer is withheld when a label in the
    same request has no answer of its own and overlaps it that much: a blank
    street address is one field to type, a street address in the apartment box
    is a wrong form."""
    answered = set(answers) | set(other_answered)
    unanswered = [l for l in all_labels if l not in answered]
    keep = {}
    for key, val in answers.items():
        if any(keyword_overlap(u, key) >= 0.8 for u in unanswered):
            continue
        keep[key] = val
    return keep
