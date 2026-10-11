"""Earliness: ONE reference clock per posting, and a receipt that never flatters.

Why (docs/EARLINESS_RECEIPT.md, 2026-10-11): applications to a posting are
front-loaded — about 41% arrive in the first 48 hours and 56% within four days
(Dice.com job-board data, St. Louis Fed) — and recruiters read in arrival order
and stop once a shortlist forms. SpotApply already finds postings inside that
window (5-minute pulse lane); this module makes the window VISIBLE: which
shortlisted jobs are still inside it today, and, after the user submits, how
early they actually were ("Applied 3h 12m after posting").

Three rules, pinned by tests/test_earliness_receipt.py:

* ONE reference time per posting: the EARLIER of a trusted ATS publish date and
  when we first saw it. A `posted_at` later than `first_seen` is an edit or
  refresh date (Greenhouse `updated_at`, future-dated feeds) and is never used —
  the rule can only make a receipt LESS flattering, never more. Boards whose
  scraper reads no true publish timestamp (Workday, Personio, aggregators) get
  the "found" clock and the copy says so.
* The receipt states a clock, never odds. No "likely", no "Nx more": every
  multiplier in circulation is untraceable to primary data. The one number the
  copy cites is the volume share above, with its source.
* An email-imported application (made outside SpotApply) never gets a receipt:
  its `submitted_at` is the import time.
"""
from __future__ import annotations

from datetime import datetime

# Scrapers that read the board's own publish timestamp (not an edit date):
# greenhouse first_published, lever createdAt, ashby publishedAt,
# smartrecruiters releasedDate, workable published_on.
TRUSTED_POSTED_SOURCES = frozenset({"greenhouse", "lever", "ashby", "smartrecruiters", "workable"})

EVIDENCE_NOTE = (
    "About 41% of applications to a posting arrive in its first 48 hours and 56% "
    "within four days (Dice.com job-board data, St. Louis Fed). Applying early means "
    "fewer competing applications are in the pile when a recruiter first reads it."
)

# Words the earliness and urgency copy must never contain: they turn a volume
# curve into a personal probability nobody has measured.
FORBIDDEN_WORDS = ("odds", "likely", "x more", "×", "times more", "more likely")


def window_hours() -> int:
    try:
        from app.config import settings
        return max(1, int(getattr(settings, "earliness_window_hours", 48) or 48))
    except Exception:
        return 48


def _naive(dt):
    if dt is None:
        return None
    try:
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=None)
    except Exception:
        return None
    return dt


def _source_value(job) -> str:
    src = getattr(job, "source", None)
    return str(getattr(src, "value", src) or "").lower()


def reference(job) -> tuple[datetime | None, str]:
    """The posting's reference time and which clock it is: ('posted' | 'found').

    `found` = first_seen (or discovered_at); `posted` = posted_at, but ONLY for a
    trusted source. The reference is the EARLIER of the two.
    """
    if job is None:
        return None, ""
    found = _naive(getattr(job, "first_seen", None) or getattr(job, "discovered_at", None))
    posted = _naive(getattr(job, "posted_at", None)) if _source_value(job) in TRUSTED_POSTED_SOURCES else None
    if posted is not None and found is not None:
        return (posted, "posted") if posted <= found else (found, "found")
    if posted is not None:
        return posted, "posted"
    if found is not None:
        return found, "found"
    return None, ""


def stamp(application, job, now: datetime | None = None) -> bool:
    """Write the receipt onto an application at submit time. Email imports and
    postings with no reference are left NULL (= unmeasured, never guessed)."""
    if application is None or (getattr(application, "apply_track", "") or "") == "email_import":
        return False
    ref, kind = reference(job)
    if ref is None:
        return False
    now = _naive(now) or datetime.utcnow()
    application.applied_after_min = max(0, int((now - ref).total_seconds() // 60))
    application.applied_ref = kind
    return True


def _human(minutes: int) -> str:
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours, rem = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {rem}m" if rem else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d {h}h" if (h and days < 7) else f"{days}d"


def receipt(application, job=None) -> dict | None:
    """The card chip: {minutes, kind, early, label, detail} or None when there is
    nothing honest to show (email import, unmeasured row)."""
    if application is None or (getattr(application, "apply_track", "") or "") == "email_import":
        return None
    minutes = getattr(application, "applied_after_min", None)
    kind = getattr(application, "applied_ref", None)
    if minutes is None or kind not in ("posted", "found"):
        return None
    minutes = int(minutes)
    early = minutes <= window_hours() * 60
    label = f"Applied {_human(minutes)} after " + ("posting" if kind == "posted" else "we found it")
    clock = ("The posting time is the board's own publish timestamp." if kind == "posted"
             else "This board publishes no trustworthy posting date, so the clock starts when SpotApply first saw the posting.")
    outcome = ("This one went in inside the window." if early
               else "This one went in later; the clock is shown so the next one can be earlier.")
    return {"minutes": minutes, "kind": kind, "early": early, "label": label,
            "detail": f"{EVIDENCE_NOTE} {outcome} {clock}"}


def early_window(job, now: datetime | None = None) -> dict:
    """Is the posting still inside the early window, and how long is left?"""
    ref, kind = reference(job)
    if ref is None:
        return {"in_window": False, "hours_left": 0, "kind": kind}
    now = _naive(now) or datetime.utcnow()
    elapsed_h = (now - ref).total_seconds() / 3600.0
    left = window_hours() - elapsed_h
    in_window = elapsed_h >= 0 and left > 0
    return {"in_window": in_window, "hours_left": int(left) if in_window else 0, "kind": kind}


def copy_is_honest(text: str) -> bool:
    low = (text or "").lower()
    return not any(w in low for w in FORBIDDEN_WORDS)
