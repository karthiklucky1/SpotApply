"""Stage metrics for contact research — so "we don't know" cannot read as zero.

Phase 8 asks for observed elapsed time and cost per stage, with successful,
empty, failed and timed-out runs reported SEPARATELY. The honest answer on
2026-09-25 was that the logs could not support it: `linkedin_xray.find_champions`
logged only on exception, `referral.generate_referral_drafts` logged nothing at
all, and no counter anywhere recorded how many searches ran, how many came back
empty, or how long any of it took. A report built on that would have been
inventing numbers.

So this module records them. It does not launch anything, call anything or widen
any permission — it is counters around code that already runs.

FOUR STAGES, and one of them does not exist:

    job_validation          liveness/delivery_gate — the posting is still live
    contact_search          linkedin_xray.find_champions — SerpAPI/Google X-Ray
    relevance_verification  NOT IMPLEMENTED. Nothing checks that a person found
                            still holds the role the snippet implied.
    output_generation       referral.generate_referral_drafts — the drafts

`NOT_IMPLEMENTED` is a first-class state precisely so the fourth line of a report
cannot read "0 runs, 0ms, 0 failures" — which looks like a stage that ran
perfectly. A stage that was never built reports `implemented: false`, and
`measured: false` says no run has been observed since this process started.

SIX OUTCOMES, never collapsed into "ok / not ok":

    ok            returned at least one candidate
    empty         ran, succeeded, found nothing — a real and common answer
    failed        the provider errored or returned a non-200
    timeout       the call exceeded its deadline
    quota         the provider refused on quota/rate limit (recoverable, not a bug)
    not_configured  no API key — the stage never ran, and is not a failure

PRIVACY. Aggregate counters only. Never a person's name, a profile URL, an
employer, a job id or a user id in a label — the same rule `liveness` follows,
for the same reason: these are real people who did not ask to be in our metrics.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, Optional

# ── stages ───────────────────────────────────────────────────────────────────

JOB_VALIDATION = "job_validation"
CONTACT_SEARCH = "contact_search"
RELEVANCE_VERIFICATION = "relevance_verification"
OUTPUT_GENERATION = "output_generation"

#: Every stage Phase 8 names, and whether the code for it exists at all.
#: `relevance_verification` is False and must stay False until something
#: actually verifies that a person found still holds the role implied.
STAGES: Dict[str, bool] = {
    JOB_VALIDATION: True,
    CONTACT_SEARCH: True,
    RELEVANCE_VERIFICATION: False,
    OUTPUT_GENERATION: True,
}

# ── outcomes ─────────────────────────────────────────────────────────────────

OK = "ok"
EMPTY = "empty"
FAILED = "failed"
TIMEOUT = "timeout"
QUOTA = "quota"
NOT_CONFIGURED = "not_configured"

OUTCOMES = (OK, EMPTY, FAILED, TIMEOUT, QUOTA, NOT_CONFIGURED)

#: `find_champions` reason string → outcome. Anything unmapped is FAILED, which
#: is the safe direction: an unknown reason is not evidence the call worked.
_REASON_OUTCOMES = {
    "serpapi_key_not_set": NOT_CONFIGURED,
    "no_company": NOT_CONFIGURED,
    "no_university": NOT_CONFIGURED,
    "serpapi_quota": QUOTA,
    "serpapi_invalid_key": NOT_CONFIGURED,
}


def outcome_for(reason: str) -> str:
    r = (reason or "").strip().lower()
    if not r:
        return FAILED
    if r in _REASON_OUTCOMES:
        return _REASON_OUTCOMES[r]
    if "timeout" in r or "timed out" in r or "readtimeout" in r:
        return TIMEOUT
    if "429" in r or "quota" in r or "rate limit" in r:
        return QUOTA
    return FAILED


# ── counters ─────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_counts: Dict[str, int] = defaultdict(int)          # "stage.outcome" -> n
_latency_ms: Dict[str, int] = defaultdict(int)      # "stage" -> summed ms
_latency_n: Dict[str, int] = defaultdict(int)
_latency_max: Dict[str, int] = defaultdict(int)
#: Billable third-party calls actually made, per stage. A call that never left
#: the process (no API key, no company) is not billable and is not counted here
#: — the same rule `analytics/spend.py` applies to LLM calls.
_provider_calls: Dict[str, int] = defaultdict(int)


def record(stage: str, outcome: str, *, elapsed_ms: Optional[float] = None,
           results: int = 0, provider_call: bool = False) -> None:
    """One observation. Never raises — metrics must not break the feature."""
    try:
        if stage not in STAGES:
            return
        if outcome not in OUTCOMES:
            outcome = FAILED
        with _lock:
            _counts[f"{stage}.{outcome}"] += 1
            if results:
                _counts[f"{stage}.results_total"] += int(results)
            if provider_call:
                _provider_calls[stage] += 1
            if elapsed_ms is not None:
                ms = int(max(elapsed_ms, 0))
                _latency_ms[stage] += ms
                _latency_n[stage] += 1
                if ms > _latency_max[stage]:
                    _latency_max[stage] = ms
    except Exception:                                # pragma: no cover
        pass


class timed:
    """Context manager that records elapsed time and an outcome.

    Usage keeps the outcome explicit — `t.outcome = EMPTY` — because the most
    common real result of a contact search is "ran fine, found nobody", and a
    wrapper that inferred success from "did not raise" would file that as OK.
    """

    def __init__(self, stage: str, *, provider_call: bool = False):
        self.stage = stage
        self.outcome = FAILED           # until the body says otherwise
        self.results = 0
        self.provider_call = provider_call
        self._t0 = 0.0

    def __enter__(self) -> "timed":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.outcome = TIMEOUT if "timeout" in str(exc_type.__name__).lower() else FAILED
        record(self.stage, self.outcome,
               elapsed_ms=(time.monotonic() - self._t0) * 1000.0,
               results=self.results, provider_call=self.provider_call)
        return False                     # never swallow the exception


def snapshot(reset: bool = False) -> dict:
    """Per-stage counters since process start (or the last reset).

    Every stage appears, including the one that does not exist, and each carries
    `implemented`, `observed_runs` and `measured` so a reader can tell FOUR
    things apart: a stage that was never built; one built but never exercised
    since the last deploy; one that was attempted but never actually called a
    provider (no API key — attempts, no timing); and one that ran and found
    nothing. Collapsing any of those into "0" is how a readiness report claims a
    capability the product does not have.
    """
    with _lock:
        counts = dict(_counts)
        lat_ms = dict(_latency_ms)
        lat_n = dict(_latency_n)
        lat_max = dict(_latency_max)
        calls = dict(_provider_calls)
        if reset:
            _counts.clear(); _latency_ms.clear(); _latency_n.clear()
            _latency_max.clear(); _provider_calls.clear()

    out: dict = {"stages": {}}
    for stage, implemented in STAGES.items():
        by_outcome = {o: counts.get(f"{stage}.{o}", 0) for o in OUTCOMES}
        runs = sum(by_outcome.values())
        n = lat_n.get(stage, 0)
        entry = {
            "implemented": implemented,
            # `measured` means WE HAVE TIMING, not "something was attempted":
            # 50 runs that all returned "no API key" made no call and took no
            # measurable time, and reading that as a measured stage is the error
            # Phase 8 names. `observed_runs` counts attempts either way.
            "measured": bool(n),
            "observed_runs": runs,
            "runs": runs,
            "by_outcome": by_outcome,
            "results_total": counts.get(f"{stage}.results_total", 0),
            "provider_calls": calls.get(stage, 0),
            # None, never 0.0: "we have no timing" and "it took no time" are
            # different answers and Phase 8 names the confusion explicitly.
            "elapsed_ms_avg": round(lat_ms.get(stage, 0) / n, 1) if n else None,
            "elapsed_ms_max": lat_max.get(stage) or None,
            "timed_runs": n,
        }
        if not implemented:
            entry["note"] = ("stage not implemented — nothing verifies that a "
                             "person found still holds the role implied")
        out["stages"][stage] = entry

    out["any_measurement"] = any(s["measured"] for s in out["stages"].values())
    out["any_observation"] = any(s["observed_runs"] for s in out["stages"].values())
    return out
