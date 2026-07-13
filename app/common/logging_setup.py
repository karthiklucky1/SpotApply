"""Make the app's INFO logs actually appear in production.

Prod runs ``uvicorn app.api.server:app``, which configures its OWN (``uvicorn.*``)
loggers but never the root logger — so nothing handles our ``log.info(...)``
records and Python's last-resort handler prints only WARNING+. Result: scraper
404 warnings showed, but every lane diagnostic (hot-lane heartbeat, scheduler
runs, fresh lane, matching) was silently dropped, making it impossible to tell
from logs whether the hot lane was even running.

Kept dependency-light so it imports and unit-tests without the app stack.
"""
from __future__ import annotations

import logging
import os

# Chatty dependencies pinned to WARNING so lane diagnostics aren't buried.
_NOISY = ("httpx", "httpcore", "urllib3", "sentence_transformers",
          "faiss", "asyncio", "uvicorn.access", "hpack", "openai")

_HANDLER_MARKER = "_hirepath"


def setup_logging(level: int | None = None) -> None:
    """Attach a single INFO handler to the root logger (idempotent — tagged so a
    re-import never stacks handlers) and quiet noisy libraries. ``level``
    defaults to the ``LOG_LEVEL`` env var (default INFO)."""
    if level is None:
        level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if not any(getattr(h, _HANDLER_MARKER, False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        setattr(handler, _HANDLER_MARKER, True)  # marker: don't double-add on re-import
        root.addHandler(handler)
    root.setLevel(level)
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)
