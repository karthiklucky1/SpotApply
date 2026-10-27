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
import sys

# Chatty dependencies pinned to WARNING so lane diagnostics aren't buried.
_NOISY = ("httpx", "httpcore", "urllib3", "sentence_transformers",
          "faiss", "asyncio", "uvicorn.access", "hpack", "openai")

_HANDLER_MARKER = "_hirepath"


class _SplitStreamHandler(logging.StreamHandler):
    """stdout below ERROR, stderr from ERROR up.

    Railway tags EVERY stderr line as severity "error", and a plain
    StreamHandler writes everything to stderr — so every INFO heartbeat read
    as an error and the dashboard's error filter returned thousands of lines a
    minute, which is how the tailoring 400s of 2026-10-26 sat unnoticed among
    them. The stream is chosen per record inside ``emit``, which
    ``Handler.handle`` runs under the handler's lock, and looked up at emit time
    so a replaced ``sys.stdout`` (pytest capture) is honoured."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr if record.levelno >= logging.ERROR else sys.stdout
        super().emit(record)


def setup_logging(level: int | None = None) -> None:
    """Attach a single INFO handler to the root logger (idempotent — tagged so a
    re-import never stacks handlers) and quiet noisy libraries. ``level``
    defaults to the ``LOG_LEVEL`` env var (default INFO)."""
    if level is None:
        level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if not any(getattr(h, _HANDLER_MARKER, False) for h in root.handlers):
        handler = _SplitStreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        setattr(handler, _HANDLER_MARKER, True)  # marker: don't double-add on re-import
        root.addHandler(handler)
    root.setLevel(level)
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)
