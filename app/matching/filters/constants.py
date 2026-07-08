# Shared constants for job matching and pre-filtering stages
# (Country/location detection lives in app.common.geo — used by discovery,
# the rule filter, and retrieval so all stages agree.)

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
