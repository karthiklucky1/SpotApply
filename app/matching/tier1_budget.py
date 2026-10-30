"""Tier-1 (prescore) calls per user per UTC day: 2,000, or 5,000 on a setup day.

WHY (founder, 2026-10-30). Tier-1 was bounded only by two things that are not
about Tier-1: the Anthropic-fallback allowance (OpenAI prescores were never
counted) and USER_DAILY_PAID_CALL_CAP, the all-calls backstop. That backstop sat
at 2,000 and was what actually stopped people — one first-day user at 12:36 UTC
with 2,229 fresh jobs never looked at, another the evening before — while it
also bounded every Tier-2 final and cache prewarm in the same count. So Tier-1
now has its own number, and the backstop sits above it.

Measured 2026-10-01..30 (llm_spend, every call metered): p50 451, p90 1,528,
busiest established user 1,557 with nothing left waiting. A SETUP day — resume
upload, role save, Resume search (`adoption.seed_new_user`), the first day
included — adopts a 14-day catch-up window: one first day copied 7,200 jobs and
needed ~4,200 Tier-1 looks. Hence ``tier1_setup_day_cap`` that day.

THE COUNT IS OF REAL CALLS. `take()` is called inside `Reranker.prescore` only
once a provider is about to be asked: after the rule pre-filter and the
empty-description guard (free verdicts), after the provider cooldown and the
platform budget skips, and after the paid-call reservation. A provider outage
must not spend a user's day of Tier-1 on calls that never happen — then the
outage would outlive itself as a feed outage until 00:00 UTC.

A REFUSAL LEAVES THE JOB QUEUED. `prescore()` returns ``None`` to mean "could
not decide — advance to Tier-2" (fail-open), so a capped call must NOT look
like that: it would buy a Claude final for a job nobody judged. It returns
``TIER1_DEFERRED`` instead and every caller leaves the job where it is. A job
that already carries a kept prescore needs no Tier-1 call at all, so a capped
user's strongest waiting jobs still get their finals.

Persisted in ``platform_counter`` (`app/common/daily_counter.py`, one atomic
conditional UPDATE per call) so the number survives deploys; the names end in
``:user:<uid>`` so account deletion purges them.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)


class _Deferred:
    """The 'cap reached, leave it queued' answer. Not a tuple and not None, so
    no caller can mistake it for a score or for fail-open."""
    __slots__ = ()

    def __repr__(self) -> str:          # pragma: no cover - debugging aid
        return "TIER1_DEFERRED"


TIER1_DEFERRED = _Deferred()

_SETUP_TTL = 300.0
_lock = threading.Lock()
_setup_cache: dict = {}         # user_id -> {"day": date, "setup": bool, "at": monotonic}
_exhausted: dict = {}           # user_id -> (day, cap) the refusal was for


def _today() -> date:
    return datetime.utcnow().date()


def _counted(user_id: Optional[str]) -> bool:
    return bool(user_id) and user_id not in ("local", "__shared__")


def _name(user_id: str) -> str:
    return f"tier1:user:{user_id}"


def _setup_name(user_id: str) -> str:
    return f"tier1_setup:user:{user_id}"


def mark_setup_day(user_id: Optional[str]) -> None:
    """Today is a setup day for this user (resume upload / role save / Resume
    search): the higher cap applies until 00:00 UTC. Clears an earlier refusal,
    because the cap it was made under no longer holds."""
    if not _counted(user_id):
        return
    from app.common.daily_counter import reserve
    reserve(_setup_name(user_id), 0)          # 0 = record only
    with _lock:
        _setup_cache[user_id] = {"day": _today(), "setup": True, "at": time.monotonic()}
        _exhausted.pop(user_id, None)


def _is_setup_day(user_id: str) -> bool:
    day = _today()
    now = time.monotonic()
    with _lock:
        c = _setup_cache.get(user_id)
        if c and c["day"] == day and (c["setup"] or now - c["at"] < _SETUP_TTL):
            return c["setup"]
    from app.common.daily_counter import count
    setup = count(_setup_name(user_id), day) is not None
    with _lock:
        _setup_cache[user_id] = {"day": day, "setup": setup, "at": now}
    return setup


def daily_cap(user_id: Optional[str]) -> int:
    """This user's Tier-1 calls for today. 0 = unlimited."""
    base = max(0, int(settings.tier1_daily_cap or 0))
    if base == 0 or not _counted(user_id):
        return 0
    setup = max(base, int(settings.tier1_setup_day_cap or 0))
    return setup if setup > base and _is_setup_day(user_id) else base


def used_today(user_id: Optional[str]) -> Optional[int]:
    """Tier-1 calls counted today (None = no row yet / unreadable)."""
    if not _counted(user_id):
        return None
    from app.common.daily_counter import count
    return count(_name(user_id))


def exhausted(user_id: Optional[str]) -> bool:
    """True once a call was refused today under the cap that still applies.
    In-process and free: the lanes ask this per user per cycle, so they can
    stop selecting jobs that would need a Tier-1 call."""
    if not _counted(user_id):
        return False
    with _lock:
        mark = _exhausted.get(user_id)
    if not mark:
        return False
    day, cap = mark
    if day != _today():
        with _lock:
            _exhausted.pop(user_id, None)
        return False
    return cap == daily_cap(user_id)


def take(user_id: Optional[str]) -> bool:
    """Reserve ONE Tier-1 call for this user. False = today's cap is reached.

    A refusal is only remembered when the counter really is at the cap: the
    reservation also refuses on a database error ("money that cannot be
    accounted for is not spent"), and remembering THAT would stop a user's
    Tier-1 for the rest of the day over one blip.
    """
    if not _counted(user_id):
        return True
    from app.common.daily_counter import count, reserve
    cap = daily_cap(user_id)
    if cap <= 0:
        reserve(_name(user_id), 0)            # unlimited: count it, never refuse
        return True
    if exhausted(user_id):
        return False
    if reserve(_name(user_id), cap):
        return True
    used = count(_name(user_id))
    if used is not None and used >= cap:
        with _lock:
            first = _exhausted.get(user_id) != (_today(), cap)
            _exhausted[user_id] = (_today(), cap)
        if first:
            log.info("Tier-1 daily cap reached (%d calls) — fresh jobs stay queued; "
                     "kept prescores still reach Tier-2", cap)   # no user id in the line
    return False


def reset_state() -> None:
    """Tests only: forget the in-process caches."""
    with _lock:
        _setup_cache.clear()
        _exhausted.clear()
