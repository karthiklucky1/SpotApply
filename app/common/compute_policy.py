"""Who may have automatic, personalised background work done for them — and
when it costs money.

Feature access and background compute are DIFFERENT questions. The plan
(`_get_user_plan`, including temporary Pro for everyone) answers which features
a person may use when they ask. This module answers what the platform may do
on their behalf while they are NOT asking — adopt jobs into their pool, queue
them, and above all pay a model provider to rank them. The 2026-09-16 incident
(10 dormant accounts took 90.8% of a week's LLM spend) and the 2026-09-25 audit
(a tab left open kept an account "active") were both this confusion.

STATES, from the last MEANINGFUL action (`last_meaningful_activity_at`):

    setup      no usable profile yet (no résumé)       nothing personalised
    active     meaningful action within IDLE_AFTER     everything, within budgets
    idle       IDLE_AFTER .. DORMANT_AFTER             queue/adopt (free DB work);
                                                       NO paid AI, NO research
    dormant    past DORMANT_AFTER (or never engaged)   nothing personalised
    paused     the user paused it                      nothing personalised
    paid       a live PAID entitlement                 its purchased promise
                                                       (still bounded by budgets)

"Meaningful" is decided at the request layer (`server._activity_kind`): a write,
a document fetch/download, starting a fill, "Resume search". A page view is
"seen" (it updates `last_active_at` only); polls, notification fetches and token
refreshes are nothing at all. A user returning to the dashboard sees a pause
notice with a Resume button; the first meaningful action resumes them.

A NULL `last_meaningful_activity_at` is NOT grandfathered as active: the old
`last_active_at` was stamped by polling, so it proves nothing, and the audit
asked that trial work start again only on the next real interaction.

Enforced at TWO points on purpose: when work is queued (the lane user lists,
via `server._user_is_active`) and immediately before a paid provider call
(`reranker`, via `paid_ai_allowed`) — a job queued while the user was active
must not be paid for after they left. The provider-call check is cached per
user for `_CACHE_S` so twenty scoring workers do not each read the profile.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

log = logging.getLogger(__name__)

SETUP, ACTIVE, IDLE, DORMANT, PAUSED, PAID = ("setup", "active", "idle", "dormant",
                                              "paused", "paid")

#: Kinds of automatic work.
QUEUE = "queue"                # adoption, retrieval, placement of already-scored jobs
PAID_AI = "paid_ai"            # any model-provider call made without the user asking
RESEARCH = "research"          # recruiter/contact research made without the user asking

_ALLOWED = {
    ACTIVE: {QUEUE, PAID_AI, RESEARCH},
    PAID: {QUEUE, PAID_AI, RESEARCH},
    IDLE: {QUEUE},
    DORMANT: set(),
    PAUSED: set(),
    SETUP: set(),
}

POLICY_VERSION = 1


def enforced() -> bool:
    from app.config import settings
    return bool(getattr(settings, "compute_policy_enforced", True))


def idle_after() -> timedelta:
    from app.config import settings
    return timedelta(hours=max(1, int(getattr(settings, "trial_idle_after_hours", 24) or 24)))


def dormant_after() -> timedelta:
    from app.config import settings
    return timedelta(hours=max(1, int(getattr(settings, "trial_dormant_after_hours", 72) or 72)))


@dataclass(frozen=True)
class SearchState:
    state: str
    since: Optional[datetime]       # the timestamp the state was derived from
    reason: str                     # one sentence the user can read

    def allows(self, kind: str) -> bool:
        return kind in _ALLOWED.get(self.state, set())


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def search_state(profile, *, now: Optional[datetime] = None,
                 paid: Optional[Callable[[object], bool]] = None,
                 has_resume: Optional[bool] = None) -> SearchState:
    """The one decision. Pure apart from the optional ``paid`` callback.

    ``paid(profile) -> bool`` is `server._user_paid_search_is_live`; it is only
    consulted for a user who would otherwise not be active, so it costs a read
    for a handful of profiles, never every tick for everyone.
    """
    now = now or datetime.utcnow()
    if profile is None:
        return SearchState(SETUP, None, "No profile yet")
    if _naive(getattr(profile, "search_paused_at", None)) is not None:
        why = (getattr(profile, "pause_reason", "") or "").strip()
        return SearchState(PAUSED, _naive(profile.search_paused_at),
                           "You paused your search" + (f" ({why})" if why else ""))
    if has_resume is False:
        return SearchState(SETUP, None, "Upload a résumé to start your search")
    last = _naive(getattr(profile, "last_meaningful_activity_at", None))
    if last is not None and now - last < idle_after():
        return SearchState(ACTIVE, last, "Your search is running")
    if paid is not None:
        try:
            if paid(profile):
                return SearchState(PAID, last, "Your paid search keeps running")
        except Exception as e:                       # never pause a search we may be charging for
            log.debug("compute policy: paid lookup failed (%s) — treating as paid", e)
            return SearchState(PAID, last, "Your paid search keeps running")
    if last is not None and now - last < dormant_after():
        return SearchState(IDLE, last, "Your search is paused to save resources — "
                                       "resume whenever you are ready")
    return SearchState(DORMANT, last, "Your search is paused to save resources — "
                                      "resume whenever you are ready")


# ── the provider-call check ──────────────────────────────────────────────────

_CACHE_S = 60.0
_cache: dict = {}
_lock = threading.Lock()


def _load_profile(user_id: str):
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserProfile
    with get_session() as s:
        return s.exec(select(UserProfile).where(UserProfile.user_id == user_id)).first()


def paid_ai_allowed(user_id: Optional[str], *, now: Optional[float] = None) -> bool:
    """May the platform make an AUTOMATIC paid model call for this user now?

    Local/anonymous (None/"local") and the platform itself are always allowed:
    their spend is bounded by the platform backstops, and this gate is about a
    person who has left. A profile that cannot be read is allowed too — the
    queue-time gate already admitted the work, and failing closed on a
    database hiccup would stop every user's scoring at once.
    """
    if not user_id or user_id in ("local", "__shared__") or not enforced():
        return True
    now = time.monotonic() if now is None else now
    with _lock:
        hit = _cache.get(user_id)
        if hit and now - hit[0] < _CACHE_S:
            return hit[1]
    try:
        profile = _load_profile(user_id)
        from app.api.server import _user_paid_search_is_live
        allowed = search_state(profile, paid=_user_paid_search_is_live).allows(PAID_AI)
    except Exception as e:
        log.debug("compute policy: could not decide for a user (%s) — allowing", e)
        allowed = True
    with _lock:
        _cache[user_id] = (now, allowed)
    return allowed


def forget(user_id: Optional[str]) -> None:
    """Drop the cached answer — called when the user acts, pauses or resumes."""
    with _lock:
        _cache.pop(user_id or "", None)


def reset_state() -> None:
    """Tests only."""
    with _lock:
        _cache.clear()


# ── atomic per-attempt reservation ──────────────────────────────────────────

def reserve_paid_call(user_id: Optional[str]) -> bool:
    """Reserve ONE automatic paid provider attempt — retries and fallback
    providers each reserve their own — against the user's daily ceiling and the
    platform's. Atomic across workers, replicas and restarts
    (`daily_counter.reserve`: one conditional UPDATE). Refuses on a database
    failure: money that cannot be accounted for is not spent.

    These ceilings are backstops above the plan's own budgets (finals_budget),
    not the product limit; they exist so a loop, a retry storm or a second
    process cannot outrun the ledger.
    """
    from app.common.daily_counter import reserve
    from app.config import settings
    platform_cap = int(getattr(settings, "platform_daily_paid_call_cap", 0) or 0)
    if not reserve("paid_calls:platform", platform_cap):
        log.warning("paid call refused: platform daily ceiling (%s) reached", platform_cap)
        return False
    if user_id and user_id not in ("local", "__shared__"):
        user_cap = int(getattr(settings, "user_daily_paid_call_cap", 0) or 0)
        if not reserve(f"paid_calls:user:{user_id}", user_cap):
            log.warning("paid call refused: per-user daily ceiling (%s) reached", user_cap)
            return False
    return True
