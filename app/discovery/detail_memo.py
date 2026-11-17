"""Fetch a posting's detail ONCE.

Three job systems publish a listing with no description in it — Rippling
(title, department, location), join.com (title, city, work mode) and Breezy
(title, location, type). The description is one more request PER POSTING, and
until 2026-11-15 these adapters simply did not make it: every row from them was
stored with an empty description (431 of 431 fresh rows in a day), so the cheap
tier waved it through as "no description" and the expensive tier scored it on a
title.

The naive fix — fetch every posting's detail on every poll, as BambooHR does —
is exactly the load that gets an address refused: Rippling's own board lists
717 postings, and the fast tier polls it twelve times an hour.

So a detail is fetched once per posting:

  * the adapter asks ``needs_detail(source, id, fingerprint)``. A posting the
    pool already holds WITH its description is returned as a *listing-only*
    row (``RawJob.listing_only``): the upsert refreshes its ``last_seen`` and
    writes nothing else, so an empty description can never overwrite a real one;
  * a posting the pool does not hold yet gets its detail fetched — if the
    source's budget (``take``) allows. With no budget the posting is simply not
    emitted this poll and comes back on the next one. A blind row is never
    written to "fill the gap";
  * the memory of what the pool holds is written by the UPSERT
    (``remember``), not by the adapter: a fetched result the lane then dropped
    (a tick that ran out of time) was never stored, and must be fetched again.
    A listing-only row whose shared row turns out to be missing is forgotten
    (``forget``) for the same reason.

The fingerprint is the listing-phase title + location. When either changes the
posting counts as new, so an edited posting gets its detail — and through it
its description, location and geography — re-read.

Process-local on purpose: a set of ids per source, loaded once from the shared
pool (an index-only read) and kept current by the upsert. No table, no
per-poll query.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Dict, Iterable, Optional

from app.config import settings

log = logging.getLogger(__name__)

# sha256("") — the content hash of a row stored with no description.
EMPTY_CONTENT_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_MISSING = object()
_lock = threading.Lock()
# source -> {external_id: fingerprint | None}. None = known from the database,
# fingerprint not seen yet: the first listing that names it supplies one.
_known: Dict[str, Dict[str, Optional[str]]] = {}
_load_failed_at: Dict[str, float] = {}
# Ids forgotten once already (see `forget`).
_refetched: Dict[str, set] = {}
_LOAD_RETRY_SECONDS = 600.0
_MAX_PER_SOURCE = 400_000

# source -> [tokens, last refill (monotonic)]
_buckets: Dict[str, list] = {}
_RATES: tuple[str, dict] = ("", {})


def _rates() -> dict:
    """``DETAIL_FETCH_PER_MINUTE`` ("rippling=20,join=20,breezy=30") parsed once."""
    global _RATES
    raw = str(getattr(settings, "detail_fetch_per_minute", "") or "")
    if _RATES[0] != raw:
        parsed: dict = {}
        for part in raw.split(","):
            name, _, per_min = part.partition("=")
            try:
                value = float(per_min.strip())
            except ValueError:
                continue
            if name.strip() and value > 0:
                parsed[name.strip().lower()] = value
        _RATES = (raw, parsed)
    return _RATES[1]


def enabled(source: str) -> bool:
    """True when this source fetches details once (it has a budget)."""
    return bool(getattr(settings, "detail_once_enabled", True)) and source in _rates()


def max_per_poll() -> int:
    return max(1, int(getattr(settings, "detail_max_per_poll", 25) or 25))


def fingerprint(title: str, location: str) -> str:
    """Listing-phase identity of a posting's content: what the LISTING says."""
    raw = f"{(title or '').strip()}|{(location or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _load(source: str) -> Dict[str, Optional[str]]:
    """Ids of this source's shared rows that already carry a description."""
    table: Dict[str, Optional[str]] = {}
    try:
        from sqlmodel import select

        from app.db.init_db import get_session
        from app.db.models import Job, JobSource
        from app.discovery.pipeline import SHARED_POOL_USER
        with get_session() as session:
            rows = session.exec(
                select(Job.external_id).where(
                    Job.user_id == SHARED_POOL_USER,
                    Job.source == JobSource(source),
                    Job.content_hash != EMPTY_CONTENT_HASH,
                    Job.content_hash.is_not(None),
                ).limit(_MAX_PER_SOURCE)
            ).all()
        for ext in rows:
            if ext:
                table[str(ext)] = None
        _load_failed_at.pop(source, None)
        log.info("detail memo: %s — %d postings already hold a description", source, len(table))
    except Exception as e:
        # Unknown is not "nothing known" for long: everything looks new until
        # the read succeeds, which only costs budgeted fetches, and it is
        # retried rather than believed.
        _load_failed_at[source] = time.monotonic()
        log.warning("detail memo: could not load %s (%s) — treating as empty for now", source, e)
    return table


