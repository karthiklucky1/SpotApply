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

NO_SPONSORSHIP_PATTERNS = [
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
    # International phrasings (kept in sync with app/intelligence/sponsorship.py)
    "must have the right to work",
    "right to work in the",
    "must be eligible to work in",
    "eligible to work without sponsorship",
    "without the need for sponsorship",
    "without visa sponsorship",
    "must be authorised to work",
    "must be authorized to work in",
    "work permit required",
    "valid work permit",
    "eu work permit",
    "citizens only",
    "permanent residents only",
    "unable to provide visa sponsorship",
    "not able to sponsor",
]

STAFF_TITLES = [
    "staff", "principal", "director", "vp", "head of", "engineering manager", "lead software engineer"
]
