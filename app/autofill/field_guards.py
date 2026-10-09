"""What the AI-answer path must never answer, and replies it must never keep.

Live test 2026-10-09 (Ashby, Horizon3): the extension found the page's HIDDEN
reCAPTCHA textarea (``name="g-recaptcha-response"``, ``display:none``), read
its name as the "question", and asked ``/api/answer-question``. The model
replied "I'd be happy to help, but I notice the essay question appears
incomplete or unclear. "g-recaptcha-response" looks like a technical
parameter…", that reply was CACHED under the user's AnswerMemory, and the
extension typed it into the captcha field of a real application.

Two rules, applied at every door that reads or writes AnswerMemory
(``answer_question``, ``_lookup_memory``/``_save_memory``, ``/api/save-answer``,
``/api/recall-answers``):

* an anti-bot field (captcha, Turnstile, honeypot) or a bare form identifier is
  not a question: nothing answers it, nothing is cached for it, and a row
  already cached under such a key is never served;
* a model reply that talks ABOUT the question instead of answering it ("I'd be
  happy to help, but…", "the question appears incomplete") is not an answer:
  it is never returned, never cached, and a cached one is never served.

Some questions are facts only the applicant holds — how they heard about the
role, work authorization, protected self-identification. A model can only
invent those, so they are left for the applicant (``user_only_reason``).

The extension carries the same patterns (``isAntiBotField`` /
``looksLikeMetaReply`` in extension/content.js); this side protects users on
older extension builds the moment the server deploys.
"""
from __future__ import annotations

import re
from typing import Optional

# Captcha / challenge / honeypot field names and labels. Word-ish boundaries are
# loose on purpose: these appear as "g-recaptcha-response", "h-captcha-response",
# "cf-turnstile-response", "frc-captcha-solution", "website_honeypot".
_ANTI_BOT_RE = re.compile(
    r"(re-?captcha|h-?captcha|captcha|turnstile|cf[-_]chl|honey-?pot|arkose|funcaptcha"
    r"|friendly-?captcha|frc-captcha|bot[-_ ]?(?:check|trap|field)|anti[-_ ]?bot"
    r"|leave (?:this )?(?:field )?(?:blank|empty)|do ?n[o']t (?:fill|change) (?:this|in))",
    re.I,
)

# No whitespace and a separator, digit or bracket: a form field's NAME, not a
# question a person wrote ("g-recaptcha-response", "question_68444493",
# "cards[abc][field0]"). Plain words ("Comments") are not identifiers.
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.:\[\]-]+")
_IDENTIFIER_MARK_RE = re.compile(r"[_\-\d\[\]:.]")

_HEAR_ABOUT_RE = re.compile(
    r"how did you (?:first )?(?:hear|learn|find out|find)|hear about (?:us|this)|"
    r"(?:referral|lead|candidate|application) source|source of (?:referral|application)|"
    r"where did you (?:hear|find|see|learn)",
    re.I,
)
_WORK_AUTH_RE = re.compile(
    r"sponsor|authoriz|visa status|work permit|right to work|eligible to work", re.I)
_DEMOGRAPHIC_RE = re.compile(
    r"\b(gender|sex|pronouns?|race|racial|ethnic\w*|hispanic|latin[oaex]|veteran|disab\w*"
    r"|sexual orientation|transgender|lgbtq?\w*)\b",
    re.I,
)

# A reply ABOUT the question rather than an answer to it. Anchored phrases at
# the start (a real answer never opens with "I'd be happy to help"), plus a few
# unmistakable meta statements anywhere in the text.
_META_START_RE = re.compile(
    r"^\s*[\"']?(?:"
    r"i'?d be (?:happy|glad) to help"
    r"|i(?:'m| am) (?:sorry|unable|not able)"
    r"|i (?:cannot|can't|can not|am unable to|won't) (?:answer|write|respond|provide|help|complete)"
    r"|as an ai\b"
    r"|i notice (?:that )?(?:the|this|your) (?:essay )?(?:question|prompt|field|text)"
    r"|it (?:looks|seems|appears) (?:like|that) (?:the|this|your) (?:essay )?(?:question|prompt|field|text)"
    r"|(?:the|this) (?:essay )?(?:question|prompt|field) (?:appears|seems|you provided|is (?:incomplete|unclear|missing))"
    r"|could you (?:please )?(?:provide|clarify|share)"
    r"|please (?:provide|clarify|share) (?:the|more|a|an)"
    r")",
    re.I,
)
_META_ANY_RE = re.compile(
    r"(?:question|prompt) (?:appears|seems) (?:to be )?(?:incomplete|unclear|missing|empty|cut off)"
    r"|technical (?:parameter|field|identifier)"
    r"|(?:is|isn't|is not|doesn't look like|does not look like) (?:a|an) (?:real |actual )?"
    r"(?:essay |application )?question"
    r"|form field (?:name|identifier)"
    r"|provide the (?:actual|full|complete) (?:essay )?question"
    r"|as an ai (?:language )?model",
    re.I,
)

#: What the essay prompt asks the model to reply with for a non-question.
SKIP_SENTINEL = "SKIP"


def is_anti_bot_field(label: str | None) -> bool:
    """A captcha / challenge / honeypot field, by its name or label."""
    return bool(label) and bool(_ANTI_BOT_RE.search(str(label)))


def is_field_identifier(label: str | None) -> bool:
    """A bare form-field name used as the 'question' (the extension falls back
    to an element's name/id when it has no label)."""
    s = (label or "").strip()
    if not s or any(c.isspace() for c in s):
        return False
    return bool(_IDENTIFIER_RE.fullmatch(s)) and bool(_IDENTIFIER_MARK_RE.search(s))


def not_a_question_reason(label: str | None) -> Optional[str]:
    """Why ``label`` is not something to answer at all, or None."""
    if is_anti_bot_field(label):
        return "anti_bot_field"
    if is_field_identifier(label):
        return "field_identifier"
    return None


def user_only_reason(question: str | None) -> Optional[str]:
    """Why a model must not write this answer (only the applicant knows it)."""
    q = question or ""
    if _HEAR_ABOUT_RE.search(q):
        return "applicant_fact"
    if _WORK_AUTH_RE.search(q):
        return "work_authorization"
    if _DEMOGRAPHIC_RE.search(q):
        return "self_identification"
    return None


def looks_like_meta_reply(answer: str | None) -> bool:
    """A reply that discusses the question instead of answering it."""
    a = (answer or "").strip()
    if not a:
        return False
    if a.strip(" .\"'").upper() == SKIP_SENTINEL:
        return True
    return bool(_META_START_RE.search(a) or _META_ANY_RE.search(a))
