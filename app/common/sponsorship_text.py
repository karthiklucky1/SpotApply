"""Refusal detection over posting text — the ONE place the phrases live.

Two modules used to carry hand-synced copies of the same list
(`app/intelligence/sponsorship.py` and `app/matching/filters/constants.py`,
each with a comment telling the next reader to keep them in sync). Nothing
tested that they agreed, and they disagree in the worst possible direction:
the filter HARD-BLOCKS on its copy (`rule_filter.py`, `passed=False`,
`score_override=10`) while the card only colours a badge. A phrase in one and
not the other means the board and the filter tell the user different things
about the same posting.

This module is deliberately import-light — only `re` — because
`app/intelligence/sponsorship.py` is imported on the write path and must not
pull in the filters package (whose `__init__` drags in numpy).

## Why matching is not `phrase in description`

Both copies tested plain substring containment on the lowercased posting. That
reads a refusal out of sentences that state the opposite:

    "There is no sponsorship requirement for this role."   -> "no sponsorship"
    "We consider candidates with or without sponsorship."  -> "without sponsorship"
    "You do not need to be a US citizen or permanent resident."

For a user who needs sponsorship, each of those made `rule_filter` drop a job
they were fully eligible for — silently, before scoring, so it never appeared
on the board at all. That is the exact population the product exists to serve.

So: match per SENTENCE, on word boundaries, and let an explicit canceller list
veto a match whose own sentence shows it is not a refusal. Cancellers are
deliberately conservative — each one is a construction that cannot be read as a
refusal. Ambiguous phrasing ("this role does not require sponsorship", which
usually means "we want someone who already has status") is left alone and
still counts as a refusal.
"""
from __future__ import annotations

import html
import re
from typing import List, NamedTuple, Optional

# Explicit, unambiguous refusals. A posting containing one of these — in a
# sentence no canceller vetoes — will not sponsor THIS role regardless of the
# employer's overall visa record, so the rule filter may hard-block on it.
#
# Only EXPLICIT refusals belong here. Ambiguous right-to-work boilerplate
# ("must be authorized to work in...") appears in postings from employers that
# DO sponsor, and OPT/EAD holders ARE authorized to work, so it lives in
# WORK_AUTH_BOILERPLATE (filters/constants.py) and never hard-blocks.
NO_SPONSORSHIP_HARD: List[str] = [
    "not offer visa sponsorship",
    "unable to sponsor",
    "do not sponsor",
    "will not sponsor",
    "cannot sponsor",
    "no visa sponsorship",
    "no sponsorship",
    "does not sponsor",
    "must be us citizen",
    "us citizen or permanent resident",
    "us citizenship required",
    "active security clearance required",
    "must hold an active secret",
    "must possess an active ts/sci",
    # International phrasings — explicit refusals are universal even though the
    # H-1B intelligence built on top of this is US-specific.
    "without sponsorship",  # "...authorized/eligible to work without sponsorship"
    "without the need for sponsorship",
    "without visa sponsorship",
    "citizens only",
    "permanent residents only",
    "unable to provide visa sponsorship",
    "not able to sponsor",
]

# A canceller vetoes a refusal match found in the SAME sentence. Every entry
# here must be a construction that cannot be read as a refusal — when in doubt,
# leave it out and let the phrase count. A false negative here shows the user a
# scarier badge than the posting deserves; a false positive drops an eligible
# job before it is ever scored.
_CANCELLER_SOURCES = [
    # "there is no sponsorship requirement", "no sponsorship restrictions"
    r"\bno\b[^.]{0,24}\bsponsorship\b[^.]{0,24}\b(requirements?|restrictions?|constraints?)\b",
    # "open to candidates with or without sponsorship"
    r"\bwith or without\b[^.]{0,30}\bsponsorship\b",
    # "visa sponsorship is available / offered / provided / supported"
    r"\bsponsorship\b[^.]{0,20}\b(is|are)\s+(available|offered|provided|supported)\b",
    # "we offer/provide/support visa sponsorship"
    r"\bwe\s+(do\s+)?(offer|provide|support)\b[^.]{0,24}\bsponsorship\b",
    # "we will sponsor" / "we can sponsor" — note this cannot match "will not
    # sponsor" or "cannot sponsor", which are themselves refusal phrases.
    r"\b(will|can|do)\s+sponsor\b",
    # "eligible for visa sponsorship" — but never "NOT (be) eligible for…",
    # which is a refusal (it used to veto itself through this line).
    r"(?<!not\s)(?<!not\sbe\s)\beligible\s+for\s+(visa\s+)?sponsorship\b",
    # A citizenship or clearance line that is a preference, or waived:
    # "Active Secret clearance preferred", "No clearance required".
    r"\b(clearance|citizen\w*)\b[^.]{0,40}\b(preferred|a\s+plus|nice[\s-]to[\s-]have|desired"
    r"|desirable|bonus|helpful|not\s+required|not\s+necessary)\b",
    r"\b(preferred|a\s+plus|nice[\s-]to[\s-]have|bonus)\b[^.]{0,30}\b(clearance|citizen\w*)\b",
    r"\b(no|not|without)\s+(\w+\s+){0,2}(clearance|citizenship)\s+(is\s+)?(required|needed|necessary)\b",
    # "you do not need to be a US citizen or permanent resident"
    r"\b(do not|does not|don't|doesn't)\s+(need|have)\s+to\s+be\b",
]
_CANCELLERS = [re.compile(p, re.IGNORECASE) for p in _CANCELLER_SOURCES]

