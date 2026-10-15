"""Urgency / timing intelligence.

Timing is read from lifecycle data we already store on every Job: ``posted_at``
/ ``first_seen`` / ``last_seen`` / ``is_closed`` / ``ghost_score`` — no new
tables, no scraping. The early window comes from ONE reference clock
(``app.strategy.earliness.reference``: the earlier of a trusted publish date and
when we first saw the posting), so a Greenhouse edit date can never make a stale
posting read as hot. The copy states what is measured — a posting's
applications are front-loaded (about 41% in the first 48 hours, Dice data via
the St. Louis Fed) — and never a personal probability: no "odds", no "likely",
no multiplier (pinned by tests/test_earliness_receipt.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class UrgencyAssessment:
    score: int      # 0-100 (used as a ranking tiebreak)
    label: str      # short badge text ("" = nothing worth showing)
    reason: str
    tone: str       # 'hot' | 'fresh' | 'normal'


def _age_days(dt, now: datetime | None = None) -> int | None:
    """Whole days from ``dt`` to ``now`` (default: the current UTC time;
    tolerant of tz-aware/naive)."""
    if not dt:
        return None
    try:
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=None)
        if getattr(now, "tzinfo", None) is not None:
            now = now.replace(tzinfo=None)
        return ((now or datetime.utcnow()) - dt).days
    except Exception:
        return None


def _span_days(a, b) -> int | None:
    if not a or not b:
        return None
    try:
        if getattr(a, "tzinfo", None) is not None:
            a = a.replace(tzinfo=None)
        if getattr(b, "tzinfo", None) is not None:
            b = b.replace(tzinfo=None)
        return (b - a).days
    except Exception:
        return None


def assess(job, now: datetime | None = None) -> UrgencyAssessment:
    """Score how time-sensitive / high-opportunity this posting is.

    Ages are measured to ``now`` (default: the current UTC time); tests pin it
    so the early window does not drift with the wall clock."""
    if bool(getattr(job, "is_closed", False)):
        return UrgencyAssessment(0, "", "", "normal")

    from app.strategy import earliness
    posted, ref_kind = earliness.reference(job)
    if posted is None:
        posted = getattr(job, "discovered_at", None)
    age = _age_days(posted, now)
    open_span = _span_days(getattr(job, "first_seen", None), getattr(job, "last_seen", None))
    ghost = getattr(job, "ghost_score", 0) or 0

    # Inside the early window (default 48h from the reference clock): the hours
    # left are shown so "apply today" is a measured statement, not a mood. A
    # board with no trustworthy publish date says the clock started when WE
    # found the posting, and attributes nothing to the board.
    window = earliness.early_window(job, now)
    if window["in_window"]:
        left = window["hours_left"]
        since = ("Posted" if ref_kind == "posted"
                 else "Found by SpotApply (this board publishes no trustworthy posting date)")
        return UrgencyAssessment(
            92, f"⚡ Early window · {left}h left",
            f"{since} under {earliness.window_hours()}h ago. {earliness.EVIDENCE_NOTE}",
            "hot",
        )
    if age is not None and age <= 6:
        return UrgencyAssessment(
            68, "🆕 Fresh",
            "Posted within the last week. Most of a posting's applications arrive in its "
            "first few days, so there is still time to be among the earlier ones.",
            "fresh",
        )

    # Open a long time and not a likely ghost → genuinely hard to fill.
    if open_span is not None and open_span >= 21 and ghost < 0.5:
        return UrgencyAssessment(
            74, "🔥 Hard to fill",
            f"Still active after {open_span}+ days — the company is struggling to "
            "fill this and tends to move fast on a strong applicant.", "hot",
        )

    if age is not None and age <= 14:
        return UrgencyAssessment(35, "", "", "normal")
    return UrgencyAssessment(15, "", "", "normal")
