"""Adaptive finals budget — spend Claude on evidence, not on a counter.

The flat per-plan `finals_daily` cap was wrong in both directions on the same
day: it stopped a strong user at 50 while the 50th final was still returning
70-fits, and it happily spent 50 finals on a Saturday proving that 20 weak jobs
are weak. This module replaces "a fixed number of results" with "a bounded
amount of money, spent while the evidence says the next candidate is worth it".

TWO numbers (PRO shown; every plan scales the same way from
PLAN_LIMITS["finals_daily"]). This budget bounds COST. What a user is promised
is now stated in PLAN_LIMITS["shortlist_daily"] — jobs delivered to the board —
because that is the number they can see:

    soft   =  50/day    normal spending point — spent freely, no justification
    burst  = 100/day    absolute ceiling for one UTC day

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
reset on every deploy, which is exactly why the Aug 14-21 stall appeared to
"heal" on restart. The ledger is the source of truth; a tiny per-process cache
keeps the hot path off the database.

PACING (2026-09-03). The numbers above say how much a user may spend; on their
own they said nothing about WHEN, and the answer in production was "all of it
in the first hour". Both active users' finals for 2026-09-02 and 09-03 were
spent in the 00:xx UTC hour (176 and 87 finals) and the other 23 hours scored
nothing, so every posting appearing during the US working day (13:00-01:00 UTC)
waited for the next midnight. The DAY budget is therefore released along a
curve (`day_fraction`): a head start at 00:00 UTC, the rest linearly through
the day. What is not spent stays available — the curve only bounds spend from
above, and at 24:00 it reads the full budget, so HOW MUCH is unchanged.

NO WEEKLY CEILING (removed 2026-09-05). There was one, released along its own
curve, and it took production to zero finals for four days. The curve was
applied to a ledger that already held a front-loaded week: users who had spent
normally under the previous all-at-midnight rules were instantly past what the
week had "released" (300 spent against 158 released on the Wednesday) and
stayed at zero until Sunday. Worse, `allowance` reported it as reason "paced",
which the lane counts as the budget working rather than a stall — so the
outage produced no warning at all. Two lessons, both encoded here now: a
spend control must never be able to retroactively invalidate spend already
made, and a reason that means "you get nothing" must never be filed under
"healthy".
"""
from __future__ import annotations

import logging
import math
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
_recent: dict = {}              # user_id -> deque[bool], the last N final outcomes
_lock = threading.Lock()

_DAY_CACHE_TTL = 30.0           # seconds


def _utc_now() -> datetime:
    """THE clock: the ledger's day key and the pacing curves both read it, so a
    test that pins it moves the whole budget to that moment consistently."""
    return datetime.utcnow()


def _utc_day() -> date:
    return _utc_now().date()


def _week_start(d: Optional[date] = None) -> date:
    """Monday of that week. No longer a budget boundary — it only stamps
    UserUsage.week_start so weekly spend stays reportable."""
    d = d or _utc_day()
    return d - timedelta(days=d.weekday())      # Monday


def _ledgerable(user_id: Optional[str]) -> bool:
    """'local' and the anonymous case have no plan and no ledger row."""
    return bool(user_id) and user_id != "local"


# ── Plan → the three budgets ─────────────────────────────────────────────────