# Refusals and requirements no fixed phrase can spell — each is a REQUIREMENT
# or a REFUSAL construction, never a mere mention ("citizenship status" in EEO
# boilerplate matches none of them). Added 2026-10-28 after two delivered jobs
# for an F-1 OPT user: "U.S. Citizenship with eligibility for DoD Secret
# clearance" and "does not intend to hire … candidates who will require
# sponsorship now OR in the future (i.e. F-1 visa holders)". A U.S. security
# clearance itself requires U.S. citizenship, so a required one is a refusal
# for anyone who needs sponsorship.
_REFUSAL_PATTERN_SOURCES = [
    ("not eligible for sponsorship",
     r"\b(?:not\s+(?:be\s+)?eligible|ineligible)\s+for\s+(?:visa\s+|employment\s+|immigration\s+)?sponsorship\b"),
    ("sponsorship not available",
     r"\bsponsorship\s+(?:is|are|will|can|would)\s*not\s+(?:be\s+)?(?:available|offered|provided|possible|supported)\b"),
    ("sponsorship not available", r"\bsponsorship\s+(?:is\s+)?unavailable\b"),
    ("unable to offer sponsorship",
     r"\b(?:unable|not\s+able|not\s+in\s+a\s+position)\s+to\s+(?:offer|provide|support)\s+(?:\w+\s+){0,2}sponsorship\b"),
    ("will not hire candidates who need sponsorship",
     r"\b(?:does|do|will|would)\s+not\s+(?:intend\s+to\s+|be\s+able\s+to\s+)?(?:hire|consider|accept|employ)\b[^.]{0,100}\bsponsorship\b"),
    ("candidates needing sponsorship not considered",
     r"\b(?:requir\w*|need\w*)\s+(?:\w+\s+){0,2}sponsorship\b[^.]{0,80}"
     r"\b(?:will\s+not|won't|cannot|can't|are\s+not|is\s+not)\s+(?:be\s+)?(?:considered|eligible)\b"),
    ("without requiring sponsorship",
     r"\bwithout\s+(?:requiring|needing|the\s+need\s+(?:of|for)|need\s+of)\s+(?:\w+\s+){0,2}sponsorship\b"),
    # "This role does not qualify for employer-sponsored work authorization."
    ("role does not qualify for sponsorship",
     r"\b(?:does|do|will)\s+not\s+qualify\s+for\s+(?:\w+[-\s])?sponsor\w*"),
    # "Nationwide does not participate in the STEM OPT Extension program."
    ("does not participate in STEM OPT",
     r"\b(?:does|do|will)\s+not\s+(?:participate\s+in|support|accept)\s+(?:the\s+)?(?:stem\s+)?opt\b"),
    # Export control: "applicant must be a (i) U.S. citizen or national, (ii)
    # U.S. lawful, permanent resident …" — the same meaning as the phrase "us
    # citizen or permanent resident" above, written as an ITAR list.
    ("US persons only (export control)",
     r"\bmust\s+(?:be|qualify\s+as)\s+(?:an?\s+)?(?:\(i\)\s*)?(?:us|united\s+states)\s+"
     r"(?:citizen\s+or\s+(?:national|permanent\s+resident)|persons?)\b"),
    ("must be a US citizen",
     r"\bmust\s+(?:be|hold|possess|have)\s+(?:a\s+|an\s+)?(?:us|united\s+states)\s+citizen"),
    # Not the employer-wide line "…required for all positions with a government
    # clearance": that describes OTHER roles; this one's own clearance line, if
    # it has one, is caught below.
    ("US citizenship required",
     r"\b(?:us|united\s+states)\s+citizenship\s+(?:is\s+)?(?:required|mandatory|needed|a\s+requirement)\b"
     r"(?!\s+for\s+(?:all\s+|any\s+|certain\s+)?(?:positions|roles|jobs)\s+(?:with|that|requiring|which))"),
    # "requires" — never "may/might/can/could require" (a hedge, not a rule).
    ("requires US citizenship",
     r"(?<!may\s)(?<!might\s)(?<!can\s)(?<!could\s)\b(?:requires?|requiring)\s+(?:\w+\s+){0,2}"
     r"(?:us|united\s+states)\s+citizenship\b"),
    # A requirements bullet that IS the line: "U.S. Citizenship with …".
    ("US citizenship requirement",
     r"^(?:us|united\s+states)\s+citizenship\b(?!\s+(?:status|and\s+immigration|is\s+not|not\b))"
     r"(?![^.]{0,40}\bfor\s+(?:all\s+|any\s+|certain\s+)?(?:positions|roles|jobs)\s+(?:with|that|requiring|which))"),
    # Not "only US citizens AND lawfully authorized non-US citizens".
    ("US citizens only",
     r"\b(?:only\s+(?:us|united\s+states)\s+citizens(?!\s+(?:and|or)\b)"
     r"|(?:us|united\s+states)\s+citizens\s+only)\b"),
    ("active security clearance",
     r"\b(?:active|current)\s+(?:dod\s+)?(?:secret|top\s+secret|ts/sci|ts|security)\s+(?:security\s+)?clearance\b"),
    ("ability to obtain a security clearance",
     r"\b(?:ability|able|eligib\w*|willing\w*)\s+to\s+(?:obtain|acquire|get)\s+(?:and\s+maintain\s+)?(?:an?\s+)?"
     r"(?:dod\s+|us\s+|federal\s+|government\s+)?(?:secret|top\s+secret|ts/sci|ts|security)\s+(?:security\s+)?clearance\b"),
    ("eligibility for a security clearance",
     r"\beligib\w*\s+(?:for|to\s+obtain)\s+(?:an?\s+)?(?:dod\s+|us\s+|federal\s+)?"
     r"(?:secret|top\s+secret|ts/sci|ts|security)\s+(?:security\s+)?clearance\b"),
    ("security clearance required",
     r"\b(?:secret|top\s+secret|ts/sci|security)\s+clearance\s+(?:is\s+)?(?:required|mandatory|needed)\b"),
]

