"""Interaction learning — the user's own actions tune their matching.

Every dismissal (✕ on a card → SKIPPED with the user_dismissed marker) and
every engagement (tailored / autofilled / submitted / interviewing / offer)
is a training signal, the way Otta/Welcome-to-the-Jungle learn from saves and
applies. From that history we derive a lightweight per-user profile:

  - companies the user keeps dismissing (and never engaged with) → sink
  - companies the user engaged with → boost
  - distinctive title words that keep getting dismissed / engaged → nudge

The profile produces (a) a deterministic score adjustment used when ranking
the shortlist, and (b) a short natural-language note injected into the LLM
reranker prompt so scoring itself calibrates to revealed preferences.

System-generated SKIPPEDs (company-cap expiry, ghost closes) are NOT the
user's opinion and are excluded via their notes markers.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job

log = logging.getLogger(__name__)

# Appended to Application.notes by the /skip endpoint for real user dismissals.
USER_DISMISS_MARKER = "user_dismissed"

# System paths that also set SKIPPED — never count these as user opinion.
_SYSTEM_SKIP_HINTS = ("expired after", "job closed", "removed from company",
                      "slot reopened", "dead at shortlist", "deactivated")

_ENGAGED_STATUSES = {
    ApplicationStatus.TAILORED, ApplicationStatus.AUTOFILLED,
    ApplicationStatus.AWAITING_USER, ApplicationStatus.READY_TO_SUBMIT,
    ApplicationStatus.SUBMITTED, ApplicationStatus.INTERVIEWING,
    ApplicationStatus.OFFER, ApplicationStatus.ACCEPTED,
}

# How many times a company must be dismissed (with zero engagement) to sink.
DISMISS_COMPANY_THRESHOLD = 2


def _title_tokens(title: str) -> list[str]:
    from app.discovery.title_filter import _GENERIC_TOKENS
    import re
    toks = re.split(r"[^a-z0-9+#]+", (title or "").lower())
    return [t for t in toks if len(t) >= 4 and t not in _GENERIC_TOKENS]


@dataclass
class PreferenceProfile:
    disliked_companies: set = field(default_factory=set)
    liked_companies: set = field(default_factory=set)
    disliked_tokens: Counter = field(default_factory=Counter)
    liked_tokens: Counter = field(default_factory=Counter)
    dismissed_total: int = 0
    engaged_total: int = 0

    @property
    def has_signal(self) -> bool:
        return self.dismissed_total >= 1 or self.engaged_total >= 1

    def adjustment(self, company: str | None, title: str | None) -> float:
        """Deterministic priority delta, roughly -25..+12 on the 0-100 scale."""
        score = 0.0
        c = (company or "").strip().lower()
        if c and c in self.disliked_companies:
            score -= 25.0
        elif c and c in self.liked_companies:
            score += 6.0
        toks = _title_tokens(title or "")
        dis_hits = sum(1 for t in toks if self.disliked_tokens.get(t, 0) >= 2)
        like_hits = sum(1 for t in toks if self.liked_tokens.get(t, 0) >= 2)
        score += min(like_hits, 2) * 3.0 - min(dis_hits, 3) * 6.0
        return score

    def feedback_note(self) -> str:
        """Short prompt block for the LLM reranker; '' when nothing learned."""
        if not self.has_signal:
            return ""
        parts: list[str] = []
        if self.disliked_companies:
            parts.append("repeatedly dismissed jobs at: "
                         + ", ".join(sorted(self.disliked_companies)[:5]))
        dis = [t for t, n in self.disliked_tokens.most_common(6) if n >= 2]
        if dis:
            parts.append("tends to dismiss roles mentioning: " + ", ".join(dis))
        like = [t for t, n in self.liked_tokens.most_common(6) if n >= 2]
        if like:
            parts.append("engages with roles mentioning: " + ", ".join(like))
        if not parts:
            return ""
        return ("Revealed preferences from this candidate's own actions — "
                "weigh them when scoring fit: the candidate " + "; ".join(parts) + ".")


def _is_user_dismissal(app: Application) -> bool:
    if app.status != ApplicationStatus.SKIPPED:
        return False
    notes = (app.notes or "").lower()
    if USER_DISMISS_MARKER in notes:
        return True
    # Legacy dismissals predate the marker: count them only when no system
    # hint explains the skip.
    return not any(h in notes for h in _SYSTEM_SKIP_HINTS)


def build_preference_profile(user_id: str | None) -> PreferenceProfile:
    """Derive the user's revealed preferences from their application history.
    Cheap (one join query); safe to call per matching pass / dashboard render."""
    prof = PreferenceProfile()
    try:
        with get_session() as session:
            rows = session.exec(
                select(Application, Job)
                .join(Job, Application.job_id == Job.id)
                .where(Job.user_id == user_id)
            ).all()
    except Exception as e:
        log.debug("preference profile query failed for %s: %s", user_id, e)
        return prof

    dismissed_by_company: Counter = Counter()
    engaged_companies: set = set()
    for app, job in rows:
        company = (job.company or "").strip().lower()
        if app.status in _ENGAGED_STATUSES:
            prof.engaged_total += 1
            if company:
                engaged_companies.add(company)
            prof.liked_tokens.update(set(_title_tokens(job.title)))
        elif _is_user_dismissal(app):
            prof.dismissed_total += 1
            if company:
                dismissed_by_company[company] += 1
            prof.disliked_tokens.update(set(_title_tokens(job.title)))

    prof.liked_companies = engaged_companies
    prof.disliked_companies = {
        c for c, n in dismissed_by_company.items()
        if n >= DISMISS_COMPANY_THRESHOLD and c not in engaged_companies
    }
    # A token the user also engages with is not a dislike signal.
    for t in list(prof.disliked_tokens):
        if prof.liked_tokens.get(t, 0) >= prof.disliked_tokens[t]:
            del prof.disliked_tokens[t]
    return prof
