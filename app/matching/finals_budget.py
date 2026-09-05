"""When to keep buying final scores — measured in jobs DELIVERED, not calls made.

THE OBJECTIVE (2026-09-05). This used to pace a fixed number of Tier-2 calls
across the day: PRO got 50 finals, released at ~1.77/hour, and the lane stopped
when the counter ran out. That optimises the wrong thing. Nobody wants 50 LLM
calls; they want the day's qualified jobs on their board, early. The measured
result of pacing calls was a p50 of 685 minutes from prescore to final score —
a job found at 09:00 waited until evening for a slot it shared with last week's
backlog, against a product whose whole promise is applying first.

So the budget now asks one question:

    has this user received the jobs their plan promises today?

    target   PLAN_LIMITS[plan]["shortlist_daily"]   20 Free / 35 Pro
    ceiling  PLAN_LIMITS[plan]["finals_daily"]      the money guard, nothing else

Scoring runs at full speed until the target is met, then stops. No curve, no
drip, no per-hour release. A quiet pool costs nothing because there is nothing
to score; a rich morning is spent in the morning, which is the point.

WHICH STOP ACTUALLY FIRES — do not read the target as a promise. A hit rate of
~10% (of 57,309 real Claude finals, 11.6% cleared 65 and fewer clear 70) means
35 delivered jobs costs on the order of 350 finals, and the Pro ceiling is 250.
So on a rich day the CEILING is what stops a user, at roughly 25 delivered, and
the target is an early-out for the days when the pool is unusually good. That is
the honest reading, and it is why the ceiling is sized as an allocation you can
afford every day rather than as a rare-disaster bound. Raising delivery means
raising `finals_daily` and paying for it; only better Tier-1 precision makes it
free.

Promise-ordering (scoring_lane._user_queue) is what decides WHICH 250 you buy —
it does not make 350 cost less. It matters most exactly because the ceiling
binds: the finals that never happen are the bottom of the queue, not a random
tail.

THREE STOPS, in order:

    1. delivered >= target   the goal is met. The cheapest possible stop, and
                             the only one that means success.
    2. spent >= ceiling      the money guard, and the usual stop. Bounds the
                             day at a price the plan can carry.
    3. yield collapsed       today's hits/finals below `finals_yield_continue_
                             rate`, judged only once `finals_yield_window`
                             finals have actually been scored today. Catches a
                             Tier-1 that has started promising jobs Claude
                             rejects. Read `recent_hit_rate` before touching
                             either number: an under-powered version of this
                             guard fires on healthy users by coin-flip.

Nothing here chases the target. If the pool holds six jobs worth showing, the
user gets six and the rest of the ceiling is never spent.

DURABILITY. The counters that bound money are persisted to `UserUsage`
(finals_count / finals_hits, one row per user per UTC day). In-memory counters
reset on every deploy, which is why the Aug 14-21 stall appeared to "heal" on
restart. The ledger is the source of truth; a small per-process cache keeps the
hot path off the database.

NO WINDOW LONGER THAN A DAY. There was a weekly ceiling, released along its own
curve, and on 2026-09-03 it took production to zero finals for 39 hours: the
curve was applied to a ledger that already held a front-loaded week, so users
who had spent normally were instantly past what the week had "released" and
stayed there. `allowance` reported it as reason "paced", which the lane counts
as the budget working rather than a stall, so the outage produced no warning at
all. Two rules came out of that, both pinned by test: a spend control must never
be able to retroactively invalidate spend already made, and a reason that means
"you get nothing" must never be filed under "healthy".
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

# (user_id, day) -> {"finals": n, "hits": n, "at": monotonic}. Authoritative
# value lives in UserUsage; this only avoids a SELECT per decision. Writes
# increment the cached value in place, so within one process it never lags.
_day_cache: dict = {}
_delivered_cache: dict = {}     # user_id -> {"day": date, "n": int, "at": monotonic}
_lock = threading.Lock()

_DAY_CACHE_TTL = 30.0           # seconds
_DELIVERED_TTL = 20.0           # shorter: this one decides when to STOP


def _utc_now() -> datetime:
    """THE clock. A test that pins it moves the whole budget consistently."""
    return datetime.utcnow()


def _utc_day() -> date:
    return _utc_now().date()


def _week_start(d: Optional[date] = None) -> date:
    """Monday of that week. NOT a budget boundary — it only stamps
    UserUsage.week_start so weekly spend stays reportable."""
    d = d or _utc_day()
    return d - timedelta(days=d.weekday())


def _ledgerable(user_id: Optional[str]) -> bool:
    """'local' and the anonymous case have no plan and no ledger row."""
    return bool(user_id) and user_id != "local"


# ── The ledger ───────────────────────────────────────────────────────────────

def _read_day(user_id: str, day: date) -> tuple[int, int]:
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

    The increment is an ATOMIC `SET x = x + n`, never a read-modify-write: 20
    scoring workers finish finals concurrently and read-then-write loses
    increments under exactly that concurrency. A lost increment on a money
    counter means overspend, which is the one direction this must not fail in.

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
    # simply swallowed the IntegrityError would DROP its increment. The loser
    # retries the UPDATE against the row the winner just created.
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


def day_counts(user_id: Optional[str]) -> tuple[int, int]:
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


def delivered_today(user_id: Optional[str], cached: bool = True) -> int:
    """Jobs SpotApply put on this user's board today.

    THE definition, for everyone. It is what the budget aims at AND what the
    three shortlist caps enforce (matching/pipeline, strategy/pulse_lane,
    strategy/scoring_lane), and those two must agree exactly: the budget's whole
    job is to buy finals for jobs the cap will actually accept. They were four
    separate copies of the same query, which is three chances to drift.

    Counted from `application` rather than kept as a counter, so it is
    self-healing — a lost increment would silently buy a user a second day's
    worth of scoring. func.count, never len(all()): the lanes' copies each
    materialised every row of the day on a hot path.

    EMAIL IMPORTS DO NOT COUNT. Importing your Gmail history writes Application
    rows dated today for applications you made elsewhere, months ago. Counting
    those would let one import fill the day's quota and switch the feed off —
    and the dashboard already labels them "applied outside SpotApply".

    ``cached=False`` for the shortlist loops, which increment a local count as
    they go and must not read a value from 20 seconds ago.
    """
    day = _utc_day()
    now = time.monotonic()
    key = user_id or "__anon__"
    if cached:
        with _lock:
            c = _delivered_cache.get(key)
            if c and c["day"] == day and (now - c["at"]) < _DELIVERED_TTL:
                return c["n"]
    try:
        from sqlalchemy import func
        from sqlmodel import select
        from app.db.init_db import get_session
        from app.db.models import Application
        uid_arg = user_id if _ledgerable(user_id) else None
        start = datetime.combine(day, datetime.min.time())
        with get_session() as session:
            q = select(func.count(Application.id)).where(
                Application.created_at >= start,
                Application.apply_track != "email_import",
            )
            q = q.where(Application.user_id == uid_arg) if uid_arg \
                else q.where(Application.user_id.is_(None))
            v = session.exec(q).one()
            n = int(v[0] if isinstance(v, tuple) else v)
    except Exception as e:                                  # pragma: no cover
        # A read failure must not stop scoring, and must not stop a job reaching
        # the board — but "fail open to 0" alone is wrong in BOTH consumers: it
        # removes the budget's first stop (leaving the ceiling to bound a day
        # 7x larger) and it removes the shortlist cap. So prefer today's last
        # known value, however stale, and only fall back to 0 when there has
        # never been one. Deliberately does NOT refresh the cache timestamp: the
        # next call retries the query rather than settling on a stale number.
        with _lock:
            c = _delivered_cache.get(key)
            last = c["n"] if c and c["day"] == day else 0
        log.debug("delivered_today failed for %s (%s) — using last known %d",
                  user_id, e, last)
        return last
    with _lock:
        _delivered_cache[key] = {"day": day, "n": n, "at": now}
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
    _write(user_id, day, finals=1)


def record_outcome(user_id: Optional[str], score: float) -> None:
    """The final's verdict. A 'hit' is a score at or above the shortlist bar —
    the only definition that matches what the user actually receives."""
    hit = float(score) >= float(settings.shortlist_score_threshold)
    if not _ledgerable(user_id) or not hit:
        return
    day = _utc_day()
    with _lock:
        c = _day_cache.get(user_id)
        if c and c["day"] == day:
            c["hits"] += 1
    _write(user_id, day, hits=1)


def recent_hit_rate(user_id: Optional[str]) -> tuple[float, bool]:
    """(hit rate today, is that a verdict?) from the DAY LEDGER.

    THE SAMPLE GATE IS THE WHOLE DESIGN. Below `finals_yield_window` finals
    today there is no verdict and the caller must not stop: read the arithmetic
    before changing either number.

    A hit is a final at or above `shortlist_score_threshold` (70). Of 57,309
    real Claude finals only 11.6% cleared 65, so call the true rate ~8-10%. A
    run of consecutive misses is therefore ORDINARY, not evidence:

        P(0 hits in 10 finals | p=0.10) = 0.9^10  = 35%
        P(0 hits in 50 finals | p=0.10) = 0.9^50  = 0.5%

    The first version of this guard used a 10-final window at a 10% continue
    rate — i.e. it fired on roughly a third of perfectly healthy users, and it
    read a LIVE in-process ring that only a purchased final can append to. Once
    it fired, the user could buy no final, so nothing could ever refresh the
    evidence: zero finals for that user until the next deploy. An absorbing
    state reached by coin-flip, in the guard whose stated job is preventing
    all-day stalls.

    Two properties fix that and both are load-bearing:
      * the ledger, not a ring — it is per UTC DAY, so a new day always reopens
        the spend no matter what yesterday looked like, and a restart mid-day
        reads the true picture instead of an empty one;
      * a sample gate large enough that a stop means something. Within a day
        the stop is still terminal by construction (no finals => no new
        evidence), which is exactly what "this user's Tier-1 is broken today"
        should mean — and unlike the ring version it is bounded by the day, and
        the lane reports it as a user who stopped SHORT (a warning), never as
        healthy.
    """
    window = max(1, settings.finals_yield_window)
    finals, hits = day_counts(user_id)
    if finals < window:
        return 1.0, False
    return hits / float(finals), True


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
    """The Tier-1 advance gate: min(advance, shortlist). Below it a job is a
    misfit and is drained; at or above it it is worth an authoritative look.
    ONE gate — there is no burst zone to raise it for any more."""
    return min(settings.prescore_advance_threshold, settings.shortlist_score_threshold)


def allowance(user_id: Optional[str], per_cycle_cap: int,
              ceiling: int, target: int) -> Allowance:
    """The whole policy.

    ``ceiling`` is the plan's hard daily cost cap (finals_daily); ``target`` is
    the plan's daily shortlist promise (shortlist_daily). Full speed until the
    target lands, then stop.
    """
    delivered = delivered_today(user_id)
    if target > 0 and delivered >= target:
        return Allowance(0, normal_gate(),
                         f"delivered {delivered}/{target} — the day's jobs are on the board")

    spent, _hits = day_counts(user_id)
    if ceiling > 0 and spent >= ceiling:
        return Allowance(0, normal_gate(),
                         f"daily cost ceiling ({spent}/{ceiling} finals) at "
                         f"{delivered}/{target} delivered")

    rate, evidenced = recent_hit_rate(user_id)
    if evidenced and rate < settings.finals_yield_continue_rate:
        return Allowance(0, normal_gate(),
                         f"yield {rate:.1%} below {settings.finals_yield_continue_rate:.1%} "
                         f"over {spent} finals today "
                         f"— Tier-1 is promising jobs the final score rejects")

    n = per_cycle_cap if ceiling <= 0 else min(per_cycle_cap, ceiling - spent)
    return Allowance(max(0, n), normal_gate(), f"delivering {delivered}/{target}")


def reset_state() -> None:
    """Drop every in-process cache. For tests and the day roll."""
    with _lock:
        _day_cache.clear()
        _delivered_cache.clear()
