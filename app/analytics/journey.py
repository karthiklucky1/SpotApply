"""The user journey, measured from things people DID — never from polling.

Audit 2026-09-25 (finding 14): the only production signal of engagement was a
last-active timestamp that a background tab kept fresh, so "1 active user" was
an open browser. This module records a small set of milestones from the routes
where a person acts, and nothing else:

    signup · resume_uploaded · profile_ready · first_shortlist · job_opened ·
    resume_requested · review_opened · document_downloaded ·
    application_recorded · outcome_recorded · active_day · search_resumed

PRIVACY. Each event is a FunnelEvent(stage="journey") whose `reason` is
"<milestone>:<user key>", where the user key is a salted SHA-256 of the user id
(16 hex chars) — enough to count distinct people and returns, not enough to
identify anyone. Metadata carries only labels (e.g. an outcome bucket), never
résumé text, work-authorization data, job text, names, emails or URLs.
Internal/test accounts listed in INTERNAL_USER_IDS are never recorded, and
anonymous traffic (bots) never reaches an authenticated action route.

Every function is best-effort: a metric never fails the request it rides on.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

STAGE = "journey"
MILESTONES = (
    "signup", "resume_uploaded", "profile_ready", "first_shortlist", "job_opened",
    "resume_requested", "review_opened", "document_downloaded",
    "application_recorded", "outcome_recorded", "active_day", "search_resumed",
)
#: Milestones recorded at most once per user, ever.
ONCE = frozenset({"signup", "resume_uploaded", "profile_ready", "first_shortlist"})
#: Outcome labels a status change may carry — buckets, not free text.
OUTCOMES = frozenset({"interviewing", "offer", "accepted", "rejected", "ghosted"})


def user_key(user_id: Optional[str]) -> str:
    from app.config import settings
    salt = (getattr(settings, "analytics_salt", "") or "spotapply-journey-v1")
    return hashlib.sha256(f"{salt}:{user_id or 'local'}".encode()).hexdigest()[:16]


def _internal(user_id: Optional[str]) -> bool:
    from app.config import settings
    raw = getattr(settings, "internal_user_ids", "") or ""
    ids = {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}
    return bool(user_id) and user_id in ids


def record(user_id: Optional[str], milestone: str, *, outcome: Optional[str] = None,
           day: Optional[datetime] = None, session=None) -> bool:
    """Record one milestone. Returns True when a row was written.

    Pass ``session`` when the caller is already inside a write transaction
    (slate.place): the row then commits WITH the decision it describes, and no
    second connection is opened while the first holds locks."""
    if milestone not in MILESTONES or _internal(user_id):
        return False
    try:
        from app.db.init_db import get_session
        key = user_key(user_id)
        reason = f"{milestone}:{key}"
        meta: dict = {}
        if outcome is not None:
            if outcome not in OUTCOMES:
                return False
            meta["outcome"] = outcome
        now = day or datetime.utcnow()
        if session is not None:
            return _write(session, milestone, reason, meta, now, commit=False)
        with get_session() as s:
            return _write(s, milestone, reason, meta, now, commit=True)
    except Exception as e:                           # never fail the request
        log.debug("journey %s not recorded: %s", milestone, e)
        return False


def _write(s, milestone: str, reason: str, meta: dict, now: datetime, *, commit: bool) -> bool:
    from sqlmodel import select
    from app.db.models import FunnelEvent
    if milestone in ONCE or milestone == "active_day":
        q = select(FunnelEvent.id).where(FunnelEvent.stage == STAGE,
                                         FunnelEvent.reason == reason)
        if milestone == "active_day":
            start = datetime(now.year, now.month, now.day)
            q = q.where(FunnelEvent.created_at >= start,
                        FunnelEvent.created_at < start + timedelta(days=1))
        if s.exec(q.limit(1)).first() is not None:
            return False
    s.add(FunnelEvent(job_id=None, stage=STAGE, passed=True, reason=reason,
                      metadata_json=json.dumps(meta) if meta else None, created_at=now))
    if commit:
        s.commit()
    return True


def summary(days: int = 30, now: Optional[datetime] = None) -> dict:
    """Distinct users per milestone in the window, day-1/3/7 returns and the
    conversion between consecutive milestones. Aggregates only."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import FunnelEvent
    now = now or datetime.utcnow()
    since = now - timedelta(days=days)
    with get_session() as s:
        rows = s.exec(select(FunnelEvent.reason, FunnelEvent.created_at)
                      .where(FunnelEvent.stage == STAGE, FunnelEvent.created_at >= since)
                      .limit(200_000)).all()
    users: dict = {m: set() for m in MILESTONES}
    first_seen: dict = {}
    active_days: dict = {}
    for reason, at in rows:
        m, _, key = (reason or "").partition(":")
        if m not in users or not key:
            continue
        users[m].add(key)
        if m in ("signup", "profile_ready", "resume_uploaded"):
            first_seen[key] = min(first_seen.get(key, at), at)
        if m == "active_day":
            active_days.setdefault(key, set()).add(at.date())
    returns = {}
    for n in (1, 3, 7):
        cohort = [k for k, t in first_seen.items() if t <= now - timedelta(days=n)]
        back = [k for k in cohort
                if any(d >= (first_seen[k] + timedelta(days=n)).date()
                       for d in active_days.get(k, ()))]
        returns[f"day_{n}"] = {"cohort": len(cohort), "returned": len(back)}
    counts = {m: len(v) for m, v in users.items()}
    order = ["signup", "resume_uploaded", "profile_ready", "first_shortlist", "job_opened",
             "resume_requested", "review_opened", "document_downloaded",
             "application_recorded", "outcome_recorded"]
    steps = []
    for a, b in zip(order, order[1:]):
        steps.append({"from": a, "to": b, "users_from": counts[a], "users_to": counts[b]})
    return {"window_days": days, "distinct_users": counts, "returns": returns,
            "steps": steps,
            "note": ("Distinct people per milestone from action routes only; polling, "
                     "page views and internal accounts are excluded. A job-match score "
                     "is not an interview probability and is not reported here.")}


# ── request → milestone (used by server.JourneyMiddleware) ───────────────────
import re as _re

_ROUTES = (
    ("POST", _re.compile(r"^/api/resume/upload$"), "resume_uploaded"),
    ("POST", _re.compile(r"^/application/\d+/viewed$"), "job_opened"),
    ("POST", _re.compile(r"^/run/tailor/\d+$"), "resume_requested"),
    ("GET", _re.compile(r"^/application/\d+/review$"), "review_opened"),
    ("GET", _re.compile(r"^/application/\d+/download-resume$"), "document_downloaded"),
    ("POST", _re.compile(r"^/application/\d+/submit$"), "application_recorded"),
    ("POST", _re.compile(r"^/application/\d+/outcome$"), "outcome_recorded"),
    ("POST", _re.compile(r"^/api/search/resume$"), "search_resumed"),
)


def milestone_for(method: str, path: str) -> Optional[str]:
    """The milestone a SUCCESSFUL request to this route represents, or None.
    Only action routes are listed — no GET that a timer makes can match."""
    m = (method or "").upper()
    for meth, rx, name in _ROUTES:
        if m == meth and rx.match(path or ""):
            return name
    return None
