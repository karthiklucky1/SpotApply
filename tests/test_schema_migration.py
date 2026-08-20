"""A model field must reach the live database on its own.

`create_all()` only creates missing TABLES — it never alters one that already
exists, so every new field needs an ALTER. That used to be two hand-written
lists (init_db's per-table migrations and server's _USERPROFILE_COLUMNS), and
they drifted: `target_roles_auto` shipped in neither, so production answered
`UndefinedColumn: column userprofile.target_roles_auto does not exist` every
~40 seconds and the scoring and pulse lanes crash-looped — they read
UserProfile and never touch the API route that lazily repaired the schema.

These tests are schema-driven for the same reason the migration now is.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, select

from app.db.init_db import engine, ensure_model_columns, get_session
from app.db.models import UserProfile


def _live_columns(table: str) -> set[str]:
    return {c["name"].lower() for c in inspect(engine).get_columns(table)}


def test_every_model_column_exists_on_the_live_schema():
    """The whole class of bug, in one assertion."""
    ensure_model_columns()
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    missing = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in tables:
            continue
        live = {c["name"].lower() for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name.lower() not in live:
                missing.append(f"{table.name}.{col.name}")
    assert not missing, (
        f"declared on the model but absent from the database: {missing} — "
        f"every read of that table raises UndefinedColumn in production")


def test_a_dropped_column_is_restored(monkeypatch):
    """Reproduces the outage: drop the column, confirm reads fail, migrate, confirm they work."""
    if engine.dialect.name != "sqlite":
        pytest.skip("rehearsal drops a column; SQLite-only")

    with engine.begin() as c:
        c.execute(text("ALTER TABLE userprofile DROP COLUMN target_roles_auto"))
    assert "target_roles_auto" not in _live_columns("userprofile")

    with pytest.raises(Exception):
        with get_session() as s:
            s.exec(select(UserProfile)).first()

    assert ensure_model_columns() >= 1
    assert "target_roles_auto" in _live_columns("userprofile")

    # The read path works again, and the model default came with it.
    with get_session() as s:
        s.add(UserProfile(user_id="migration-probe"))
        s.commit()
        row = s.exec(select(UserProfile).where(
            UserProfile.user_id == "migration-probe")).first()
        assert row.target_roles_auto is True
        s.delete(row)
        s.commit()


def test_the_migration_is_idempotent():
    """It runs on EVERY boot — a second pass must be a no-op, not an error."""
    ensure_model_columns()
    assert ensure_model_columns() == 0


def test_defaults_are_rendered_for_each_scalar_type():
    """Existing rows get a value, so the DEFAULT clause has to be right."""
    from app.db.init_db import _ddl_default

    class _Col:
        def __init__(self, arg):
            self.default = type("D", (), {"arg": arg})()

    assert _ddl_default(_Col(True)) == " DEFAULT TRUE"
    assert _ddl_default(_Col(False)) == " DEFAULT FALSE"
    assert _ddl_default(_Col(0)) == " DEFAULT 0"
    assert _ddl_default(_Col("full_time")) == " DEFAULT 'full_time'"
    assert _ddl_default(_Col("it's")) == " DEFAULT 'it''s'", "quotes must be escaped"
    # default_factory (utcnow) is the ORM's job, not the DDL's.
    assert _ddl_default(_Col(lambda: 1)) == ""
    assert _ddl_default(type("C", (), {"default": None})()) == ""
