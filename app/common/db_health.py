"""Make a database that silently stopped accepting writes impossible to miss.

INCIDENT 2026-09-26 02:26–~04:00 UTC. A session-level
`SET default_transaction_read_only = on` (issued by a read-only diagnostic
session) stayed on a pooled Postgres backend behind Supavisor's TRANSACTION
pooler. Supavisor hands server backends to whichever client transaction comes
next, so the setting leaked into the application: every app transaction that
landed on that backend failed with `ReadOnlySqlTransaction` — board polls, job
inserts, `slate.place()`'s row lock, the scoring sweep. About 2,200 failed
writes over ~93 minutes, and nothing surfaced it: each caller logged a WARNING
and carried on, the lanes reported themselves alive, and reads (which is all
the dashboard does) kept working.

This module counts that one error class at the engine level (a SQLAlchemy
`handle_error` hook — no query, no round trip, no behaviour change), logs a
CRITICAL line with the remediation at most every few minutes, and exposes the
counters to `/api/admin/health`.

REMEDIATION, recorded where the next person will look:
  * find the backend(s):  postgres_logs "read-only transaction" grouped by
    process_id (Supabase log explorer) — the error names no pid in the app;
  * `SELECT pg_terminate_backend(<pid>)` for the idle pooled backend(s)
    (`application_name = 'Supavisor'`); Supavisor opens a clean replacement;
  * diagnostic sessions must use `SET LOCAL` inside a transaction, or connect
    as `supabase_read_only_user` — never a session-level SET through the
    transaction pooler (port 6543).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state = {"read_only_errors": 0, "first_at": None, "last_at": None, "last_logged": 0.0}
_LOG_EVERY_S = 300.0


def is_read_only_error(exc: BaseException) -> bool:
    """True for Postgres SQLSTATE 25006 (read_only_sql_transaction)."""
    orig = getattr(exc, "orig", exc)
    code = getattr(orig, "pgcode", None)
    if code == "25006":
        return True
    return type(orig).__name__ == "ReadOnlySqlTransaction"


def record(exc: BaseException, now: Optional[float] = None) -> bool:
    """Count one error if it is the read-only class. Returns True when counted."""
    if not is_read_only_error(exc):
        return False
    now = time.time() if now is None else now
    with _lock:
        _state["read_only_errors"] += 1
        _state["first_at"] = _state["first_at"] or now
        _state["last_at"] = now
        due = now - _state["last_logged"] >= _LOG_EVERY_S
        if due:
            _state["last_logged"] = now
        n = _state["read_only_errors"]
    if due:
        log.critical(
            "DATABASE REJECTING WRITES: %d ReadOnlySqlTransaction error(s) since %s. "
            "A pooled backend is in read-only mode (a leaked session-level SET) or "
            "the project is in disk-full read-only mode. Find the backend pid in "
            "postgres logs and pg_terminate_backend() it; see app/common/db_health.py.",
            n, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_state["first_at"])))
    return True


def snapshot() -> dict:
    with _lock:
        s = dict(_state)
    fmt = (lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None)
    return {"read_only_errors": s["read_only_errors"], "first_at": fmt(s["first_at"]),
            "last_at": fmt(s["last_at"]),
            "writes_failing_now": bool(s["last_at"] and time.time() - s["last_at"] < 120)}


def install(engine) -> None:
    """Attach the counter to an engine. Idempotent; never raises."""
    try:
        from sqlalchemy import event
        if getattr(engine, "_spotapply_db_health", False):
            return

        @event.listens_for(engine, "handle_error")
        def _on_error(ctx):                     # noqa: ANN001
            try:
                record(ctx.original_exception)
            except Exception:                   # the hook must never mask the error
                pass

        engine._spotapply_db_health = True
    except Exception as e:                      # pragma: no cover
        log.debug("db_health hook not installed: %s", e)


def reset_state() -> None:
    """Tests only."""
    with _lock:
        _state.update({"read_only_errors": 0, "first_at": None, "last_at": None,
                       "last_logged": 0.0})
