"""The first hour: a new or returning user gets looked after, not queued.

Production, 2026-09-28 (counts only): a friend returning to a weeks-old account
got 3 jobs in her first hour. Nothing special happened on her return — her
queue waited for the lanes' next ticks, the scoring lane's "empty board first"
ordering did not apply (she had old scored jobs), and her target roles were
Senior/Architect titles against ~1 year of experience, so only 20 of her 2,139
finals ever scored 60+. Three things fix the first hour:

1. BOOST — resume upload, first role save, or a return after the idle window
   opens a ``welcome_boost_minutes`` window: adoption of the full fresh window
   runs NOW, and the scoring lane puts the user first with a larger per-cycle
   slice. It buys nothing extra: the plan's own daily budget still decides how
   many finals they get, the boost only decides that they get them FIRST.
2. PROGRESS — ``status()`` feeds the dashboard's panel so the first minutes
   show work happening instead of an empty board.
3. ROLE CHECK — ``seniority_tip()`` names senior titles the resume's years
   cannot reach and offers the same roles without the seniority words.

The boost window is process-local on purpose (one web+lanes process): a
restart forgets it, which costs at most one hour of priority — never jobs.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.config import settings

log = logging.getLogger(__name__)

_BOOSTS: Dict[str, datetime] = {}      # user_id -> boost start (UTC)
_LOCK = threading.Lock()


def _window() -> timedelta:
    return timedelta(minutes=max(0, int(settings.welcome_boost_minutes)))


def begin(user_id: Optional[str], reason: str, now: Optional[datetime] = None) -> bool:
    """Open (or keep) this user's boost window. True when a new one started."""
    if not user_id or user_id == "local" or settings.welcome_boost_minutes <= 0:
        return False
    now = now or datetime.utcnow()
    with _LOCK:
        started = _BOOSTS.get(user_id)
        if started is not None and now - started < _window():
            return False
        _BOOSTS[user_id] = now
        # Bounded: forget windows that closed long ago.
        for uid, ts in list(_BOOSTS.items()):
            if now - ts > _window() * 4:
                _BOOSTS.pop(uid, None)
    try:   # the panel's cached answer predates the boost
        from app.common import ttl_cache
        ttl_cache.invalidate(f"welcome:{user_id}")
    except Exception:
        pass
    log.info("Welcome boost started (%s)", reason)   # no user id in the line
    return True


def started_at(user_id: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """Start of the user's boost window while it is open, else None."""
    if not user_id:
        return None
    now = now or datetime.utcnow()
    with _LOCK:
        started = _BOOSTS.get(user_id)
    if started is None or now - started >= _window():
        return None
    return started


def is_boosted(user_id: Optional[str], now: Optional[datetime] = None) -> bool:
    return started_at(user_id, now) is not None


def returning_after_idle(previous_meaningful: Optional[datetime],
                         now: Optional[datetime] = None) -> bool:
    """A meaningful request from someone idle past the trial idle window (or
    who never acted) is a return worth a welcome."""
    now = now or datetime.utcnow()
    if previous_meaningful is None:
        return True
    return now - previous_meaningful >= timedelta(hours=max(1, settings.trial_idle_after_hours))


def welcome_back(user_id: Optional[str]) -> bool:
    """Start the boost and refresh the pool in the background. Returns whether
    a new boost started (a second call inside the window does nothing)."""
    if not begin(user_id, "returning user"):
        return False
    threading.Thread(target=_refresh, args=(user_id,), name="welcome-refresh",
                     daemon=True).start()
    return True


def _refresh(user_id: str) -> None:
    """Adopt the whole fresh window now, then ask the scoring lane for a cycle
    — the lane's own gates (compute policy, plan budget, breaker) decide what
    is spent. Best-effort; the scheduled lanes remain the backstop."""
    if not _profile_exists(user_id):
        return   # deleted meanwhile: adoption would re-create the profile
    try:
        from app.strategy.adoption import adopt_shared_jobs
        adopted = adopt_shared_jobs(user_id)
        log.info("Welcome refresh: %d fresh posting(s) adopted", adopted)
    except Exception as e:
        log.warning("welcome refresh adoption failed: %s", e)
    kick_scoring()


def _profile_exists(user_id: str) -> bool:
    try:
        from sqlmodel import select
        from app.db.init_db import get_session
        from app.db.models import UserProfile
        with get_session() as s:
            return s.exec(select(UserProfile.id).where(
                UserProfile.user_id == user_id).limit(1)).first() is not None
    except Exception:
        return False


def kick_scoring(attempts: int = 2, wait_seconds: float = 20.0) -> None:
    """Run a scoring cycle now (boosted users go first in it). A cycle already
    in flight answers ``skipped``; wait briefly and try once more."""
    import time as _time
    try:
        from app.strategy.scoring_lane import run_scoring_lane
    except Exception as e:
        log.debug("welcome: scoring lane unavailable: %s", e)
        return
    for attempt in range(max(1, attempts)):
        try:
            if not run_scoring_lane().get("skipped"):
                return
        except Exception as e:
            log.debug("welcome scoring kick failed: %s", e)
            return
        if attempt < attempts - 1:
            _time.sleep(wait_seconds)


def per_cycle_cap(user_id: Optional[str], base: int) -> int:
    """A boosted user's per-cycle slice. The plan budget still bounds it."""
    if is_boosted(user_id):
        return max(base, int(base * max(1.0, settings.welcome_boost_cap_multiplier)))
    return base


# ── Role check ───────────────────────────────────────────────────────────────

_SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|head|director|vp|chief|architect|manager)\b",
    re.IGNORECASE)
