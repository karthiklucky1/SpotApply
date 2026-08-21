"""Adaptive finals budget — spend Claude on evidence, not on a counter.

The flat per-plan `finals_daily` cap was wrong in both directions on the same
day: it stopped a strong user at 50 while the 50th final was still returning
70-fits, and it happily spent 50 finals on a Saturday proving that 20 weak jobs
are weak. This module replaces "a fixed number of results" with "a bounded
amount of money, spent while the evidence says the next candidate is worth it".

Three numbers, three different jobs (PRO shown; every plan scales the same way
from PLAN_LIMITS["finals_daily"]):

    soft   =  50/day    normal spending point — spent freely, no justification
    burst  = 100/day    absolute ceiling for one UTC day
    weekly = 350/week   the real economic control (= 50/day average = today's cost)

Past the SOFT point every extra final must be earned by two tests:

    Test A — promise floor (free). The Tier-1 prescore already runs immediately
      before the final. In burst territory the bar rises from the normal advance
      gate (40) to `finals_promise_floor` (55): only genuinely strong candidates
      get the expensive look. Costs nothing — the prescore was already paid.

    Test B — marginal yield. Over the last `yield_window` finals, the share that
      cleared the shortlist bar must be >= `yield_continue_rate`. This is the
      guard against a miscalibrated Tier-1: if prescore keeps promising 70s and
      Claude keeps answering 40s, we stop even though Test A is passing.

Neither test can ever ADD spend: they only decide whether the burst zone opens.
Nothing here chases a target — if the pool holds 6 good jobs, the user gets 6.

DURABILITY. The counters that bound money are persisted to `UserUsage`
(finals_count / finals_hits, one row per user per UTC day). In-memory counters
cannot hold a weekly budget: every deploy would grant a fresh week, and that is
exactly why the Aug 14-21 stall appeared to "heal" on restart. The ledger is the
source of truth; a tiny per-process cache keeps the hot path off the database.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from app.config import settings

log = logging.getLogger(__name__)

# (user_id, day) -> {"finals": n, "hits": n, "at": monotonic}. Authoritative
# value lives in UserUsage; this only avoids a SELECT per decision. Writes
# increment the cached value in place, so within one process it never lags.
_day_cache: dict = {}
_week_cache: dict = {}          # user_id -> {"week": date, "finals": n, "at": monotonic}
_recent: dict = {}              # user_id -> deque[bool], the last N final outcomes
_lock = threading.Lock()

_DAY_CACHE_TTL = 30.0           # seconds
_WEEK_CACHE_TTL = 60.0


def _utc_day() -> date:
    return datetime.utcnow().date()


def _week_start(d: Optional[date] = None) -> date:
    d = d or _utc_day()
    return d - timedelta(days=d.weekday())      # Monday


def _ledgerable(user_id: Optional[str]) -> bool:
    """'local' and the anonymous case have no plan and no ledger row."""
    return bool(user_id) and user_id != "local"


# ── Plan → the three budgets ─────────────────────────────────────────────────

def budgets(soft_cap: int) -> Tuple[int, int, int]:
    """(soft, burst, weekly) derived from the plan's daily allowance.

    Multipliers rather than three numbers per plan: a plan is one dial, and the
    burst/weekly relationship (weekly = 7x soft = the same money the flat cap
    already spent) has to hold for every tier or the economics drift apart.
    """
    soft = max(0, int(soft_cap))
    burst = int(round(soft * max(1.0, settings.finals_burst_multiplier)))
    weekly = int(round(soft * max(1.0, settings.finals_weekly_multiplier)))
    return soft, burst, weekly


# ── The ledger ───────────────────────────────────────────────────────────────

def _read_day(user_id: str, day: date) -> Tuple[int, int]:
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserUsage
    with get_session() as session:
        row = session.exec(
            select(UserUsage).where(
                UserUsage.user_id == user_id,
                UserUsage.usage_date == day,
            )
        ).first()
        if not row:
            return 0, 0
        return int(row.finals_count or 0), int(row.finals_hits or 0)


def _read_week(user_id: str, week_start: date) -> int:
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserUsage
    with get_session() as session:
        rows = session.exec(
            select(UserUsage).where(
                UserUsage.user_id == user_id,
                UserUsage.week_start == week_start,
            )
        ).all()
        return sum(int(r.finals_count or 0) for r in rows)


def _write(user_id: str, day: date, finals: int = 0, hits: int = 0) -> None:
    """Add the deltas to one day row, creating it if needed.

    The increment is an ATOMIC `SET x = x + n` statement, never a read-modify-
    write: 20 scoring workers finish finals concurrently, and read-then-write
    loses increments under exactly that concurrency. A lost increment on a
    money counter means overspend, which is the one direction this must not
    fail in.

    Best-effort otherwise — a ledger write must never fail a score the user has
    already paid for.
    """
    from sqlalchemy import text as _text
    from app.db.init_db import get_session
    from app.db.models import UserUsage
    try:
        with get_session() as session:
            res = session.execute(
                # COALESCE, not a bare +: a row that predates these columns can
                # hold NULL if the ALTER ever lands without its DEFAULT, and
                # NULL + 1 is NULL — a counter that silently never counts is
                # unlimited spend.
                _text("UPDATE user_usage SET "
                      "finals_count = COALESCE(finals_count, 0) + :f, "
                      "finals_hits = COALESCE(finals_hits, 0) + :h "
                      "WHERE user_id = :u AND usage_date = :d"),
                {"f": finals, "h": hits, "u": user_id, "d": day},
            )
            if not res.rowcount:
                session.add(UserUsage(
                    user_id=user_id, usage_date=day, week_start=_week_start(day),
                    finals_count=finals, finals_hits=hits))
            session.commit()
    except Exception as e:
        log.debug("finals ledger write failed for %s (%+d finals, %+d hits): %s",
                  user_id, finals, hits, e)


def day_counts(user_id: Optional[str]) -> Tuple[int, int]:
    """(finals, hits) charged to this user so far today, ledger-backed."""
    if not _ledgerable(user_id):
        return 0, 0
    day = _utc_day()
    now = time.monotonic()
    with _lock:
        c = _day_cache.get(user_id)
        if c and c["day"] == day and (now - c["at"]) < _DAY_CACHE_TTL:
            return c["finals"], c["hits"]
    finals, hits = _read_day(user_id, day)
    with _lock:
        _day_cache[user_id] = {"day": day, "finals": finals, "hits": hits, "at": now}
    return finals, hits


def week_finals(user_id: Optional[str]) -> int:
    if not _ledgerable(user_id):
        return 0
    ws = _week_start()
    now = time.monotonic()
    with _lock:
        c = _week_cache.get(user_id)
        if c and c["week"] == ws and (now - c["at"]) < _WEEK_CACHE_TTL:
            return c["finals"]
    n = _read_week(user_id, ws)
    with _lock:
        _week_cache[user_id] = {"week": ws, "finals": n, "at": now}
    return n


def record_final(user_id: Optional[str]) -> None:
    """One authoritative Tier-2 call was paid for. Called from the reranker the
    moment the API returns, BEFORE the response is parsed — a call that returns
    unparseable JSON still cost money."""
    if not _ledgerable(user_id):
        return
    day = _utc_day()
    with _lock:
        c = _day_cache.get(user_id)
        if c and c["day"] == day:
            c["finals"] += 1
        w = _week_cache.get(user_id)
        if w and w["week"] == _week_start(day):
            w["finals"] += 1
    _write(user_id, day, finals=1)


def record_outcome(user_id: Optional[str], score: float) -> None:
    """The final's verdict. A 'hit' is a score at or above the shortlist bar —
    the only definition that matches what the user actually receives."""
    hit = float(score) >= float(settings.shortlist_score_threshold)
    if not _ledgerable(user_id):
        return
    window = max(1, settings.finals_yield_window)
    with _lock:
        ring = _recent.get(user_id)
        if ring is None or ring.maxlen != window:
            ring = deque(maxlen=window)
            _recent[user_id] = ring
        ring.append(hit)
        day = _utc_day()
        c = _day_cache.get(user_id)
        if c and c["day"] == day and hit:
            c["hits"] += 1
    if hit:
        _write(user_id, _utc_day(), hits=1)


def recent_hit_rate(user_id: Optional[str]) -> float:
    """Share of recent finals that cleared the shortlist bar.

    Returns 1.0 ("no evidence against") when too few finals have been scored to
    judge — a fresh process mid-day, or a user just starting their day. The
    burst zone is still bounded by burst and weekly, so an optimistic default
    cannot run away; a pessimistic one would silently cap every restart at soft.
    """
    window = max(1, settings.finals_yield_window)
    with _lock:
        ring = _recent.get(user_id)
        if ring is not None and len(ring) >= window:
            return sum(1 for h in ring if h) / float(len(ring))
    finals, hits = day_counts(user_id)
    if finals >= window:
        return hits / float(finals)
    return 1.0


# ── The decision ─────────────────────────────────────────────────────────────

class Allowance:
    """How many finals this user may buy right now, and how promising a
    candidate has to be to qualify."""
    __slots__ = ("n", "gate", "reason")

    def __init__(self, n: int, gate: int, reason: str):
        self.n = n
        self.gate = gate
        self.reason = reason

    def __repr__(self) -> str:          # pragma: no cover - debugging aid
        return f"Allowance(n={self.n}, gate={self.gate}, reason={self.reason!r})"


def normal_gate() -> int:
    """The everyday Tier-1 advance gate: min(advance, shortlist)."""
    return min(settings.prescore_advance_threshold, settings.shortlist_score_threshold)


def burst_gate() -> int:
    """Test A. Above the soft point only strong candidates are worth a final."""
    return max(normal_gate(), settings.finals_promise_floor)


def allowance(user_id: Optional[str], per_cycle_cap: int, soft_cap: int) -> Allowance:
    """The whole policy, in one place.

    A slice never straddles the soft boundary: below it the slice is clipped at
    the boundary, so the next cycle re-decides with the strict gate rather than
    spending burst money under everyday rules.
    """
    soft, burst, weekly = budgets(soft_cap)
    if soft <= 0:
        return Allowance(per_cycle_cap, normal_gate(), "no plan cap")

    spent_day, _hits = day_counts(user_id)
    week_left = weekly - week_finals(user_id)
    if week_left <= 0:
        return Allowance(0, normal_gate(), "weekly budget spent")

    if spent_day < soft:
        n = min(per_cycle_cap, soft - spent_day, week_left)
        return Allowance(max(0, n), normal_gate(), "within soft budget")

    if spent_day >= burst:
        return Allowance(0, burst_gate(), "daily burst ceiling")

    rate = recent_hit_rate(user_id)
    if rate < settings.finals_yield_continue_rate:
        return Allowance(0, burst_gate(),
                         f"yield {rate:.0%} below {settings.finals_yield_continue_rate:.0%}")

    n = min(per_cycle_cap, burst - spent_day, week_left)
    return Allowance(max(0, n), burst_gate(), f"burst earned (yield {rate:.0%})")


def reset_state() -> None:
    """Drop every in-process cache. For tests and the day roll."""
    with _lock:
        _day_cache.clear()
        _week_cache.clear()
        _recent.clear()