# Word-boundary matchers, built once. `\b` on both ends stops "no sponsorship"
# matching inside a longer token and keeps the phrase list readable as plain
# text rather than as regex source.
_PHRASE_RES = [(p, re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE))
               for p in NO_SPONSORSHIP_HARD] + \
              [(label, re.compile(src, re.IGNORECASE)) for label, src in _REFUSAL_PATTERN_SOURCES]

# Sentence-ish boundaries. Postings are as often bullet lists with newlines as
# they are prose, so newlines, bullets and pipes end a "sentence" too.
_SENT_SPLIT = re.compile(r"[.!?;\n\r•·|]+")


class Refusal(NamedTuple):
    """A matched refusal, with the sentence it came from so the UI can quote it."""
    phrase: str
    sentence: str


# Abbreviations whose periods are not sentence ends: split on them and "Must
# be a U.S. citizen" became "Must be a U", "S", "citizen" — no phrase could
# ever match the most common way U.S. postings state the requirement.
_ABBREVIATIONS = [(re.compile(r"\bU\.\s?S\.\s?A\.", re.IGNORECASE), "USA"),
                  (re.compile(r"\bU\.\s?S\.", re.IGNORECASE), "US"),
                  (re.compile(r"\bi\.e\.", re.IGNORECASE), "ie"),
                  (re.compile(r"\be\.g\.", re.IGNORECASE), "eg")]


_TAG_RE = re.compile(r"</?[A-Za-z][^<>]{0,300}>")
#: A line broken mid-sentence: "This role \ndoes not\n qualify for …" is one
#: sentence, and splitting on its newlines hid the refusal (Nationwide,
#: 2026-10-28). Only after a word no line can end on — callers pass lowercased
#: text, so the case of the next line cannot tell a wrap from a new bullet.
_SOFT_WRAP_RE = re.compile(
    r"\b(not|no|the|a|an|to|for|of|and|or|is|are|be|will|would|does|do|can|cannot|must|"
    r"may|in|on|with|without|requires?|eligible|unable)[ \t]*\r?\n[ \t]*(?=[a-z])", re.I)


def _sentences(text: str) -> List[str]:
    # Markup is not text: "<strong>not</strong> eligible for sponsorship" and
    # "not&nbsp;eligible" are the same sentence as the plain one.
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    # One space between words, or the cancellers' "not " look-behinds miss
    # "not\xa0 eligible" and veto the refusal they exist to spare.
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = _SOFT_WRAP_RE.sub(r"\1 ", text)
    for rx, repl in _ABBREVIATIONS:
        text = rx.sub(repl, text)
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def find_refusal(text: str) -> Optional[Refusal]:
    """The first genuine refusal in `text`, or None.

    Returns the matched phrase and the sentence containing it — the sentence is
    what makes the claim inspectable ("this posting says: <quote>") instead of
    an unexplained red badge.
    """
    if not text:
        return None
    for sentence in _sentences(text):
        for phrase, rx in _PHRASE_RES:
            if not rx.search(sentence):
                continue
            if any(c.search(sentence) for c in _CANCELLERS):
                break  # this sentence is positive — try the next sentence
            return Refusal(phrase=phrase, sentence=sentence[:400])
    return None


def refuses(text: str) -> bool:
    """True when the posting explicitly refuses sponsorship."""
    return find_refusal(text) is not None
