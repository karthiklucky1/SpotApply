"""Container memory telemetry — the missing evidence when the platform OOM-kills us.

An OOM kill leaves no Python traceback: the kernel reaps the process and the
platform just reports "ran out of memory". Without a periodic reading of where
memory actually sat, every post-mortem is guesswork. This module reads the two
numbers that matter and costs nothing (a couple of procfs reads):

  * ``rss_mb()``      — this Python process only (torch + models + FAISS + lanes)
  * ``cgroup_*()``    — the WHOLE container, which is what the platform's limit
                        is enforced against

The gap between them is the point. Every headless Chromium we launch is a child
process: it does NOT show up in our RSS but it absolutely counts against the
container limit. A deploy that OOMs at "only 700 MB RSS" is usually 700 MB of
Python plus two 400 MB browsers.

Stdlib only — no psutil dependency for something procfs already answers.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_MB = 1024.0 * 1024.0

# cgroup v2 (what modern container platforms use) then v1 fallback.
_V2_MAX = "/sys/fs/cgroup/memory.max"
_V2_CURRENT = "/sys/fs/cgroup/memory.current"
_V1_MAX = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_V1_CURRENT = "/sys/fs/cgroup/memory/memory.usage_in_bytes"

# A cgroup with no limit reports "max" (v2) or a sentinel near 2^63 (v1). Treat
# anything above this as "unlimited" rather than reporting a petabyte ceiling.
_UNLIMITED_ABOVE_MB = 1024.0 * 1024.0  # 1 TB


def _read_int(path: str) -> int | None:
    try:
        with open(path, "r") as fh:
            raw = fh.read().strip()
    except (OSError, ValueError):
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def rss_mb() -> float | None:
    """Resident set size of THIS process in MB (None off Linux)."""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    # "VmRSS:\t  123456 kB"
                    return int(line.split()[1]) / 1024.0
    except (OSError, IndexError, ValueError):
        pass
    return None


def cgroup_limit_mb() -> float | None:
    """The container's memory ceiling in MB — the number the platform kills on."""
    for path in (_V2_MAX, _V1_MAX):
        val = _read_int(path)
        if val:
            mb = val / _MB
            if mb < _UNLIMITED_ABOVE_MB:
                return mb
    return None


def cgroup_usage_mb() -> float | None:
    """Whole-container memory usage in MB — includes Chromium children."""
    for path in (_V2_CURRENT, _V1_CURRENT):
        val = _read_int(path)
        if val is not None:
            return val / _MB
    return None


def snapshot() -> dict:
    """One reading of every number, plus the derived headroom percentage.

    ``used_pct`` is container usage against the container limit — that is the
    ratio that predicts an OOM kill, not process RSS against the limit.
    """
    rss = rss_mb()
    limit = cgroup_limit_mb()
    usage = cgroup_usage_mb()
    pct = None
    if limit and usage is not None and limit > 0:
        pct = round(usage / limit * 100.0, 1)
    return {
        "rss_mb": round(rss, 1) if rss is not None else None,
        "container_mb": round(usage, 1) if usage is not None else None,
        "limit_mb": round(limit, 1) if limit is not None else None,
        "used_pct": pct,
        # Non-Python memory: browsers, subprocesses, page cache charged to us.
        "non_python_mb": (round(usage - rss, 1)
                          if (usage is not None and rss is not None) else None),
    }


def format_snapshot(snap: dict | None = None) -> str:
    """Compact one-line rendering for logs."""
    s = snap or snapshot()
    parts = []
    if s.get("rss_mb") is not None:
        parts.append(f"rss={s['rss_mb']:.0f}MB")
    if s.get("container_mb") is not None:
        parts.append(f"container={s['container_mb']:.0f}MB")
    if s.get("limit_mb") is not None:
        parts.append(f"limit={s['limit_mb']:.0f}MB")
    if s.get("used_pct") is not None:
        parts.append(f"used={s['used_pct']:.0f}%")
    if s.get("non_python_mb") is not None:
        parts.append(f"non-python={s['non_python_mb']:.0f}MB")
    return " ".join(parts) or "memory stats unavailable"


def log_snapshot(label: str, warn_pct: float = 85.0,
                 logger: logging.Logger | None = None) -> dict:
    """Log one reading — WARNING once the container is within ``warn_pct`` of its
    limit, INFO otherwise. Returns the snapshot so callers can also act on it.

    Log the approach to the ceiling, not just the crash: a WARNING line at 88%
    thirty seconds before the kill is what turns "ran out of memory" into a
    diagnosis.
    """
    _log = logger or log
    snap = snapshot()
    line = format_snapshot(snap)
    pct = snap.get("used_pct")
    if pct is not None and pct >= warn_pct:
        _log.warning("MEMORY HIGH [%s] %s", label, line)
    else:
        _log.info("memory [%s] %s", label, line)
    return snap


def under_pressure(threshold_pct: float = 85.0) -> bool:
    """True when the container is close enough to its limit that starting
    another memory-heavy job (a browser, a from-scratch FAISS build) is likely
    to tip it over. False when the limit is unknown — never block work on a
    reading we could not take."""
    snap = snapshot()
    pct = snap.get("used_pct")
    return pct is not None and pct >= threshold_pct


def env_summary() -> dict:
    """Thread/allocator env that materially changes RSS, for the debug endpoint."""
    return {
        "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
        "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
        "TOKENIZERS_PARALLELISM": os.environ.get("TOKENIZERS_PARALLELISM"),
        "MALLOC_ARENA_MAX": os.environ.get("MALLOC_ARENA_MAX"),
        "WEB_CONCURRENCY": os.environ.get("WEB_CONCURRENCY"),
    }
