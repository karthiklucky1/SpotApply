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
  The full test (``looks_like_meta_reply``) runs on MODEL output only; text the
  applicant typed is refused only in the forms a model alone writes
  (``is_model_only_reply``), because their own answer stays theirs.

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
#
# Honeypot WORDING counts only in the forms a honeypot uses: the whole text is
# the instruction ("Leave this field blank", "Please leave this field empty.",
# "Do not fill this in"), or it addresses humans ("If you are human, leave this
# field blank"). A real question that says when to leave it blank ("LinkedIn
# profile (leave blank if none)", "Leave blank if you were not referred") is a
# field the applicant fills, so it must stay fillable and learnable (review
# 2026-10-09: the looser pattern stopped LinkedIn/GitHub fields the 1.0.0
# build filled). Labels are whitespace-collapsed before matching. "Human"
# counts only as the whole address, followed by punctuation, the end, or
# "leave"/"don't": "If you are a Human Resources professional, which HRIS
# platforms have you used?" is a real question (review 2026-10-09).
_ANTI_BOT_RE = re.compile(
    r"(re-?captcha|h-?captcha|captcha|turnstile|cf[-_]chl|honey-?pot|arkose|funcaptcha"
    r"|friendly-?captcha|frc-captcha|bot[-_ ]?(?:check|trap|field)|anti[-_ ]?bot"
    r"|if you(?:'re|’re| are) (?:a )?human(?:\s*[,.;:!?)]|\s*$|\s+(?:please |then )?(?:leave|do ?n[o'’]t)\b)"
    r"|^\W*(?:please )?(?:leave this (?:field |input |box )?(?:blank|empty)"
    r"|do ?n[o'’]t (?:fill|change) (?:in )?this(?: field)?(?: in| out)?)\W*$)",
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

# A MODEL reply ABOUT the question rather than an answer to it
# (``looks_like_meta_reply``). Anchored phrases at the start (a real answer
# never opens with "I'd be happy to help"), plus a few unmistakable meta
# statements anywhere in the text. "I'm unable"/"I'm sorry" count only when
# they are about answering or about the question: "I am unable to start before
# January" is an answer.
_META_START_RE = re.compile(
    r"^\s*[\"']?(?:"
    r"i'?d be (?:happy|glad) to help"
    r"|i(?:'m| am) (?:sorry,? (?:but )?i (?:cannot|can't|can not|am unable to|am not able to)"
    r"|unable to|not able to) (?:answer|write|respond|provide|help|complete|assist)\b"
    r"|i(?:'m| am) (?:sorry|unable|not able)\b[^.?!]{0,60}\b(?:the|this|your) (?:essay )?(?:question|prompt)\b"
    r"|i (?:cannot|can't|can not|am unable to|won't) (?:answer|write|respond|provide|help(?! but)|complete)"
    r"|as an ai(?: (?:language )?model| assistant)?\s*,"
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
    r"|(?:looks|seems|appears) (?:like|to be) (?:a|an) (?:technical|form|html|internal|system) "
    r"(?:parameter|field|identifier|name|token|value)"
    r"|(?:this|that|it|\"[^\"]{1,80}\") (?:is|isn't|is not|doesn't look like|does not look like"
    r"|doesn't appear to be|does not appear to be) (?:a|an) (?:real |actual |complete |valid )?"
    r"(?:essay |application )?question\b"
    r"|form field (?:name|identifier)"
    r"|provide the (?:actual|full|complete) (?:essay )?question"
    r"|as an ai language model|as an ai (?:model|assistant),",
    re.I,
)

# The forms ONLY a model writes, anchored at the start. Text a PERSON typed
# (``/api/save-answer``, ``/api/recall-answers``, every AnswerMemory read) is
# judged by these alone: an applicant's own answer may say "I am unable to
# start before January" or "is a question of mission alignment", and it stays
# theirs (review 2026-10-09). A cached reply about the question in these forms
# (the reCAPTCHA reply was one) still never comes back.
_MODEL_ONLY_RE = re.compile(
    r"^\s*[\"']?(?:"
    r"i'?d be (?:happy|glad) to help(?: you)?(?:\s*[,!.:;]| with (?:this|that|the|your) "
    r"(?:essay |application )?(?:question|prompt))"
    r"|as an ai(?: (?:language )?model| assistant)?\s*,"
    r"|(?:i notice (?:that )?|it (?:looks|seems|appears) (?:like|that) )?(?:the|this|your) "
    r"(?:essay )?(?:question|prompt) (?:appears|seems|is) (?:to be )?"
    r"(?:incomplete|unclear|missing|empty|cut off)"
    r")",
    re.I,
)

#: What the essay prompt asks the model to reply with for a non-question.
SKIP_SENTINEL = "SKIP"


def is_anti_bot_field(label: str | None) -> bool:
    """A captcha / challenge / honeypot field, by its name or label."""
    return bool(label) and bool(_ANTI_BOT_RE.search(" ".join(str(label).split())))


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
    """A MODEL reply that discusses the question instead of answering it.

    For model output only (a fresh generation). Text a person typed is judged
    by ``is_model_only_reply``, which never refuses a plausible answer."""
    a = (answer or "").strip()
    if not a:
        return False
    if a.strip(" .\"'").upper() == SKIP_SENTINEL:
        return True
    return bool(_META_START_RE.search(a) or _META_ANY_RE.search(a))


def is_model_only_reply(answer: str | None) -> bool:
    """A reply in a form only a model writes ("I'd be happy to help, but…",
    "As an AI,…", "The question appears incomplete"). Safe on text a person
    typed: it is what /api/save-answer, /api/recall-answers and every
    AnswerMemory read refuse."""
    a = (answer or "").strip()
    return bool(a) and bool(_MODEL_ONLY_RE.search(a))
