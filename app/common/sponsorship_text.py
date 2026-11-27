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
    # 60, not 30: "Nice to have: active TS/SCI U.S. Government Security
    # Clearance" puts 39 characters between the two once _CLEARANCE reads the
    # qualifiers, and a preference must stay a preference.
    r"\b(preferred|a\s+plus|nice[\s-]to[\s-]have|bonus)\b[^.]{0,60}\b(clearance|citizen\w*)\b",
    r"\b(no|not|without)\s+(\w+\s+){0,2}(clearance|citizenship)\s+(is\s+)?(required|needed|necessary)\b",
    # "you do not need to be a US citizen or permanent resident"
    r"\b(do not|does not|don't|doesn't)\s+(need|have)\s+to\s+be\b",
    # A hedge is not a refusal: "we aren't able to sponsor visas for every role
    # and every candidate", "we can't guarantee sponsorship", "assessed on an
    # individual basis". The employer sponsors; this role may be one it does.
    r"\b(for|in)\s+(every|all)\s+(roles?|positions?|candidates?|cases?)\b",
    r"\bguarantee\w*\b|\bcase[- ]by[- ]case\b|\bindividual\s+basis\b",
    # The opposite of a refusal: "we hire without regard to visa status or
    # sponsorship needs", "regardless of whether you need sponsorship"; and a
    # question, not an answer: "ask whether or not we sponsor this role".
    r"\bwithout\s+regard\b|\bregardless\s+of\b|\bwhether\s+or\s+not\b",
    # "…but supports STEM OPT", "we welcome OPT/CPT candidates" — never after a
    # negation ("unable to support H-1B", "does not accept OPT").
    r"(?<!to\s)(?<!not\s)(?<!n't\s)(?<!never\s)(?<!longer\s)"
    r"\b(supports?|accepts?|welcomes?|sponsors?)\s+([\w/-]+\s+){0,3}(stem\s+opt|opt|cpt|h-?1-?b)\b",
]
_CANCELLERS = [re.compile(p, re.IGNORECASE) for p in _CANCELLER_SOURCES]

# Any way of saying "not": an auxiliary + not, a contraction, "unable to",
# "no longer". The patterns below put it in front of the verb it governs, with
# only a few WORDS between (a comma ends the reach, so "if you are not a US
# citizen, we sponsor visas" is not a negated "sponsor").
_NEG = (r"(?:(?:do|does|did|will|would|can|could|shall|is|are|am)\s+not|cannot|can't|won't|"
        r"don't|doesn't|isn't|aren't|wouldn't|unable\s+to|not\s+able\s+to|"
        r"not\s+in\s+a\s+position\s+to|no\s+longer|not)")
# The statuses a refusal can name instead of the word "sponsorship".
_VISA = r"(?:stem\s+opt|opt|cpt|h-?1-?b|h1-b)"

# Refusals and requirements no fixed phrase can spell — each is a REQUIREMENT
# or a REFUSAL construction, never a mere mention ("citizenship status" in EEO
# boilerplate matches none of them). Added 2026-10-28 after two delivered jobs
# for an F-1 OPT user: "U.S. Citizenship with eligibility for DoD Secret
# clearance" and "does not intend to hire … candidates who will require
# sponsorship now OR in the future (i.e. F-1 visa holders)". A U.S. security
# clearance itself requires U.S. citizenship, so a required one is a refusal
# for anyone who needs sponsorship.
#
# A clearance level, then the qualifiers that may sit between it and the word
# "clearance". "able to obtain a TS/SCI U.S. Government Security Clearance"
# (CACI, delivered at 82 on 2026-10-29) put three there and read as no
# requirement at all. Known qualifier words only, so "a security badge and
# clearance" is still not a clearance.
_CLEARANCE = (
    r"(?:secret|top[\s-]+secret|ts/sci|ts|security)"
    r"(?:\s+(?:us|government|federal|dod|doe|level|\(ts\)|sci|with|polygraph|poly|"
    r"full[\s-]scope|ci|or|top|secret|security|interim|final)){0,4}"
    r"\s+clearance\b")
