"""Retry-once wrapper for Postgres deadlocks (SQLSTATE 40P01).

Every multi-row companyregistry writer acquires its row locks in ascending
primary-key order (see ``pulse_lane._flush_polls``), which turns would-be
deadlocks between OUR writers into plain waits. A deadlock can still be handed
to us by a transaction outside that discipline — another process during a
deploy overlap, or a session outside the app entirely — and every batch
registry write is idempotent, so the correct response is one quiet retry, not
a lost write.

Deliberately narrow: one retry, deadlocks only. Anything else re-raises so the
caller's own error handling (all of these writers warn-and-continue) still
owns the failure.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import DBAPIError

log = logging.getLogger(__name__)

T = TypeVar("T")

_DEADLOCK_SQLSTATE = "40P01"


def is_deadlock(exc: BaseException) -> bool:
    """True when the exception is a Postgres deadlock, however it is wrapped."""
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "pgcode", None) == _DEADLOCK_SQLSTATE


def run_with_deadlock_retry(label: str, fn: Callable[[], T],
                            delay_seconds: float = 0.25) -> T:
    """Run ``fn`` (which must open its OWN session/transaction so a retry gets
    a fresh one), retrying exactly once if Postgres kills it as a deadlock
    victim. The retried transaction re-acquires its locks from scratch, and the
    partner that won the first round has committed by then."""
    try:
        return fn()
    except DBAPIError as e:
        if not is_deadlock(e):
            raise
        log.warning("%s: deadlock victim (40P01) — retrying once", label)
        time.sleep(delay_seconds)
        return fn()
