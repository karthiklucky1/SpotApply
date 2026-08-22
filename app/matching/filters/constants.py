# Shared constants for job matching and pre-filtering stages
# (Country/location detection lives in app.common.geo — used by discovery,
# the rule filter, and retrieval so all stages agree.)

# Source quality weights — used to decide which candidates get the (limited)
# LLM rerank budget. Direct ATS postings are live, deduplicated at origin, and
# link straight to the application form; redirect aggregators are the noisiest.
SOURCE_QUALITY: dict[str, float] = {
    # Direct ATS — live open/close, direct apply links
    "greenhouse": 1.0, "lever": 1.0, "ashby": 1.0, "smartrecruiters": 1.0,
    "workday": 1.0, "workable": 1.0, "bamboohr": 1.0, "teamtailor": 1.0,
    # First-party boards / high-signal aggregators
    "serpapi": 0.9, "linkedin": 0.9, "indeed": 0.9, "crowdsourced": 0.9,
    "wellfound": 0.85, "otta": 0.85,
    # Remote-only feeds — high competition, mixed freshness
    "remotive": 0.75, "remoteok": 0.75, "themuse": 0.75, "arbeitnow": 0.75,
    "jobicy": 0.75, "weworkremotely": 0.75, "indeed_rss": 0.75,
    # Redirect aggregators — links bounce through their pages
    "adzuna": 0.6, "reed": 0.6, "jooble": 0.6,
}
DEFAULT_SOURCE_QUALITY = 0.8
FRESH_POSTING_BONUS = 1.15   # priority multiplier for postings < 48h old
FRESH_POSTING_HOURS = 48


def source_quality(source) -> float:
    """Quality weight for a Job.source (enum or raw string)."""
    key = getattr(source, "value", source)
    return SOURCE_QUALITY.get(str(key or "").lower(), DEFAULT_SOURCE_QUALITY)

# Explicit, unambiguous refusals — a posting containing one of these will not
# sponsor THIS role regardless of the employer's overall visa record, so the
# rule filter may hard-block on them for sponsorship-needing users.
#
# The list itself now lives in app/common/sponsorship_text.py, imported by BOTH
# this module and app/intelligence/sponsorship.py. It used to be duplicated in
# the two files with a "keep in sync" comment on each and no test that they
# agreed. Do not re-inline it here: the card and the filter disagreeing about
# the same posting is the failure that split it out.
#
# Matching is NOT `phrase in description` any more — use find_refusal(), which
# scopes the match to one sentence and vetoes positives like "there is no
# sponsorship requirement for this role". Plain containment on that sentence
# hard-blocked jobs the user was fully eligible for.
from app.common.sponsorship_text import (  # noqa: E402,F401  (re-export)
    NO_SPONSORSHIP_HARD, find_refusal, refuses,
)

# Ambiguous right-to-work boilerplate. Employers that DO sponsor put these
# lines in postings too (and OPT/EAD holders ARE authorized to work), so these
# must never hard-block — the LLM reranker judges them against the full posting
# plus the employer's public sponsorship record (_sponsor_note).
WORK_AUTH_BOILERPLATE = [
    "must have the right to work",
    "right to work in the",
    "must be eligible to work in",
    "must be authorised to work",
    "must be authorized to work in",
    "work permit required",
    "valid work permit",
    "eu work permit",
]

# Combined list for consumers that only need "mentions work authorization".
NO_SPONSORSHIP_PATTERNS = NO_SPONSORSHIP_HARD + WORK_AUTH_BOILERPLATE

STAFF_TITLES = [
    "staff", "principal", "director", "vp", "head of", "engineering manager", "lead software engineer"
]