_REFUSAL_PATTERN_SOURCES = [
    # ── Refusals by STRUCTURE (2026-11-24) ───────────────────────────────────
    # The fixed phrases above and the constructions below them knew 27 of 50
    # ways production postings refuse sponsorship (one user's pool, 4,000
    # postings, every sentence naming sponsorship or OPT beside a negation).
    # "Will not provide sponsorship for employment visas or participate in STEM
    # OPT" was none of them, and that posting reached an F-1 OPT user's board
    # at 88. An employer has unlimited wordings and five things to say, so the
    # five are matched instead: the verb "sponsor" negated; sponsorship not
    # offered; sponsorship not available; work "without … sponsorship"; and
    # people who need it not wanted. tests/test_sponsorship_wordings.py holds
    # the corpus, refusals and look-alikes both.
    ("will not sponsor",
     rf"\b{_NEG}\s+(?:[\w-]+\s+){{0,4}}?sponsor(?:ing)?\b"),
    ("does not provide sponsorship",
     rf"\b{_NEG}\s+(?:be\s+)?(?:[\w-]+\s+){{0,2}}?(?:offer\w*|provid\w*|support\w*|extend\w*)\s+"
     r"(?:[\w-]+\s+){0,6}?sponsorship\b"),
    ("sponsorship not available",
     r"\bsponsorship\b[^.]{0,130}?\b(?:(?:is|are|will|can|would)\s+)?(?:currently\s+|presently\s+)?"
     r"(?:not|n't)\s+(?:be\s+)?(?:currently\s+|presently\s+)?(?:available|offered|provided|possible|supported)\b"),
    ("without sponsorship",
     r"\bwithout\s+(?:(?:the|a|any)\s+)?(?:(?:requiring|needing|need(?:\s+(?:of|for))?|"
     r"requirement\s+(?:of|for))\s+)?(?:[\w-]+\s+){0,5}?sponsorship\b"),
    ("must not require sponsorship",
     r"\bnot\s+(?:now\s+or\s+in\s+the\s+future\s+)?(?:requir\w*|need\w*)\s+(?:[\w-]+\s+){0,6}?sponsorship\b"),
    ("candidates who need sponsorship not accepted",
     rf"\b{_NEG}\s+(?:be\s+)?(?:accept\w*|consider\w*|hir\w*|review\w*|interview\w*)\b[^.]{{0,100}}\bsponsorship\b"),
    ("do not apply if you need sponsorship", r"\bnot\s+apply\b[^.]{0,140}\bsponsorship\b"),
    ("not eligible for sponsorship",
     r"\b(?:not\s+(?:be\s+)?eligible|ineligible)\s+for\s+[^.]{0,40}?sponsor"),
    ("does not support OPT / H-1B",
     rf"\b(?:unable|not\s+able)\s+to\s+(?:support|sponsor|accept|hire|consider|employ)\s+"
     rf"(?:[\w/-]+\s+){{0,5}}?{_VISA}\b"),
    ("does not participate in STEM OPT",
     r"\b(?:do|does|will|can|would)\s*(?:not|n't)\s+(?:be\s+)?"
     r"(?:support\w*|accept\w*|consider\w*|participat\w*|provid\w*|offer\w*)\b[^.]{0,100}"
     r"\b(?:stem\s+opt|opt\s+extensions?|opt/cpt|cpt/opt)\b"),
    ("no OPT / CPT / H-1B", r"\bno\s+(?:stem\s+opt|opt/cpt|cpt/opt|opt\s+or\s+cpt|cpt|h-?1-?b|h1-b)\b"),
    ("OPT / H-1B holders not eligible",
     rf"\b(?:{_VISA}|f-?1)\b[^.]{{0,80}}\b(?:are|is)\s+not\s+eligible\b"),
    # "US citizenship or green card only" — the word "only" must close the list
    # itself: "citizenship status is used only to verify eligibility" is the
    # E-Verify notice, not a rule about who may apply.
    ("citizens or green card holders only",
     r"\b(?:citizens?|citizenship|green\s+card(?:\s+holders?)?|permanent\s+residents?)"
     r"(?:\s*(?:or|and|/|&|,)\s*(?:us\s+)?(?:citizens?|citizenship|gc|green\s+card(?:\s+holders?)?|"
     r"permanent\s+residents?))*\s+only\b"),
    # "No relocation or visa sponsorship" — the phrase list knows only the two
    # words side by side.
    ("no sponsorship",
     r"\bno\s+(?:[\w-]+\s+){1,3}?(?:or|and|/|nor)\s+(?:visa\s+|work\s+|employment\s+)?sponsorship\b"),
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
     r"\b(?:active|current)\s+(?:dod\s+)?" + _CLEARANCE),
    # "obtain/maintain", "gain and maintain", "apply for and maintain", and up
    # to two issuers ("US Government TS/SCI ...") are the same requirement.
    ("ability to obtain a security clearance",
     r"\b(?:ability|able|eligib\w*|willing\w*)\s+to\s+(?:obtain|acquire|get|gain|apply\s+for)"
     r"(?:\s*/\s*maintain|\s+and\s+maintain)?\s+(?:an?\s+)?"
     r"(?:(?:dod|us|federal|government)\s+){0,2}" + _CLEARANCE),
    ("eligibility for a security clearance",
     r"\beligib\w*\s+(?:for|to\s+obtain)\s+(?:an?\s+)?(?:(?:dod|us|federal|government)\s+){0,2}" + _CLEARANCE),
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
    # A typographic apostrophe is the same word: "we’re unable", "don’t offer".
    text = text.replace("’", "'").replace("‘", "'")
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
