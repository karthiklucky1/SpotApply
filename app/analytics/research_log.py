"""Persisted contact-research observations (audit 2026-09-25, finding 12).

`app/intelligence/contact_research.py` keeps in-process counters that reset on
every deploy, so no elapsed time, call count or empty-result rate survived long
enough to price the feature. Each observation is also written here as one
FunnelEvent(stage="contact_research") with aggregate labels ONLY — never a
person's name, a profile URL, a company, a job id or a user id. Writes happen
on a daemon thread so the feature never waits on the database.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

PERSIST_STAGE = "contact_research"


def persist(stage: str, outcome: str, elapsed_ms: Optional[float], results: int,
            provider_call: bool, *, ok: bool) -> None:
    try:
        from app.analytics.funnel import FunnelTracker
        FunnelTracker.record(None, PERSIST_STAGE, ok, reason=f"{stage}.{outcome}",
                             metadata={"elapsed_ms": None if elapsed_ms is None
                                       else int(max(elapsed_ms, 0)),
                                       "results": int(results or 0),
                                       "provider_call": bool(provider_call),
                                       # Relevance verification does not exist: a
                                       # result is NEVER a verified recruiter.
                                       "verified": False})
    except Exception:
        pass


def persist_async(*args, **kwargs) -> None:
    from app.config import settings
    if getattr(settings, "research_log_sync", False):     # tests
        persist(*args, **kwargs)
        return
    threading.Thread(target=persist, args=args, kwargs=kwargs, daemon=True,
                     name="research-log").start()


def persisted_summary(days: int = 30) -> dict:
    """Stage/outcome counts, provider calls and latency percentiles from the
    persisted rows — survives deploys, unlike `contact_research.snapshot()`."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import FunnelEvent
    since = datetime.utcnow() - timedelta(days=days)
    out: dict = {}
    with get_session() as s:
        rows = s.exec(select(FunnelEvent.reason, FunnelEvent.metadata_json)
                      .where(FunnelEvent.stage == PERSIST_STAGE,
                             FunnelEvent.created_at >= since).limit(100_000)).all()
    for reason, mj in rows:
        stage, _, outcome = (reason or "").partition(".")
        e = out.setdefault(stage, {"by_outcome": defaultdict(int), "provider_calls": 0,
                                   "_ms": [], "results_total": 0})
        e["by_outcome"][outcome] += 1
        try:
            m = json.loads(mj or "{}")
        except Exception:
            m = {}
        e["provider_calls"] += 1 if m.get("provider_call") else 0
        e["results_total"] += int(m.get("results") or 0)
        if isinstance(m.get("elapsed_ms"), int):
            e["_ms"].append(m["elapsed_ms"])
    for e in out.values():
        ms = sorted(e.pop("_ms"))
        e["by_outcome"] = dict(e["by_outcome"])
        e["latency_ms"] = ({"n": len(ms), "p50": ms[len(ms) // 2],
                            "p95": ms[min(len(ms) - 1, int(len(ms) * 0.95))]} if ms else None)
        e["verified_contacts"] = 0      # verification is not implemented
    return {"window_days": days, "stages": out,
            "note": "Results are unverified search hits, not verified recruiters."}
