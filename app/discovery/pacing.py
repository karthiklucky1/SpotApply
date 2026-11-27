"""One pace for every request this process sends to one site.

Some sites limit by ADDRESS, across everything we ask of them. Spacing the
requests of one board's poll does nothing for that: the pulse lane polls dozens
of boards of the same system at once from a thread pool, so the site sees a
burst however polite each poll is on its own.

Workday, 2026-11-21: with the requests of each poll spaced a second apart and
unchanged boards making no detail requests at all, 1,143 of 1,440 polls in
three hours were still answered 429 — on the FIRST request. The limit is on our
address across every tenant, and once it trips it stays tripped for minutes.

A ``Pace`` is that one shared clock:

  * ``take()`` waits for the next slot, but only briefly. When the queue is
    longer than the caller is willing to wait it answers False and the request
    is NOT sent — the board is retried in an hour (``BoardNotSentError``,
    ``pipeline.UNSENT_BOARD_BACKOFF_HOURS``) without having spent a request or
    held a fetch thread;
  * ``cool_down()`` is called when the site refuses: nobody asks again for a
    while. Asking the next board straight after a refusal is how a throttle
    becomes a block.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class Pace:
    def __init__(self, interval: Callable[[], float], max_wait: Callable[[], float]):
        self._interval = interval
        self._max_wait = max_wait
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def take(self, patience: float = 1.0) -> bool:
        """Wait for this process's next slot. False = the queue is too long:
        do not send.

        ``patience`` multiplies how long this caller will wait. A poll that has
        already spent requests on a board asks with more than 1: giving up on
        page two throws page one's request away, and it is the large boards —
        the ones with the most postings — that need several pages.
        """
        interval = float(self._interval() or 0.0)
        if interval <= 0:
            return True
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            wait = slot - now
            if wait > float(self._max_wait() or 0.0) * max(1.0, float(patience)):
                return False
            self._next_slot = slot + interval
        if wait > 0:
            time.sleep(wait)
        return True

    def cool_down(self, seconds: float) -> None:
        """The site refused us: hold every request for this long."""
        if seconds and seconds > 0:
            with self._lock:
                self._next_slot = max(self._next_slot, time.monotonic() + float(seconds))

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._next_slot = 0.0
