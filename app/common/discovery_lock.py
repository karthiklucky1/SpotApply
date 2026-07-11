"""Process-wide serialization for the heavy discovery/matching lanes.

Full discovery (6h), the fresh lane (2h), the hot lane (20min), and the manual
"Discover" button all load the embedding model + a user's whole job pool into
memory to rebuild the FAISS index. Running two at once multiplies memory and
OOM-crashed the container. A single threading.Lock serializes them — it works
across both the asyncio.to_thread scheduled lanes and the sync FastAPI
background task the Discover button uses.

Use ``with discovery_guard():`` around the heavy body. Non-scheduled callers
that would rather skip than wait can use ``discovery_guard(blocking=False)``.
"""
from __future__ import annotations

import contextlib
import logging
import threading

log = logging.getLogger(__name__)

_LOCK = threading.Lock()


@contextlib.contextmanager
def discovery_guard(blocking: bool = True, label: str = "discovery"):
    """Serialize heavy discovery/matching work. Yields True if the lock was
    acquired (work should run), False if not (caller should skip)."""
    acquired = _LOCK.acquire(blocking=blocking)
    if not acquired:
        log.info("%s skipped — another discovery/matching pass is running", label)
        try:
            yield False
        finally:
            pass
        return
    try:
        yield True
    finally:
        _LOCK.release()
