"""The last thing that happens before a job reaches a user's board.

The live audit found 5 of 45 shortlisted jobs (11%) already dead when the user
got them. This module is the gate that stops that, and its whole design is
shaped by one rule from `app/discovery/liveness.py`:

    an endpoint that refuses to answer is NOT evidence that the job is gone.

So a 429, a 403, a timeout or a parser failure can never block delivery. Only
REMOVED and EXPIRED can, and only ever from conclusive evidence: a 404 on the
exact posting permalink, a 410, explicit removal wording in the page, or
absence from a board fetch we know was COMPLETE.

Three properties the placement path depends on:

* **Late.** The check runs immediately before `slate.place()`, so we only spend
  a request on a job that would otherwise appear on someone's board. Discovery
  and scoring never call it — they handle orders of magnitude more candidates.
* **Outside the session.** `place()` runs inside an open `get_session()`, and
  this codebase never holds a pooled connection across network I/O
  (CLAUDE.md, DB discipline). Callers verify first, then open their session.
  `blocks_delivery` is the in-session backstop and touches no network.
* **Once per posting.** Ten users receiving the same posting perform one check,
  not ten. Liveness is a property of the posting, so it is keyed and
  single-flighted by `(source, external_id)` exactly like `JobLiveness` itself.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Optional, Tuple

from app.config import settings
from app.db.models import JobLivenessState

log = logging.getLogger(__name__)

#: Sources whose `Job.url` is the canonical permalink for ONE posting, so a 404
#: there means that posting is gone. Aggregators are excluded on purpose: their
#: url is a redirect, a search link or an employer page, and a 404 on it says
#: nothing reliable about the vacancy. Those jobs fall back to whatever board
#: evidence exists and are otherwise delivered.
_VERIFIABLE_SOURCES = frozenset({
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters",
    "workable", "recruitee", "personio", "bamboohr", "breezy",
    "pinpoint", "rippling", "join", "teamtailor",
})

# ── aggregate metrics only ───────────────────────────────────────────────────
# Requirement from the brief: no job ids, no external ids, no URLs in labels.
# Keys are fixed strings and `by_source:<ats>`, which is bounded by the number
# of ATS families we support.
_METRICS: Counter = Counter()
_LATENCY_MS_TOTAL = "latency_ms_total"
_LATENCY_N = "latency_samples"
_metrics_lock = threading.Lock()

# ── single flight ────────────────────────────────────────────────────────────
# One in-flight check per posting. The scoring lane runs `scoring_workers` (20)
# concurrent workers across ALL users, which is exactly where ten simultaneous
# checks of one posting would otherwise come from. Across containers the
# JobLiveness row itself is the coordination point: once the first process
# writes checked_at, every other process sees fresh evidence and skips.
_inflight: dict = {}
_inflight_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    with _metrics_lock:
        _METRICS[key] += n


def metrics_snapshot(reset: bool = False) -> dict:
    """Aggregate counters since process start (or since the last reset)."""
    with _metrics_lock:
        data = dict(_METRICS)
        if reset:
            _METRICS.clear()
    n = data.get(_LATENCY_N, 0)
    total = data.get(_LATENCY_MS_TOTAL, 0)
    data["latency_ms_avg"] = round(total / n, 1) if n else 0.0
    return data


def _record_latency(ms: float) -> None:
    with _metrics_lock:
        _METRICS[_LATENCY_MS_TOTAL] += int(ms)
        _METRICS[_LATENCY_N] += 1


def blocks_delivery(session, source, external_id: str) -> Tuple[bool, str]:
    """In-session backstop: is this posting CONCLUSIVELY dead?

    Pure cached read — no network, safe to call with a session open. Returns
    (blocked, state). Anything other than REMOVED/EXPIRED returns False, which
    includes every inconclusive state and every posting we have never checked.
    """
    from sqlmodel import select
    from app.db.models import JobLiveness
    from app.discovery.liveness import is_dead

    src = source.value if hasattr(source, "value") else str(source)
    try:
        row = session.exec(
            select(JobLiveness.state).where(
                JobLiveness.source == src,
                JobLiveness.external_id == str(external_id),
            )
        ).first()
    except Exception as e:      # a liveness lookup must never break placement
        log.debug("blocks_delivery lookup failed for %s: %s", src, e)
        return False, JobLivenessState.UNKNOWN.value
    state = (row[0] if isinstance(row, tuple) else row) or JobLivenessState.UNKNOWN.value
    return is_dead(state), state


def _cached(source: str, external_id: str):
    from app.discovery.liveness import load_states
    got = load_states([(source, external_id)])
    return got.get((source, external_id), (None, None))


def _fetch(url: str, timeout: float) -> Tuple[Optional[int], str, str, Optional[str]]:
    """One bounded, SSRF-guarded request. Returns (status, final_url, body, error).

    SSRF-guarded for the same reason `app/discovery/verify.py` is: a job row can
    be created by `POST /api/jobs/submit`, so its URL is attacker-controllable,
    and this runs server-side. Redirects are followed BY HAND so every hop is
    re-checked — which is also what lets us see the final URL and detect a
    redirect to a careers index. A blocked host is an ERROR, never a removal:
    "we refused to fetch it" is not evidence the posting is gone.
    """
    import httpx
    from app.common.ssrf import guarded_request
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False,
                          headers={"User-Agent": settings.liveness_user_agent}) as client:
            r, err = guarded_request(client, "GET", url)
            if r is None:
                return None, "", "", err or "blocked"
            # Bounded read: removal wording is always near the top, and an
            # unbounded body on a delivery path is a memory risk in a container
            # that already holds torch, FAISS and Chromium (docs/MEMORY.md).
            ctype = (r.headers.get("content-type") or "").lower()
            body = r.text[:20000] if ctype.startswith("text") else ""
            return r.status_code, str(r.url), body, None
    except Exception as e:
        # Every transport failure is inconclusive by construction.
        return None, "", "", type(e).__name__


def verify_for_delivery(source, external_id: str, url: str) -> Tuple[str, str]:
    """Establish liveness for a posting about to be delivered.

    Returns (state, how) where `how` is one of: cached_dead, cached_fresh,
    unverifiable, checked, deduped, disabled. Never raises — an unverifiable
    posting is delivered, because refusing to deliver on a failed check would
    be exactly the "a refusal means death" mistake this design forbids.
    """
    from app.discovery import liveness as lv

    src = source.value if hasattr(source, "value") else str(source)
    ext = str(external_id)
    key = (src, ext)

    if not settings.liveness_gate_enabled:
        return JobLivenessState.UNKNOWN.value, "disabled"

    state, checked_at = _cached(src, ext)

    # Already known gone: no request, and the caller drops it.
    if lv.is_dead(state):
        _bump("avoided_check_already_dead")
        return state, "cached_dead"

    # Conclusive evidence young enough to trust.
    if not lv.needs_check(state, checked_at,
                          max_age_hours=settings.liveness_recheck_hours):
        _bump("avoided_check_fresh_evidence")
        return state or JobLivenessState.UNKNOWN.value, "cached_fresh"

    if src not in _VERIFIABLE_SOURCES or not url:
        # No permalink we can trust, so there is nothing safe to conclude.
        # Delivered, and counted so the gap is visible rather than silent.
        _bump("unverifiable_source")
        _bump(f"by_source:{src}:unverifiable")
        return state or JobLivenessState.UNKNOWN.value, "unverifiable"

    # ── single flight ────────────────────────────────────────────────────────
    with _inflight_lock:
        event = _inflight.get(key)
        leader = event is None
        if leader:
            event = threading.Event()
            _inflight[key] = event

    if not leader:
        # Someone else is already asking. Wait briefly, then read their answer.
        _bump("checks_deduplicated")
        event.wait(timeout=max(1.0, settings.liveness_check_timeout_seconds + 1.0))
        state, _ = _cached(src, ext)
        return state or JobLivenessState.UNKNOWN.value, "deduped"

    try:
        started = time.monotonic()
        _bump("checks_attempted")
        status, final_url, body, error = _fetch(
            url, float(settings.liveness_check_timeout_seconds))
        elapsed_ms = (time.monotonic() - started) * 1000.0
        _record_latency(elapsed_ms)
        new_state, reason = lv.classify(
            status, requested_url=url, final_url=final_url, body=body, error=error)
        lv.record(src, ext, new_state, reason=reason,
                  http_status=status, checked_url=url)
        _bump(f"state:{new_state}")
        _bump(f"by_source:{src}:{new_state}")
        return new_state, "checked"
    except Exception as e:
        # The gate itself failing is inconclusive, never fatal, never dead.
        log.warning("Liveness check errored for %s (delivering anyway): %s", src, e)
        _bump("check_errored")
        return JobLivenessState.UNKNOWN.value, "unverifiable"
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        event.set()


class CycleBudget:
    """A wall-clock allowance for checks inside one delivery cycle.

    Requirement: the gate must never stall the shortlist pipeline. A user with
    35 deliveries, each hitting the 6s timeout, would add 210s to a lane that
    runs every 90s. Once the budget is spent the lane stops making requests and
    decides on cached evidence alone — which still blocks anything already
    known dead, and still delivers everything else.
    """

    def __init__(self, seconds: Optional[float] = None):
        self.limit = float(settings.liveness_budget_seconds_per_cycle
                           if seconds is None else seconds)
        self.spent = 0.0

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.spent >= self.limit

    def charge(self, seconds: float) -> None:
        self.spent += max(0.0, seconds)


def verified_dead(source, external_id: str, url: str,
                  budget: "Optional[CycleBudget]" = None) -> bool:
    """Convenience for the lanes: True only when the posting is conclusively
    gone and must not be delivered.

    With a `budget`, a cycle that has already spent its allowance answers from
    cached evidence instead of making another request.
    """
    from app.discovery.liveness import is_dead

    if budget is not None and budget.exhausted:
        _bump("checks_skipped_cycle_budget")
        src = source.value if hasattr(source, "value") else str(source)
        state, _checked = _cached(src, str(external_id))
        dead = is_dead(state)
        if dead:
            _bump("blocked_before_delivery")
        return dead

    started = time.monotonic()
    state, how = verify_for_delivery(source, external_id, url)
    if budget is not None and how in ("checked", "deduped"):
        budget.charge(time.monotonic() - started)
    dead = is_dead(state)
    if dead:
        _bump("blocked_before_delivery")
    return dead