_STRIP_RE = re.compile(r"\b(senior|sr\.?|staff|principal|lead)\b\.?", re.IGNORECASE)


def _junior_version(role: str) -> Optional[str]:
    """The same work without the seniority: "Senior Backend Engineer" ->
    "Backend Engineer", "Cloud Solutions Architect" -> "Cloud Engineer". Titles
    whose whole point is managing people have no honest junior twin."""
    r = role.strip()
    if re.search(r"\b(head|director|vp|chief|manager)\b", r, re.IGNORECASE):
        return None
    r = _STRIP_RE.sub("", r)
    r = re.sub(r"\b(solutions?\s+)?architect\b", "Engineer", r, flags=re.IGNORECASE)
    r = re.sub(r"\s+", " ", r).strip(" -,/")
    if not r or r.lower() in ("engineer", "developer"):
        return None
    return r


def seniority_tip(profile, roles: List[str]) -> Optional[dict]:
    """Senior titles the resume's years cannot reach, and junior versions to add.

    Only when the profile states 1-2 years: 0 is also what an unparsed profile
    stores, and telling an experienced person they are junior is worse than
    saying nothing."""
    try:
        years = int(getattr(profile, "years_experience", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= years < 3) or not roles:
        return None
    senior = [r for r in roles if r and _SENIOR_RE.search(r)]
    if not senior:
        return None
    have = {r.strip().lower() for r in roles if r}
    suggest: List[str] = []
    named: List[str] = []       # only the roles we offer a junior version for
    for r in senior:
        j = _junior_version(r)
        if j and j.lower() not in have and j.lower() not in {s.lower() for s in suggest}:
            suggest.append(j)
            named.append(r)
    if not suggest:
        return None
    return {"years": years, "senior_roles": named[:3], "suggest": suggest[:3]}


# ── Progress (the dashboard panel) ───────────────────────────────────────────

def status(user_id: str) -> dict:
    """What the first-hour panel shows. Counts only, one user, indexed."""
    from sqlalchemy import func, or_
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import Application, Job
    from app.matching.finals_budget import delivered_today

    now = datetime.utcnow()
    started = started_at(user_id, now)
    since = started or (now - timedelta(hours=24))
    fresh_after = now - timedelta(days=max(1, settings.scoring_max_job_age_days))
    out = {"boost_active": started is not None,
           "minutes_left": (int((_window() - (now - started)).total_seconds() // 60)
                            if started else 0)}
    with get_session() as s:
        out["pool_fresh"] = int(s.exec(select(func.count(Job.id)).where(
            Job.user_id == user_id, Job.is_closed == False,  # noqa: E712
            or_(Job.first_seen >= fresh_after,
                (Job.first_seen.is_(None)) & (Job.discovered_at >= fresh_after)),
        )).first() or 0)
        out["checked"] = int(s.exec(select(func.count(Job.id)).where(
            Job.user_id == user_id,
            or_(Job.scored_at >= since, Job.prescored_at >= since),
        )).first() or 0)
        out["waiting"] = int(s.exec(select(func.count(Job.id)).where(
            Job.user_id == user_id, Job.is_closed == False,  # noqa: E712
            Job.rerank_score.is_(None),
            or_(Job.first_seen >= fresh_after,
                (Job.first_seen.is_(None)) & (Job.discovered_at >= fresh_after)),
        )).first() or 0)
        out["new_matches"] = int(s.exec(select(func.count(Application.id)).where(
            Application.user_id == user_id, Application.created_at >= since,
            Application.apply_track != "email_import",
        )).first() or 0)
    out["delivered_today"] = int(delivered_today(user_id) or 0)
    return out