def _table(source: str) -> Dict[str, Optional[str]]:
    table = _known.get(source)
    failed_at = _load_failed_at.get(source)
    if table is not None and not (
            failed_at and time.monotonic() - failed_at > _LOAD_RETRY_SECONDS):
        return table
    with _lock:
        table = _known.get(source)
        failed_at = _load_failed_at.get(source)
        if table is None or (failed_at and time.monotonic() - failed_at > _LOAD_RETRY_SECONDS):
            loaded = _load(source)
            if table:
                loaded.update(table)        # keep what the upsert taught us meanwhile
            _known[source] = table = loaded
    return table


def needs_detail(source: str, external_id: str, fp: str) -> bool:
    """True when the pool does not hold this posting's detail yet, or the
    listing now says something different about it."""
    table = _table(source)
    current = table.get(external_id, _MISSING)
    if current is _MISSING:
        return True
    if current is None:
        table[external_id] = fp             # adopt what the listing says now
        return False
    return current != fp


def remember(source: str, external_id: str, fp: Optional[str]) -> None:
    """The upsert was handed this posting WITH its detail."""
    if not external_id:
        return
    table = _table(source)
    if len(table) < _MAX_PER_SOURCE or external_id in table:
        table[external_id] = fp


def forget(source: str, external_ids: Iterable[str]) -> None:
    """A listing-only row had no stored row behind it: fetch its detail again —
    ONCE. A posting the pool then declines a second time (the title gate, a
    cross-source duplicate) is one the pool does not keep, and asking for its
    detail on every other poll would spend the budget on rows that are never
    written. It stays known until the process restarts.
    """
    table = _known.get(source)
    if not table:
        return
    asked = _refetched.setdefault(source, set())
    for ext in external_ids:
        ext = str(ext)
        if ext in asked:
            continue
        if table.pop(ext, _MISSING) is not _MISSING and len(asked) < _MAX_PER_SOURCE:
            asked.add(ext)


def take(source: str) -> bool:
    """One detail request from this source's budget, or False when it is spent.

    A token bucket: ``rate`` requests a minute, at most a minute's worth saved
    up. Never blocks — a posting without budget waits for the next poll.
    """
    rate = _rates().get(source, 0.0)
    if rate <= 0:
        return False
    now = time.monotonic()
    with _lock:
        bucket = _buckets.get(source)
        if bucket is None:
            bucket = _buckets[source] = [rate, now]
        tokens, last = bucket
        tokens = min(rate, tokens + (now - last) * rate / 60.0)
        if tokens < 1.0:
            bucket[0], bucket[1] = tokens, now
            return False
        bucket[0], bucket[1] = tokens - 1.0, now
        return True


class Poll:
    """One board poll's detail decisions, so three adapters share one rule.

        poll = detail_memo.Poll("rippling")
        action, fp = poll.decide(external_id, title, location)

    ``action`` is one of:
      "plain"  — the feature is off for this source: emit the row as the
                 listing gives it (no description), exactly as before;
      "known"  — the pool holds this posting's detail: emit ``listing_only``;
      "fetch"  — fetch the detail now, then call ``fetched()`` or ``failed()``;
      "skip"   — no budget (or details are off for this poll): do not emit the
                 posting; it comes back on the next poll.
    """

    def __init__(self, source: str):
        self.source = source
        self.on = enabled(source)
        self.cap = max_per_poll()
        self.n_fetched = 0
        self.n_skipped = 0
        self.off = False

    def decide(self, external_id: str, title: str, location: str) -> tuple[str, Optional[str]]:
        if not self.on:
            return "plain", None
        fp = fingerprint(title, location)
        if not needs_detail(self.source, external_id, fp):
            return "known", fp
        if self.off or self.n_fetched >= self.cap or not take(self.source):
            self.n_skipped += 1
            return "skip", fp
        return "fetch", fp

    def fetched(self) -> None:
        self.n_fetched += 1

    def failed(self, stop: bool = False) -> None:
        """The detail did not arrive: the posting waits for the next poll.
        ``stop`` = the site is not answering details; ask for no more this poll."""
        self.n_skipped += 1
        if stop:
            self.off = True

    @property
    def ingest_required(self) -> bool:
        """A detail the pool does not hold was fetched: the lane must ingest
        this result even when the listing itself reads unchanged."""
        return self.n_fetched > 0


def stats() -> dict:
    """Counts for logs and the admin health page. Names only."""
    return {source: {"known": len(table)} for source, table in _known.items()}


def reset() -> None:
    """Tests only."""
    with _lock:
        _known.clear()
        _buckets.clear()
        _load_failed_at.clear()
        _refetched.clear()
