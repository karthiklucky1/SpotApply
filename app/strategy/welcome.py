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

2026-09-30: the first match still took 2-4 minutes after upload, with an empty
board in between. Two more things close that gap:

4. FIRST RESULTS — ``first_results()`` scores the few most promising adopted
   jobs the moment adoption finishes, instead of at the next cycle. Same
   guards and allowance as a cycle; only the timing moves.
5. SHOW THE WORK — ``status()`` also returns the real jobs being checked and
   their verdicts (``preview``), and the panel shows what was read from the
   resume (``resume_summary``). Every row is a real posting in the user's
   pool with its real state; nothing is called a match before it is scored.

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


_FIRST_DONE: Dict[str, datetime] = {}   # user_id -> the boost window it ran in


def first_results(user_id: Optional[str]) -> dict:
    """Score the user's most promising adopted jobs now — once per welcome
    window. A role save inside the window adopts again but does not re-run it:
    the boosted user is first in every cycle anyway, and each run pays a
    résumé cache write."""
    n = int(settings.welcome_first_scores or 0)
    started = started_at(user_id)
    if n <= 0 or started is None:
        return {"skipped": "not in the first hour"}
    with _LOCK:
        if _FIRST_DONE.get(user_id) == started:
            return {"skipped": "already ran in this window"}
        _FIRST_DONE[user_id] = started
        for uid, ts in list(_FIRST_DONE.items()):
            if datetime.utcnow() - ts > _window() * 4:
                _FIRST_DONE.pop(uid, None)
    from app.strategy.scoring_lane import score_user_now
    try:
        return score_user_now(user_id, n)
    finally:
        try:
            from app.common import ttl_cache
            ttl_cache.invalidate(f"welcome:{user_id}")
        except Exception:
            pass


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
        _bound_statements(s)
        out["pool_fresh"] = int(s.exec(select(func.count(Job.id)).where(
            Job.user_id == user_id, Job.is_closed == False,  # noqa: E712
            or_(Job.first_seen >= fresh_after,
                (Job.first_seen.is_(None)) & (Job.discovered_at >= fresh_after)),
        )).first() or 0)
        # Bounded by the fresh window like every other count here: nothing
        # older can have been checked in the last hour's work, and without it
        # the walk covered every row the user ever held (no index on
        # scored_at/prescored_at).
        out["checked"] = int(s.exec(select(func.count(Job.id)).where(
            Job.user_id == user_id, _fresh(fresh_after),
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
    if started is not None:
        try:
            out.update(_preview(user_id, since, fresh_after))
        except Exception as e:
            log.debug("welcome preview unavailable: %s", e)
    return out


def _fresh(fresh_after: datetime):
    """The KNOWN-age bound (app/common/freshness.py), spelled index-friendly."""
    from sqlalchemy import or_
    from app.db.models import Job
    return or_(Job.first_seen >= fresh_after,
               (Job.first_seen.is_(None)) & (Job.discovered_at >= fresh_after))


def _bound_statements(session, seconds: int = 5) -> None:
    """Postgres only: no panel read may run longer than ``seconds``."""
    try:
        from app.strategy.scoring_lane import _arm_statement_timeout
        _arm_statement_timeout(session, seconds)
    except Exception:
        pass


# ── Show the work (the panel's live list) ────────────────────────────────────

PREVIEW_MATCHES = 3
PREVIEW_CHECKING = 3
PREVIEW_CHECKED = 2
# Stamps that are not a fit verdict (ghost 5.0, expiry 8.0, location 10.0).
_SENTINEL_MAX = 10.0


def _row(job, state: str, score: Optional[float] = None) -> dict:
    return {"id": job.id, "title": (job.title or "")[:120],
            "company": (job.company or "")[:80],
            "location": (job.location or "")[:60],
            "state": state,
            "score": int(round(score)) if score is not None else None}


def _preview(user_id: str, since: datetime, fresh_after: datetime) -> dict:
    """Real jobs from the user's pool with their real state, for the panel:

    - ``match``    — on the board since the window opened (an Application)
    - ``checking`` — queued for a fit check, next in line (the lane's order)
    - ``passed``   — checked since the window opened, below the bar
    - ``filtered`` — removed without a fit verdict (closed, too old, location)

    Plus ``passed_count``: how many were checked and kept off the board, so the
    panel can say the filter is working rather than hide it."""
    from sqlalchemy import func, or_
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import Application, Job
    from app.matching.finals_budget import normal_gate

    bar = float(settings.shortlist_score_threshold)
    fresh = _fresh(fresh_after)
    rows: List[dict] = []
    with get_session() as s:
        _bound_statements(s)
        cols = (Job.id, Job.title, Job.company, Job.location, Job.rerank_score)
        matches = s.exec(
            select(*cols).join(Application, Application.job_id == Job.id).where(
                Application.user_id == user_id, Application.created_at >= since,
                Application.apply_track != "email_import",
            ).order_by(Job.rerank_score.desc()).limit(PREVIEW_MATCHES)).all()
        match_ids = {m.id for m in matches}
        rows += [_row(m, "match", m.rerank_score) for m in matches]

        # The lane's own order (scoring_lane._user_queue), so "checking" names
        # the jobs actually next in line.
        promise = func.coalesce(Job.prescore, float(normal_gate()))
        checking = s.exec(select(*cols).where(
            Job.user_id == user_id, Job.rerank_score.is_(None),
            Job.is_closed == False, fresh,  # noqa: E712
            or_(Job.eligibility.is_(None), Job.eligibility != "unknown"),
        ).order_by(promise.desc(), Job.first_seen.desc()).limit(PREVIEW_CHECKING)).all()
        rows += [_row(c, "checking") for c in checking]

        checked_q = select(*cols).where(
            Job.user_id == user_id, Job.rerank_score.is_not(None), fresh,
            or_(Job.scored_at >= since, Job.prescored_at >= since),
        )
        checked = [c for c in s.exec(checked_q.order_by(
            func.coalesce(Job.scored_at, Job.prescored_at).desc()).limit(12)).all()
            if c.id not in match_ids]
        passed = [c for c in checked if (c.rerank_score or 0) < bar]
        rows += [_row(c, "filtered" if (c.rerank_score or 0) <= _SENTINEL_MAX else "passed",
                      c.rerank_score) for c in passed[:PREVIEW_CHECKED]]
        passed_count = int(s.exec(select(func.count(Job.id)).where(
            Job.user_id == user_id, Job.rerank_score.is_not(None),
            Job.rerank_score < bar, fresh,
            or_(Job.scored_at >= since, Job.prescored_at >= since),
        )).first() or 0)
    return {"preview": rows, "passed_count": passed_count, "bar": int(bar)}


def resume_summary(profile, roles: List[str]) -> Optional[dict]:
    """What we read from the resume, shown while the first matches arrive — it
    proves the resume was understood. Only fields the profile actually holds."""
    if profile is None:
        return None
    skills = [x.strip() for x in (getattr(profile, "key_skills", "") or "").split(",")
              if x.strip()]
    try:
        years = int(getattr(profile, "years_experience", 0) or 0)
    except (TypeError, ValueError):
        years = 0
    title = (getattr(profile, "current_title", "") or "").strip()
    roles = [r for r in (roles or []) if r][:6]
    if not (skills or years or title or roles):
        return None
    return {"title": title[:80], "years": years, "skills": skills[:8],
            "skills_count": len(skills), "roles": roles}
