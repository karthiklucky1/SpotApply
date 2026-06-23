"""Urgency / timing intelligence.

Agencies win partly on *timing* — the first 48 hours of a posting have the
highest interview odds, and a role left open for weeks signals a company that is
struggling to fill it (and will move fast on a good applicant). We can read both
from lifecycle data we already store on every Job: ``posted_at`` / ``first_seen``
/ ``last_seen`` / ``is_closed`` / ``ghost_score`` — no new tables, no scraping.
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


def _age_days(dt) -> int | None:
    """Whole days since ``dt`` (UTC, tolerant of tz-aware/naive)."""
    if not dt:
        return None
    try:
        if getattr(dt, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=None)
        return (datetime.utcnow() - dt).days
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


def assess(job) -> UrgencyAssessment:
    """Score how time-sensitive / high-opportunity this posting is."""
    if bool(getattr(job, "is_closed", False)):
        return UrgencyAssessment(0, "", "", "normal")

    posted = (getattr(job, "posted_at", None)
              or getattr(job, "first_seen", None)
              or getattr(job, "discovered_at", None))
    age = _age_days(posted)
    open_span = _span_days(getattr(job, "first_seen", None), getattr(job, "last_seen", None))
    ghost = getattr(job, "ghost_score", 0) or 0

    # Just posted — the highest-response window ("zero-minute application").
    if age is not None and age <= 2:
        return UrgencyAssessment(
            92, "🆕 Just posted",
            "Posted in the last 48 hours — the highest-response window. Applying now "
            "maximizes your interview odds.", "hot",
        )
    if age is not None and age <= 6:
        return UrgencyAssessment(
            68, "🆕 Fresh",
            "Posted within the last week — still early, ahead of most applicants.",
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
