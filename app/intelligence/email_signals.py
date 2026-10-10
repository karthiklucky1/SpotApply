"""What an inbox row says about a job application (2026-10-10).

The extension's email sync reads the inbox LIST — subject, sender, the preview
snippet (sent as ``body``) and a date — never a message body, so every decision
here is made from ~200 characters. Three questions, answered separately and
conservatively:

1. **Who sent it?** ``sender_kind``: the employer's own domain, an ATS mailer
   (Greenhouse, Workday, Lever…), a job board (LinkedIn, Indeed…), a freemail
   address, or the user themself. The old sync took the sender's DOMAIN as the
   company, so a Greenhouse confirmation was filed under "Greenhouse-mail", a
   LinkedIn Easy Apply receipt under "Linkedin" and a Workable one under
   "Workable" — 0 of 50 imported rows in production named the real employer.
2. **Which company?** ``extract_company``: from the SUBJECT when the sender is a
   platform ("Thank you for applying to Clearstory", "your application was
   sent to Cooler Master USA"), from the display name ("Acme Careers"), from
   the domain only when the sender is the employer.
3. **What does it say?** ``classify``: noise first (job alerts, sign-in links,
   profile views, marketing — a Jobs2web alert, a ClearCompany "Sign in link"
   and a Greenhouse "Show recruiters you're really interested" promo were all
   imported as applications), then rejection / interview / assessment / offer /
   acknowledgement, each with a confidence. A rejection ALWAYS beats an
   interview-stage noun ("we enjoyed the phone screen, but…"), and an
   acknowledgement suppresses a weak interview cue. Only a confident signal
   moves a card; a weak one is noted on the application for the user.

Matching an email to one of the user's applications (``pick_application``)
compares NORMALISED company names ("Acme Corp" == "Acme, Inc."), never raw
substrings ("lever" used to match "Clever"), prefers applications the user
actually submitted over shortlisted or rejected ones, and never touches a
SKIPPED one. Pure functions; the route in server.py owns the writes.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

# ── Senders ──────────────────────────────────────────────────────────────────

ATS_DOMAINS = frozenset({
    "greenhouse.io", "greenhouse-mail.io", "greenhouse-jobs.io", "grnh.se",
    "myworkday.com", "myworkdayjobs.com", "workday.com", "workdaymail.com",
    "lever.co", "hire.lever.co", "ashbyhq.com", "ashby.email",
    "smartrecruiters.com", "icims.com", "jobvite.com", "workable.com",
    "workablemail.com", "bamboohr.com", "successfactors.com", "successfactors.eu",
    "taleo.net", "oraclecloud.com", "oracle.com", "breezy.hr", "breezymail.com",
    "rippling.com", "teamtailor.com", "recruitee.com", "personio.de", "personio.com",
    "dover.com", "gem.com", "applytojob.com", "jazz.co", "jazzhr.com",
    "paylocity.com", "ultipro.com", "ukg.com", "adp.com", "clearcompany.com",
    "phenom.com", "phenompeople.com", "eightfold.ai", "avature.net", "brassring.com",
    "kenexa.com", "silkroad.com", "csod.com", "cornerstoneondemand.com",
    "jobscore.com", "pinpointhq.com", "join.com", "hirebridge.com", "crelate.com",
    "bullhorn.com", "jobadder.com", "loxo.co", "recruiterbox.com", "trakstar.com",
    "hiringthing.com", "comeet.com", "freshteam.com", "zohorecruit.com", "manatal.com",
    "talentlyft.com", "homerun.co", "wellfound.com", "hirehive.com", "pageuppeople.com",
    "myjobhelper.com", "hireology.com", "paycom.com", "paycor.com", "dayforcehcm.com",
    "ceridian.com", "sap.com", "jobs.hr.cloud.sap", "jobs2web.com", "hr.cloud.sap",
    "calendly.com", "goodtime.io", "modernloop.io", "hackerrank.com", "codility.com",
    "codesignal.com", "hirevue.com", "karat.com",
})
JOB_BOARD_DOMAINS = frozenset({
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "dice.com",
    "monster.com", "angel.co", "hired.com", "otta.com", "simplyhired.com", "jooble.org",
    "adzuna.com", "builtin.com", "themuse.com", "remoteok.com", "weworkremotely.com",
    "remotive.com", "jobicy.com", "careerbuilder.com", "wellfound.com", "levels.fyi",
    "handshake.com", "joinhandshake.com", "ladders.com", "theladders.com", "triplebyte.com",
    "welcometothejungle.com", "lensa.com", "talent.com", "neuvoo.com", "jobcase.com",
    "snagajob.com", "upwork.com", "toptal.com",
})
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "me.com", "aol.com", "protonmail.com", "proton.me",
    "pm.me", "mail.com", "gmx.com", "gmx.de", "zoho.com", "yandex.com", "fastmail.com",
    "hey.com", "duck.com",
})
# Words that name the platform, not an employer — never a company.
PLATFORM_NAMES = frozenset({
    "linkedin", "indeed", "glassdoor", "ziprecruiter", "greenhouse", "greenhouse mail",
    "greenhouse-mail", "greenhouse jobs", "greenhouse-jobs", "lever", "workday", "myworkday",
    "myworkdayjobs", "ashby", "ashbyhq", "smartrecruiters", "icims", "jobvite", "workable",
    "bamboohr", "successfactors", "taleo", "oracle", "breezy", "rippling", "teamtailor",
    "recruitee", "personio", "dover", "gem", "jazzhr", "clearcompany", "jobs2web", "dice",
    "monster", "wellfound", "hired", "otta", "simplyhired", "builtin", "calendly", "hackerrank",
    "codility", "codesignal", "hirevue", "google", "microsoft outlook", "no reply", "noreply",
    "notifications", "notification", "jobs", "careers", "recruiting", "talent", "team", "hr",
    "mail", "email", "me", "us", "you", "your", "our", "the", "this", "that", "it",
})

# ── Signals ──────────────────────────────────────────────────────────────────

REJECTION_KEYWORDS = [
    'unfortunately', 'not moving forward', 'other candidates',
    'other applicants', 'not selected', 'regret to inform',
    'we regret', 'decided not to', 'will not be proceeding',
    'not be proceeding', 'position has been filled', 'role has been filled',
    'no longer under consideration', 'not be moving forward',
    "won't be moving forward", 'will not be moving forward',
    'decided to move forward with other', 'decided to proceed with other',
    'pursue other candidates', 'not to move forward',
    'after careful consideration', 'we have chosen', 'were not selected',
    'wish you the best', 'wish you success', 'wish you well',
    'not a match at this time', 'not be advancing', 'will not be advancing',
    'unable to offer', 'not be extending', 'application was unsuccessful',
    'were unsuccessful', 'thank you for your interest, however',
    'not progressing', 'will not progress',
    # Post-interview rejections mention the stage ("after your phone screen…"),
    # so missing them here used to flip rejections into INTERVIEWING.
    "won't proceed", 'will not proceed', "won't be proceeding",
    'not continue with', "won't continue", 'unable to move forward',
    'no longer being considered', 'not been selected', 'has not been selected',
    'unsuccessful on this occasion', 'on this occasion',
    'move forward with another candidate', 'proceed with another candidate',
    'gone with another candidate', 'selected another candidate',
    'chosen another candidate', 'offer the position to another',
    'keep your resume on file', 'keep your cv on file',
    'keep your application on file', 'encourage you to apply for future',
    'apply to future openings', 'future opportunities that match',
    'not the right fit', 'other direction', 'different direction',
    'position has been closed', 'role is no longer available',
    'filled the position', 'filled this position',
]
# Cues that also appear in polite non-rejections ("we wish you the best in your
# search" closes many acknowledgements). Alone they are a WEAK rejection.
WEAK_REJECTION_CUES = frozenset({
    'wish you the best', 'wish you success', 'wish you well', 'after careful consideration',
    'on this occasion', 'keep your resume on file', 'keep your cv on file',
    'keep your application on file', 'encourage you to apply for future',
    'apply to future openings', 'future opportunities that match', 'other candidates',
    'other applicants', 'unable to offer',
})

# Precise invite / scheduling phrases. Loose words like a bare 'interview' or
# 'next steps' false-positive on acknowledgements that describe the process.
POSITIVE_KEYWORDS = [
    'invitation to interview', 'schedule an interview', 'schedule a call',
    'schedule a time', 'please schedule', 'book a time', 'set up a call',
    'set up an interview', 'pleased to invite you', 'invite you to interview',
    'invite you to an interview', 'invite you for an interview',
    'move forward with your candidacy', 'move forward with your application and',
    'selected for an interview', 'selected to interview', 'advance to the interview',
    'advance to the next round', 'like to speak with you about the role',
    'like to speak with you about this role', 'love to chat about the role',
    'share your availability', 'your availability for', 'pick a time',
    'choose a time that works', 'interview invitation', 'interview request',
    'would like to interview you', 'like to schedule', 'like to set up',
    'moving forward with your application', 'moving you forward',
    'next step is a', 'next step will be a', 'confirm your interview',
    'available for a call', 'available for a quick call', 'available for a 30', 'available for a 15',
    'minute call', 'calendly link', 'hop on a call', 'schedule a conversation',
    'interview is confirmed', 'interview confirmation', 'your interview with',
    'your upcoming interview', 'interview details',
]
# Interview-STAGE nouns: in invites, but just as often in post-interview
# rejections and process descriptions. They count only with a scheduling hint.
STAGE_NOUNS = [
    'phone screen', 'recruiter screen', 'video call', 'video interview',
    'meet the team', 'next round', 'technical assessment', 'coding challenge',
    'take-home assessment', 'technical interview', 'onsite interview',
    'on-site interview', 'final round', 'hiring manager interview',
    'panel interview', 'first round', 'second round',
]
SCHEDULING_HINTS = [
    'schedule', 'calendly', 'book a', 'set up a', 'availability',
    'pick a time', 'choose a time', 'invite', 'invitation', 'goodtime', 'modernloop',
]
# Acknowledgement / under-review auto-replies.
ACKNOWLEDGMENT_KEYWORDS = [
    'thank you for applying', 'thank you for your application', 'thanks for applying',
    'thanks for your application', 'we have received your application',
    'we received your application', "we've received your application",
    'your application has been received', 'application is currently under review',
    'application is under review', 'currently under review',
    'profile is currently under review', 'profile is under review',
    'under consideration', 'our team will review', 'our recruiting team will',
    'will be in touch', 'will reach out', 'will contact you', 'will get back to you',
    'if your experience matches', 'if selected for', 'if you are selected',
    'reviewing applications', 'reviewing all applications',
    'application has been submitted', 'application was submitted',
    'confirmation of your application', 'application confirmation',
    'application received', 'application was sent', 'application has been sent',
    'recieved your application', 'received your application',
    'successfully applied', 'successfully submitted', 'has been received',
    'thank you for your interest', 'we appreciate your interest',
]
# A greeting-type acknowledgement often OPENS an invite ("Thank you for
# applying… we'd like to schedule a call"); only these may coexist with a
# strong invite without suppressing it.
_GREETING_ACKS = frozenset({
    'thank you for applying', 'thank you for your application', 'thanks for applying',
    'thanks for your application', 'thank you for your interest', 'we appreciate your interest',
})

OFFER_RE = re.compile(
    r"\b(?:offer letter|pleased to (?:extend|offer)|extend (?:you )?an offer|offer of employment|"
    r"job offer|formal offer|we would like to offer you|congratulations[^.]{0,60}\boffer\b|"
    r"your offer (?:from|with|letter)|accept(?:ing)? (?:the|your|this) offer|signed offer|start date)\b", re.I)
ASSESSMENT_RE = re.compile(
    r"\b(?:assessment|coding challenge|take.?home|hackerrank|codility|codesignal|online test|"
    r"technical test|skills test|complete (?:the|this|your) (?:test|assessment|challenge|exercise)|"
    r"hirevue|video questions|recorded interview|one.?way interview)\b", re.I)
JOB_ALERT_RE = re.compile(
    r"\b(?:job alert|jobs? (?:you may|you might|that match|for you|matching|we think|recommended|picked)|"
    r"new jobs?(?: posted| for you| matching| at| in| near)|recommended jobs|new openings?|"
    r"\d+ new (?:jobs?|roles?|positions?)|top job picks|job recommendations|hiring now|"
    r"jobs similar to|similar jobs|your job search alerts?|daily job|weekly job|job digest|job matches)\b", re.I)
ACCOUNT_RE = re.compile(
    r"\b(?:sign.?in link|log.?in link|verification code|verify your (?:email|account|identity)|"
    r"confirm your (?:email|account|email address)|reset your password|password reset|one.?time (?:code|password|passcode)|"
    r"magic link|activate your account|complete your (?:profile|registration|account)|"
    r"your (?:security|login|access) code|two.?factor|2fa code|your code is)\b", re.I)
PROFILE_VIEW_RE = re.compile(
    r"\b(?:was viewed by|viewed your profile|viewed your resume|appeared in \d+ search|"
    r"who.s viewed|profile views?|people are looking at your profile|searched for people like you)\b", re.I)
MARKETING_RE = re.compile(
    r"\b(?:webinar|newsletter|% off|discount|upgrade to premium|try premium|premium free|"
    r"show recruiters you.re really interested|dream job|boost your|career tips|salary report|"
    r"salary insights|resume tips|interview tips|product update|new feature|introducing|"
    r"survey|feedback on your experience|rate your experience|how did we do)\b", re.I)
OUTREACH_RE = re.compile(
    r"\b(?:came across your profile|your background|your experience caught|opportunity (?:that|which) (?:may|might)|"
    r"are you open to|exciting opportunity|would you be interested|i.m a recruiter|i am a recruiter|"
    r"reaching out|quick chat|connect with you about|open to new opportunities|exploring new roles)\b", re.I)
ACK_RE = re.compile(
    r"\b(?:you applied\b|applied to\b|application (?:to|at|with|for) .{1,60}?(?:was|has been|is) (?:sent|submitted|received|under review)|"
    r"we(?:'ve| have)? (?:now )?received your (?:application|resume|cv|submission))", re.I)
# An application-shaped email names the act of applying; marketing rarely does.
APPLICATION_WORD_RE = re.compile(r"\b(?:applied|applying|application|candidacy|interview|offer letter)\b", re.I)

NOISE_KINDS = frozenset({"job_alert", "account", "profile_view", "marketing"})
APPLICATION_KINDS = frozenset({"ack", "rejection", "interview", "assessment", "offer"})
# The confidence a signal needs before it MOVES a card. Below it, the signal
# is noted on the application and the user decides.
STATUS_CHANGE_CONFIDENCE = 0.75


@dataclass
class EmailSignal:
    kind: str
    confidence: float
    cues: list = field(default_factory=list)

    @property
    def is_noise(self) -> bool:
        return self.kind in NOISE_KINDS

    @property
    def moves_status(self) -> bool:
        return self.kind in ("rejection", "interview", "offer") and self.confidence >= STATUS_CHANGE_CONFIDENCE


def _deaccent(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def sender_domain(sender: str) -> str:
    s = (sender or "").strip().lower()
    m = re.search(r"@([a-z0-9.-]+)", s)
    return m.group(1).strip(".") if m else ""


def _registrable(domain: str) -> str:
    """'us.greenhouse-mail.io' -> 'greenhouse-mail.io'; 'jobs.hr.cloud.sap' kept as is."""
    parts = [p for p in domain.split(".") if p]
    if len(parts) <= 2:
        return domain
    # Two-part public suffixes (co.uk, com.au…): keep three labels.
    if parts[-2] in {"co", "com", "org", "net", "ac", "gov", "edu"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _domain_in(domain: str, pool: frozenset) -> bool:
    if not domain:
        return False
    if domain in pool or _registrable(domain) in pool:
        return True
    # Sub-domains of a listed domain ("us.greenhouse-mail.io", "mail.lever.co").
    return any(domain.endswith("." + d) for d in pool)


def sender_kind(sender_email: str, sender_name: str = "", user_email: Optional[str] = None) -> str:
    """ats | job_board | freemail | company | self | unknown"""
    addr = (sender_email or "").strip().lower()
    dom = sender_domain(addr)
    if user_email and addr and addr == (user_email or "").strip().lower():
        return "self"
    if not dom:
        name = (sender_name or "").strip().lower()
        if name in ("me", "you"):
            return "self"
        return "unknown"
    if _domain_in(dom, ATS_DOMAINS):
        return "ats"
    if _domain_in(dom, JOB_BOARD_DOMAINS):
        return "job_board"
    if _domain_in(dom, FREEMAIL_DOMAINS):
        return "freemail"
    return "company"


# ── Company extraction ───────────────────────────────────────────────────────

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd?|fw|aw|wg)\s*:\s*)+", re.I)
_TRAIL_RE = re.compile(r"\s*(?:[-–—|:!,.;]|\(.*)\s*$")
_COMPANY_TAIL_WORDS = re.compile(
    r"\b(?:careers?|recruiting|recruitment|talent(?: acquisition)?|hiring team|hiring|team|hr|"
    r"people(?: ops| team)?|jobs|no.?reply|notifications?|via .*|through .*)\b\s*$", re.I)
_PRONOUN_RE = re.compile(r"^(?:your|our|the|this|that|my|a|an|me|you|us|it)\b", re.I)
_ROLE_WORDS_RE = re.compile(
    r"\b(?:engineer|developer|manager|analyst|designer|scientist|intern|position|role|job|opening|"
    r"opportunity|application|candidate|resume|cv|senior|junior|lead|staff|principal|director|"
    r"specialist|associate|consultant|architect|administrator|coordinator)\b", re.I)
_SUBJECT_PATTERNS = [
    # LinkedIn Easy Apply / Indeed: "KARTHIK, your application was sent to Cooler Master USA"
    re.compile(r"application (?:was |has been |is )?(?:sent|submitted|forwarded|delivered) to (?P<c>.+?)(?:\s*[-–—|!.]|$)", re.I),
    # "Thank you for applying to Clearstory", "Thanks for applying to Acme!", "Thank you for your interest in Acme"
    re.compile(r"thanks?(?:\s+you)?\s+for\s+(?:applying|your\s+application|your\s+interest|submitting your application)"
               r"(?:\s+(?:to|at|with|in)\s+(?:the\s+)?)(?P<c>.+?)(?:\s*[-–—|!,.]|\s+for\s+(?:the|our|a)\b|$)", re.I),
    # "Your application to Acme Corp", "Your application for Software Engineer at Acme", "Application update from Acme"
    re.compile(r"(?:your\s+)?application(?:\s+(?:update|status|received|confirmation))?\s+(?:to|at|with|from)\s+(?P<c>.+?)(?:\s*[-–—|!.:]|\s+(?:for|has|is|was)\b|$)", re.I),
    re.compile(r"application\s+for\s+.+?\s+at\s+(?P<c>.+?)(?:\s*[-–—|!.:]|$)", re.I),
    # "Update on your application to Acme", "News about your candidacy at Acme"
    re.compile(r"(?:update|news|decision|next steps?)\s+(?:on|about|regarding|for)\s+your\s+(?:.*?\s)?(?:application|candidacy)\s+(?:to|at|with|for)\s+(?P<c>.+?)(?:\s*[-–—|!.:]|$)", re.I),
    # "Interview with Acme", "Phone screen at Acme for Backend Engineer", "Your interview with Acme is confirmed"
    re.compile(r"(?:interview|conversation|chat|call|screen|assessment|offer)\s+(?:with|at|from)\s+(?P<c>.+?)(?:\s+(?:for|about|re|regarding|is|has|on)\b|\s*[-–—|!.:]|$)", re.I),
    # "Acme has received your application", "Acme: application received", "Acme | Interview"
    re.compile(r"^(?P<c>.+?)\s+(?:has|have)\s+received\s+your\s+application", re.I),
    re.compile(r"^(?P<c>[^-–—|:]+?)\s*[-–—|:]\s*(?:application|your application|thank you|thanks|interview|next steps|we received|update)", re.I),
    # "Welcome to the Acme hiring process", "Message from the Acme recruiting team"
    re.compile(r"(?:from|with|join|at)\s+(?:the\s+)?(?P<c>[A-Z][\w&.'’\- ]{1,40}?)\s+(?:hiring|recruiting|talent)\s+(?:team|process)", re.I),
    # "You applied to Datadog", "Successfully applied: ML Engineer at Scale AI"
    re.compile(r"applied(?:\s+to|\s*:\s*.+?\s+at|\s+for\s+.+?\s+at)\s+(?P<c>.+?)(?:\s*[-–—|!.:]|$)", re.I),
    # "An update from Coinbase", "A message from Acme"
    re.compile(r"(?:update|news|decision|message|note)\s+from\s+(?P<c>.+?)(?:\s*[-–—|!.:]|$)", re.I),
    # "Your HackerRank assessment for Stripe", "Codility test from Atlassian"
    re.compile(r"(?:assessment|test|challenge|exercise)\s+(?:for|from|with)\s+(?P<c>.+?)(?:\s*[-–—|!.:]|$)", re.I),
    # "Exciting opportunity at Datadog", "Backend role at Brex"
    re.compile(r"\b(?:opportunity|opportunities|role|position|opening|round)\s+(?:at|with)\s+(?P<c>[A-Z][\w&.'’\- ]{1,40}?)(?:\s*[-–—|!.:]|\s+for\b|$)"),
    # "Thank you for your application - Stripe", "Codility test invitation - Atlassian": the tail after a dash
    re.compile(r"[-–—|:]\s*(?P<c>[A-Z][\w&.'’\- ]{1,40})\s*$"),
]
# Words a captured phrase may hold that make it a process word, not an employer.
_PROCESS_WORDS_RE = re.compile(
    r"\b(?:screen|interview|invitation|update|received|confirmation|next steps?|reminder|status|"
    r"welcome|congratulations|thank you|thanks|offer|assessment|test|challenge|request|follow.?up|"
    r"phone|video|onsite|final|round|team|process|account|password|code|alert|digest|newsletter)\b", re.I)


def _clean_company(raw: str) -> Optional[str]:
    c = _deaccent((raw or "").strip())
    c = re.sub(r"^[\"'“”‘’\[(]+|[\"'“”‘’\])]+$", "", c).strip()
    c = _TRAIL_RE.sub("", c).strip()
    c = _COMPANY_TAIL_WORDS.sub("", c).strip(" -–—|:,")
    c = re.sub(r"\s{2,}", " ", c)
    # "the Software Engineer position at NVIDIA" -> "NVIDIA"
    m = re.search(r"\b(?:at|with|from)\s+([A-Z][^,.;]*)$", c)
    if m and _ROLE_WORDS_RE.search(c[: m.start()]):
        c = m.group(1).strip()
    if not c or len(c) < 2 or len(c) > 60:
        return None
    low = c.lower()
    if _PROCESS_WORDS_RE.search(c) and not re.search(r"\b(?:inc|llc|ltd|corp|co|labs|group)\b", low):
        return None
    if low.split()[0] in PLATFORM_NAMES and len(low.split()) > 1:
        return None
    if low in PLATFORM_NAMES or _PRONOUN_RE.match(c) and len(c.split()) == 1:
        return None
    if _ROLE_WORDS_RE.search(c) and len(c.split()) <= 3 and not re.search(r"\b(?:inc|llc|ltd|corp|co|labs|group)\b", low):
        # "Backend Engineer", "the Senior Engineer role" — a role, not a company
        return None
    if _PRONOUN_RE.match(c):
        return None
    return c


def _company_from_display(name: str) -> Optional[str]:
    n = _deaccent((name or "").strip().strip("\"'"))
    if not n or "@" in n:
        return None
    m = re.search(r"\b(?:from|at|@)\s+(?P<c>.+)$", n, re.I)
    if m:
        return _clean_company(m.group("c"))
    n = re.sub(r"\s*\((?:via|through)\s+[^)]*\)\s*$", "", n, flags=re.I)
    n = re.sub(r"\s+(?:via|through)\s+.*$", "", n, flags=re.I)
    cleaned = _clean_company(n)
    if not cleaned:
        return None
    # Two capitalised words that look like a person ("Alice Chen") are not a company.
    words = cleaned.split()
    if 1 < len(words) <= 3 and all(w[:1].isupper() and w[1:].islower() and w.isalpha() for w in words) \
            and not re.search(r"\b(?:inc|llc|ltd|corp|labs|group|systems|technologies|software|solutions|health|bank|capital|partners|studio|media|games|energy|motors|foods|logistics|ai)\b", cleaned, re.I):
        return None
    return cleaned


def _company_from_domain(sender_email: str) -> Optional[str]:
    dom = _registrable(sender_domain(sender_email))
    if not dom:
        return None
    label = dom.split(".")[0]
    label = re.sub(r"^(?:mail|email|jobs|careers|recruiting|talent|hr|no-?reply|notifications?|news|info|team|apply|hire)[-.]?", "", label) or dom.split(".")[0]
    if len(label) < 2:
        return None
    return label[:1].upper() + label[1:]


def _company_from_subject(subject: str) -> Optional[str]:
    subj = _SUBJECT_PREFIX_RE.sub("", _deaccent(subject or "")).strip()
    if not subj:
        return None
    for pat in _SUBJECT_PATTERNS:
        m = pat.search(subj)
        if m:
            c = _clean_company(m.group("c"))
            if c:
                return c
    # A bare company as the whole subject ("Neuralink") — short, no role words,
    # and no everyday word ("Quick question", "Random Email" are not employers).
    words = subj.split()
    if 1 <= len(words) <= 3 and not _ROLE_WORDS_RE.search(subj) and not _PRONOUN_RE.match(subj) \
            and subj[:1].isupper() and subj.lower() not in PLATFORM_NAMES and not subj.endswith("?") \
            and not any(w.lower().strip(",.!:") in _EVERYDAY_WORDS for w in words):
        return _clean_company(subj)
    return None


_EVERYDAY_WORDS = frozenset({
    "email", "question", "questions", "update", "updates", "hello", "hi", "hey", "reminder", "follow",
    "following", "up", "important", "urgent", "invitation", "information", "request", "notice", "note",
    "message", "re", "fwd", "fw", "application", "interview", "offer", "test", "random", "quick", "new",
    "your", "the", "a", "an", "thank", "thanks", "you", "for", "welcome", "congratulations", "action",
    "required", "confirm", "confirmation", "next", "steps", "status", "received", "regarding", "about",
    "today", "tomorrow", "week", "schedule", "scheduling", "call", "chat", "meeting", "feedback", "survey",
    "newsletter", "digest", "alert", "alerts", "job", "jobs", "career", "careers", "opportunity", "role",
    "position", "candidate", "resume", "cv", "profile", "account", "password", "code", "verify", "verification",
})


_SNIPPET_PATTERNS = _SUBJECT_PATTERNS[:5] + [_SUBJECT_PATTERNS[9]]   # the application-shaped ones only


def _company_from_snippet(snippet: str) -> Optional[str]:
    text = _deaccent(snippet or "").strip()
    if not text:
        return None
    for pat in _SNIPPET_PATTERNS:
        m = pat.search(text)
        if m:
            c = _clean_company(m.group("c"))
            if c:
                return c
    m = re.search(r"\b(?:position|role|opening|opportunity|job)\s+at\s+(?P<c>[A-Z][\w&.'’\- ]{1,40}?)(?:\s*[-–—|!.,:]|\s+(?:for|has|is|was)\b|$)", text)
    return _clean_company(m.group("c")) if m else None


def norm_company(name: Optional[str]) -> str:
    n = _deaccent((name or "").lower())
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    tokens = [t for t in n.split() if t not in {
        "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc", "gmbh",
        "sa", "ag", "bv", "nv", "pty", "the", "group", "holdings", "technologies", "technology",
        "labs", "international", "global", "usa", "us", "uk",
    }]
    return " ".join(tokens)


def company_matches(a: Optional[str], b: Optional[str]) -> bool:
    """Same employer, by normalised name. Word-boundary containment only when the
    shorter side is a real name (4+ chars): 'lever' never matches 'clever'."""
    na, nb = norm_company(a), norm_company(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    if len(short) < 4:
        return False
    return f" {short} " in f" {long_} "


def is_platform_name(name: Optional[str]) -> bool:
    return norm_company(name) in PLATFORM_NAMES or (name or "").strip().lower() in PLATFORM_NAMES


def extract_company(subject: str, sender_email: str = "", sender_name: str = "",
                    user_email: Optional[str] = None, company_guess: Optional[str] = None,
                    snippet: str = "") -> Optional[str]:
    """The employer an application email is about, or None.

    Platform senders (ATS, job board, freemail, the user) never name the
    employer by domain — the subject does, then the display name. An employer's
    own domain names it, but a subject/display/guess that AGREES with the domain
    wins for its proper casing ("NimbusAI", not "Nimbusai")."""
    kind = sender_kind(sender_email, sender_name, user_email)
    from_subject = _company_from_subject(subject)
    if not from_subject and snippet:
        # The preview often carries what the subject does not ("Thank you for
        # applying to the Software Engineer position at NVIDIA").
        from_subject = _company_from_snippet(snippet)
    from_display = _company_from_display(sender_name)
    guess = (company_guess or "").strip() or None
    if guess and is_platform_name(guess):
        guess = None
    if kind == "company":
        label = _company_from_domain(sender_email)
        for cand in (from_subject, guess, from_display):
            if cand and label and company_matches(cand, label):
                return cand
            if cand and label and norm_company(label) in norm_company(cand).replace(" ", ""):
                return cand
        if from_subject and not _ROLE_WORDS_RE.search(from_subject):
            # "Thank you for applying to Acme" sent from a parent brand's domain.
            return from_subject
        return guess or from_display or label
    for cand in (from_subject, from_display, guess):
        if cand and not is_platform_name(cand):
            return cand
    return None


# ── Classification ───────────────────────────────────────────────────────────

def classify(subject: str, snippet: str = "", sender_kind_: str = "company") -> EmailSignal:
    text = _deaccent(f"{subject or ''} {snippet or ''}").lower()
    subj = _deaccent(subject or "").lower()
    cues: list[str] = []

    # 1. Noise — never an application signal, never imported.
    if ACCOUNT_RE.search(text):
        return EmailSignal("account", 0.9, ["account"])
    if PROFILE_VIEW_RE.search(text):
        return EmailSignal("profile_view", 0.9, ["profile_view"])
    if JOB_ALERT_RE.search(text) or (sender_kind_ == "job_board" and re.search(r"\bjobs?\b", subj)
                                     and not APPLICATION_WORD_RE.search(text)):
        return EmailSignal("job_alert", 0.9, ["job_alert"])
    if MARKETING_RE.search(text) and not APPLICATION_WORD_RE.search(text):
        return EmailSignal("marketing", 0.7, ["marketing"])

    # 2. Application signals. A rejection ALWAYS wins.
    rej = [kw for kw in REJECTION_KEYWORDS if kw in text]
    if rej:
        strong = [k for k in rej if k not in WEAK_REJECTION_CUES]
        return EmailSignal("rejection", 0.92 if strong else 0.6, rej)
    acks = [kw for kw in ACKNOWLEDGMENT_KEYWORDS if kw in text]
    if ACK_RE.search(text):
        acks.append("applied")
    non_greeting_acks = [a for a in acks if a not in _GREETING_ACKS]
    strong_invite = [kw for kw in POSITIVE_KEYWORDS if kw in text]
    stage = [kw for kw in STAGE_NOUNS if kw in text]
    sched = [kw for kw in SCHEDULING_HINTS if kw in text]
    if OFFER_RE.search(text) and not non_greeting_acks and not JOB_ALERT_RE.search(text):
        return EmailSignal("offer", 0.85, ["offer"])
    if strong_invite and not non_greeting_acks:
        return EmailSignal("interview", 0.9, strong_invite)
    if ASSESSMENT_RE.search(text) and not non_greeting_acks:
        return EmailSignal("assessment", 0.8, ["assessment"])
    if stage and sched and not acks:
        return EmailSignal("interview", 0.75, stage + sched)
    if acks:
        return EmailSignal("ack", 0.85, acks)
    if OUTREACH_RE.search(text) and sender_kind_ in ("company", "freemail", "unknown"):
        return EmailSignal("recruiter_outreach", 0.7, ["outreach"])
    if stage or strong_invite:
        return EmailSignal("interview", 0.55, stage + strong_invite)   # weak: noted, never moves a card
    return EmailSignal("other", 0.3, cues)


# ── Matching an email to the user's applications ─────────────────────────────

@dataclass
class Candidate:
    id: int
    status: str                 # ApplicationStatus value
    company: str
    title: str
    submitted_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


_STATUS_RANK = {
    "submitted": 0, "interviewing": 0, "offer": 0,
    "awaiting_user": 1, "ready_to_submit": 1, "autofilled": 1, "tailored": 1,
    "shortlisted": 2, "error": 2, "matched": 3, "discovered": 3,
    "rejected": 4, "accepted": 4,
}


def _title_tokens(s: str) -> set:
    return {t for t in re.findall(r"[a-z0-9+#]+", (s or "").lower()) if len(t) > 2
            and t not in {"the", "and", "for", "with", "role", "position", "job", "application"}}


def pick_application(company: Optional[str], title_hint: str, candidates: Iterable[Candidate]) -> Optional[Candidate]:
    """The application an email about `company` most plausibly concerns. Exact
    normalised name first, then word-boundary containment; among those, the one
    the user actually SUBMITTED, then the closest title, then the most recent."""
    if not company:
        return None
    pool = [c for c in candidates if norm_company(c.company) == norm_company(company)]
    if not pool:
        pool = [c for c in candidates if company_matches(c.company, company)]
    if not pool:
        return None
    hint = _title_tokens(title_hint)

    def key(c: Candidate):
        status = c.status.value if hasattr(c.status, "value") else str(c.status)
        overlap = len(hint & _title_tokens(c.title)) if hint else 0
        recency = c.submitted_at or c.updated_at or datetime.min
        return (_STATUS_RANK.get(status, 3), -overlap, -recency.timestamp() if recency else 0)
    return min(pool, key=key)
