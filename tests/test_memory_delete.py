"""Tests for deleting a recruiter-memory entry: DELETE /api/profile/memory/{id}."""
from __future__ import annotations

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import UserPersonalMemory

_MARKER = "HirePath memory-delete test entry"


def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _cleanup():
    with get_session() as s:
        for row in s.exec(select(UserPersonalMemory)).all():
            if _MARKER in (row.raw_content or ""):
                s.delete(row)
        s.commit()


def _make_entry() -> int:
    with get_session() as s:
        row = UserPersonalMemory(
            user_id=None, source="linkedin",
            raw_content=_MARKER, recommendations="test brief",
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def test_delete_memory_entry():
    _cleanup()
    try:
        entry_id = _make_entry()
        r = _client().delete(f"/api/profile/memory/{entry_id}")
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True
        with get_session() as s:
            assert s.get(UserPersonalMemory, entry_id) is None
    finally:
        _cleanup()


def test_delete_missing_entry_returns_404():
    r = _client().delete("/api/profile/memory/999999999")
    assert r.status_code == 404
