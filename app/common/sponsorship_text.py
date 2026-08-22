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
    # "eligible for visa sponsorship"
    r"\beligible\s+for\s+(visa\s+)?sponsorship\b",
    # "you do not need to be a US citizen or permanent resident"
    r"\b(do not|does not|don't|doesn't)\s+(need|have)\s+to\s+be\b",
]
_CANCELLERS = [re.compile(p, re.IGNORECASE) for p in _CANCELLER_SOURCES]

# Word-boundary matchers, built once. `\b` on both ends stops "no sponsorship"
# matching inside a longer token and keeps the phrase list readable as plain
# text rather than as regex source.
_PHRASE_RES = [(p, re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE))
               for p in NO_SPONSORSHIP_HARD]

# Sentence-ish boundaries. Postings are as often bullet lists with newlines as
# they are prose, so newlines, bullets and pipes end a "sentence" too.
_SENT_SPLIT = re.compile(r"[.!?;\n\r•·|]+")


class Refusal(NamedTuple):
    """A matched refusal, with the sentence it came from so the UI can quote it."""
    phrase: str
    sentence: str


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


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
