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

Only a SKIPPED that carries the user_dismissed marker is the user's opinion.
Every system path that sets SKIPPED (slate replacement, company-cap
displacement, freshness auto-removal, closed postings) is not, whatever its
note says — see ``_is_user_dismissal``.
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

_ENGAGED_STATUSES = {
    ApplicationStatus.TAILORED, ApplicationStatus.AUTOFILLED,
    ApplicationStatus.AWAITING_USER, ApplicationStatus.READY_TO_SUBMIT,
    ApplicationStatus.SUBMITTED, ApplicationStatus.INTERVIEWING,
    ApplicationStatus.OFFER, ApplicationStatus.ACCEPTED,
}

# How many times a company must be dismissed (with zero engagement) to sink.
DISMISS_COMPANY_THRESHOLD = 2


def _title_tokens(title: str) -> list[str]:
    # STRUCTURAL words only. The routing filter also treats domain words
    # ("sales", "data", "software") as generic, because as standalone
    # routing terms they match whole unrelated professions — but here they
    # are the signal: "sales" in two dismissed titles is precisely what the
    # user is telling us.
    from app.discovery.title_filter import _STRUCTURAL_TOKENS as _GENERIC_TOKENS
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


def _is_user_dismissal(app) -> bool:
    """``app`` needs only ``.status`` and ``.notes`` (an Application or a projected row).

    The marker is the ONLY evidence. This used to count every SKIPPED whose
    note matched none of a list of system phrases, and the list fell behind
    the writers: "slate_replaced: ...", "Auto-removed from shortlist: ..." and
    "Displaced by higher-scoring ... under the per-company cap" matched
    nothing. Production 2026-10-30: 17,432 system skips across 23 users were
    learned as dismissals against ONE marked one, and a user who had
    dismissed nothing was scored as someone who "dismisses roles mentioning
    software, backend, full stack" — a backend role offering sponsorship came
    back 32 and three companies were barred from the board.
    """
    if app.status != ApplicationStatus.SKIPPED:
        return False
    return USER_DISMISS_MARKER in (app.notes or "").lower()


# The profile is read on EVERY /dashboard render and every matching pass.
# Cached per user for this long; any meaningful action (a skip, an apply — the
# very things it learns from) invalidates it (server._get_user_id).
PREFERENCE_CACHE_SECONDS = 300


def _preference_cache_key(user_id: str | None) -> str:
    return f"pref:{user_id or 'local'}"


class _Row:
    """The four columns the profile reads, detached from any session."""
    __slots__ = ("company", "notes", "status", "title")

    def __init__(self, status, notes, company, title):
        self.status, self.notes, self.company, self.title = status, notes, company, title


def build_preference_profile(user_id: str | None) -> PreferenceProfile:
    """Derive the user's revealed preferences from their application history.

    PROJECTED and CACHED (2026-10-10): this used to `select(Application, Job)`
    over every application the user ever had — whole rows, full descriptions,
    reasoning and insights — on every dashboard render, with no limit, timeout
    or cache. One account holds 16k SKIPPED rows; loading them from a
    cross-region database was the single largest cost of a /dashboard render
    (p50 11-18 s over the last week). The profile reads four columns; it now
    selects four columns, and the result is cached for PREFERENCE_CACHE_SECONDS.
    """
    from app.common import ttl_cache
    try:
        return ttl_cache.get_or_compute(
            _preference_cache_key(user_id), PREFERENCE_CACHE_SECONDS,
            lambda: _build_preference_profile_uncached(user_id))
    except Exception as e:                       # never fail a render over a hint
        log.debug("preference profile unavailable for %s: %s", user_id, e)
        return PreferenceProfile()


def invalidate_preference_profile(user_id: str | None) -> None:
    from app.common import ttl_cache
    ttl_cache.invalidate(_preference_cache_key(user_id))


def _build_preference_profile_uncached(user_id: str | None) -> PreferenceProfile:
    prof = PreferenceProfile()
    try:
        with get_session() as session:
            rows = [
                _Row(*r) for r in session.exec(
                    select(Application.status, Application.notes, Job.company, Job.title)
                    .join(Job, Application.job_id == Job.id)
                    .where(Job.user_id == user_id)
                ).all()
            ]
    except Exception as e:
        log.debug("preference profile query failed for %s: %s", user_id, e)
        return prof

    dismissed_by_company: Counter = Counter()
    engaged_companies: set = set()
    for row in rows:
        company = (row.company or "").strip().lower()
        if row.status in _ENGAGED_STATUSES:
            prof.engaged_total += 1
            if company:
                engaged_companies.add(company)
            prof.liked_tokens.update(set(_title_tokens(row.title)))
        elif _is_user_dismissal(row):
            prof.dismissed_total += 1
            if company:
                dismissed_by_company[company] += 1
            prof.disliked_tokens.update(set(_title_tokens(row.title)))

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
