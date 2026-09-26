"""A database that stopped accepting writes must not fail silently.

2026-09-26 02:26–~04:00 UTC: a session-level read-only SET leaked through the
transaction pooler onto a pooled backend, ~2,200 app writes failed, and every
caller only logged a WARNING. `app/common/db_health.py` counts the class.
"""
from __future__ import annotations

import pytest

from app.common import db_health


class _PgErr(Exception):
    pgcode = "25006"


class _Wrapped(Exception):
    def __init__(self, orig):
        super().__init__(str(orig))
        self.orig = orig


@pytest.fixture(autouse=True)
def _reset():
    db_health.reset_state()
    yield
    db_health.reset_state()


def test_the_read_only_class_is_recognised_by_sqlstate():
    assert db_health.is_read_only_error(_Wrapped(_PgErr("cannot execute UPDATE")))
    assert not db_health.is_read_only_error(_Wrapped(ValueError("x")))


def test_counting_logs_critical_once_per_window(caplog):
    with caplog.at_level("CRITICAL", logger="app.common.db_health"):
        for i in range(5):
            db_health.record(_Wrapped(_PgErr("ro")), now=1000.0 + i)
    snap = db_health.snapshot()
    assert snap["read_only_errors"] == 5
    assert sum("REJECTING WRITES" in r.message for r in caplog.records) == 1


def test_other_errors_are_not_counted():
    assert db_health.record(_Wrapped(RuntimeError("timeout"))) is False
    assert db_health.snapshot()["read_only_errors"] == 0


def test_the_hook_is_installed_on_the_app_engine():
    from app.db.init_db import engine
    assert getattr(engine, "_spotapply_db_health", False)


def test_the_hook_sees_a_real_driver_error():
    """End to end on SQLite: a failing statement flows through handle_error."""
    from sqlalchemy import create_engine, text
    eng = create_engine("sqlite://")
    db_health.install(eng)
    seen = []
    orig = db_health.record
    db_health.record = lambda exc, now=None: seen.append(exc) or False
    try:
        with pytest.raises(Exception):
            with eng.connect() as c:
                c.execute(text("SELECT * FROM no_such_table"))
    finally:
        db_health.record = orig
    assert seen, "the engine hook must see driver errors"
