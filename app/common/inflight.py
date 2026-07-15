"""Process-local claim registry for jobs currently being LLM-scored.

Three lanes score jobs concurrently (90s scoring lane, 5-min matching lane,
pulse fast path). All of them select work with ``rerank_score IS NULL``, so two
lanes can pick up the SAME job in the seconds before either writes a score —
the write side is already idempotent (last writer loses, no data harm), but the
LLM call itself gets paid twice.

Everything runs in ONE uvicorn process (single Railway container, no worker
fan-out), so a locked in-memory set is a complete fix — no schema change, no
row locks held across LLM calls, no Redis.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Set

_inflight: Set[int] = set()
_lock = threading.Lock()


def try_claim(jid: int) -> bool:
    """Claim a job for scoring. False = another lane is scoring it right now."""
    with _lock:
        if jid in _inflight:
            return False
        _inflight.add(jid)
        return True


def release(jid: int) -> None:
    with _lock:
        _inflight.discard(jid)


@contextmanager
def claim(jid: int) -> Iterator[bool]:
    """``with claim(jid) as ok:`` — ok says whether we own the job; the claim is
    always released on exit (success, failure, or exception)."""
    ok = try_claim(jid)
    try:
        yield ok
    finally:
        if ok:
            release(jid)