def budgets(soft_cap: int) -> Tuple[int, int]:
    """(soft, burst) derived from the plan's daily allowance.

    There is no weekly ceiling any more. It was removed 2026-09-05 after it
    took production to zero finals for four days: the weekly release curve was
    applied to a ledger that already held a front-loaded week, so users who had
    spent normally under the previous rules were instantly past what the week
    had "released" and stayed there until the curve caught up. A control that
    can retroactively invalidate spend already made is the wrong shape, and the
    per-day soft/burst pair bounds cost on its own.
    """
    soft = max(0, int(soft_cap))
    burst = int(round(soft * max(1.0, settings.finals_burst_multiplier)))
    return soft, burst


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
    from sqlalchemy.exc import IntegrityError
    from app.db.init_db import get_session
    from app.db.models import UserUsage
    # TWO attempts, because of the first final of a user's day: there is no row
    # to UPDATE, so several workers finishing together all see rowcount 0 and
    # all INSERT. uq_user_usage_date lets exactly one win, and a loser that
    # simply swallowed the IntegrityError would DROP its increment — on the
    # 00:00 UTC burst, every day, in the overspend direction. The loser retries
    # the UPDATE against the row the winner just created.
    for attempt in range(2):
        try:
            with get_session() as session:
                res = session.execute(
                    # COALESCE, not a bare +: a row that predates these columns
                    # can hold NULL if the ALTER ever lands without its DEFAULT,
                    # and NULL + 1 is NULL — a counter that silently never
                    # counts is unlimited spend.
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
            return
        except IntegrityError:
            if attempt == 0:
                continue          # another worker created today's row — UPDATE it
            log.warning("finals ledger write LOST for %s (%+d finals, %+d hits): the "
                        "insert race did not settle — spend is under-counted by that "
                        "much", user_id, finals, hits)
        except Exception as e:
            log.debug("finals ledger write failed for %s (%+d finals, %+d hits): %s",
                      user_id, finals, hits, e)
            return


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
    burst zone is still bounded by the daily burst ceiling, so an optimistic
    default cannot run away; a pessimistic one would silently cap every restart
    at soft.
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


# ── Pacing: WHEN the money is available ──────────────────────────────────────

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def day_fraction(now: Optional[datetime] = None) -> float:
    """Share of a DAY budget released by ``now`` (UTC), in 0..1.

    head + (1 - head) x elapsed/24h. The head start (`finals_pace_head_start`,
    PRO: 15% = 8 finals) is available at 00:00 so postings that arrive overnight
    are not frozen out; the rest accrues linearly (PRO: ~1.8 finals/hour). It
    is a ceiling on cumulative spend, never a quota per hour: a quiet morning's
    unspent share is still there for the afternoon. Pacing off -> 1.0, which
    is the pre-2026-09 behaviour (everything available at 00:00).
    """
    if not settings.finals_pace_enabled:
        return 1.0
    now = now or _utc_now()
    head = _clamp01(settings.finals_pace_head_start)
    elapsed = (now.hour * 3600 + now.minute * 60 + now.second) / 86400.0
    return _clamp01(head + (1.0 - head) * elapsed)



def paced(total: int, fraction: float) -> int:
    """How much of ``total`` the curve has released so far.

    Rounded UP so the first final is available as soon as any share is, and
    the epsilon keeps float noise (0.3 x 50 = 15.000000000000002) from
    releasing a final early; never above ``total``.
    """
    total = max(0, int(total))
    if total == 0:
        return 0
    return max(0, min(total, int(math.ceil(total * _clamp01(fraction) - 1e-9))))


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

    Both budgets are read THROUGH the day's pacing curve: `soft_now`/`burst_now`
    are the shares released so far. A slice never straddles the (paced) soft
    boundary: below it the slice is clipped at the boundary, so the next cycle
    re-decides with the strict gate rather than spending burst money under
    everyday rules.

    A "paced" reason means the money exists but today has not released it yet.
    It can only ever hold a user back until later the SAME day — the curve
    reaches 100% at 24:00 — which is what makes it safe for scoring_lane to
    count separately from hard stops.
    """
    soft, burst = budgets(soft_cap)
    if soft <= 0:
        return Allowance(per_cycle_cap, normal_gate(), "no plan cap")

    spent_day, _hits = day_counts(user_id)
    dfrac = day_fraction()
    soft_now, burst_now = paced(soft, dfrac), paced(burst, dfrac)

    if spent_day < soft_now:
        n = min(per_cycle_cap, soft_now - spent_day)
        return Allowance(max(0, n), normal_gate(), "within soft budget")

    if spent_day >= burst:
        return Allowance(0, burst_gate(), "daily burst ceiling")
    if spent_day >= burst_now:
        return Allowance(0, burst_gate(),
                         f"paced: day {spent_day}/{burst_now} released ({dfrac:.0%} of day)")

    rate = recent_hit_rate(user_id)
    if rate < settings.finals_yield_continue_rate:
        return Allowance(0, burst_gate(),
                         f"yield {rate:.0%} below {settings.finals_yield_continue_rate:.0%}")

    n = min(per_cycle_cap, burst_now - spent_day)
    return Allowance(max(0, n), burst_gate(), f"burst earned (yield {rate:.0%})")


def reset_state() -> None:
    """Drop every in-process cache. For tests and the day roll."""
    with _lock:
        _day_cache.clear()
        _recent.clear()
