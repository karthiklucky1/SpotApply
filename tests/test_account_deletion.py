"""DELETE /api/account must actually delete the account (GDPR).

The route used to name 7 tables by hand while the schema had 18 with a `user_id`
column, because tables kept being added after the route was written — user_card
and card_match_shadow (CardRace) were only the most recent. Rows for a "deleted"
user therefore survived in llm_spend, user_notifications, userpersonalmemory,
user_review, trusthistory, trialgrant, coupon_redemption, user_referral_reward,
recruiterprofile, and both card tables.

These tests are schema-driven for the same reason the route now is: a new
user-scoped table fails them until it has a deletion story, so the route cannot
fall behind again.
"""
from __future__ import annotations

import pytest
from sqlmodel import SQLModel, select

from app.api import server
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job

_GONE = "deleted-tenant"
_KEEP = "surviving-tenant"

# Tables whose owner column is not `user_id`; the route handles them explicitly.
_EXTRA = server._EXTRA_OWNER_COLUMNS


def _user_scoped_tables() -> dict[str, tuple[str, ...]]:
    """Every table that stores rows belonging to one user, from the schema."""
    out: dict[str, tuple[str, ...]] = {}
    for name, table in SQLModel.metadata.tables.items():
        cols = tuple(c for c in ("user_id",) if c in table.columns)
        cols += tuple(c for c in _EXTRA.get(name, ()) if c in table.columns)
        if cols:
            out[name] = cols
    return out


def _placeholder(col, uid: str, tag: str):
    """A type-appropriate value for a NOT NULL column with no default."""
    import datetime as _dt
    import sqlalchemy as sa
    t = col.type
    if isinstance(t, sa.Boolean):
        return False
    if isinstance(t, (sa.Integer, sa.BigInteger, sa.SmallInteger)):
        return 0
    if isinstance(t, (sa.Float, sa.Numeric)):
        return 0.0
    if isinstance(t, sa.DateTime):
        return _dt.datetime(2026, 7, 30, 12, 0, 0)
    if isinstance(t, sa.Date):
        return _dt.date(2026, 7, 30)
    if isinstance(t, sa.JSON):
        return {}
    # Enum columns must take one of their own values.
    enums = getattr(t, "enums", None)
    if enums:
        return enums[0]
    return f"{uid}-{tag}-{col.name}"


def _seed_row(session, table, uid: str, tag: str, owner_cols, **overrides) -> dict:
    """INSERT one row into `table` owned by uid, filling required columns generically.

    Built from the schema rather than hand-written per model, so adding a NOT NULL
    column to any user-scoped table does not silently break this test's coverage
    of that table (which is the very failure mode the test exists to catch).
    """
    values: dict = {}
    for col in table.columns:
        if col.name in overrides:
            values[col.name] = overrides[col.name]
        elif col.name in owner_cols:
            values[col.name] = uid
        elif col.primary_key or col.nullable or col.default is not None \
                or col.server_default is not None:
            continue
        else:
            values[col.name] = _placeholder(col, uid, tag)
    session.exec(table.insert().values(**values))
    return values


def _seed_one(session, user_id: str, tag: str = "1") -> int:
    """One row owned by user_id in EVERY user-scoped table, plus an Application."""
    job = Job(user_id=user_id, source="greenhouse", external_id=f"{user_id}-{tag}",
              title="Engineer", company="DelCo", url=f"https://x/{user_id}/{tag}",
              description="desc")
    session.add(job)
    session.commit()
    session.refresh(job)
    application = Application(user_id=user_id, job_id=job.id,
                              status=ApplicationStatus.SHORTLISTED)
    session.add(application)
    session.commit()
    session.refresh(application)

    for name, cols in _user_scoped_tables().items():
        if name in ("job", "application"):
            continue          # already seeded above, with real FKs
        table = SQLModel.metadata.tables[name]
        extra = {}
        if "job_id" in table.columns:
            extra["job_id"] = job.id
        if "application_id" in table.columns:
            extra["application_id"] = application.id
        _seed_row(session, table, user_id, tag, set(cols), **extra)
    session.commit()
    return application.id


def _rows_for(session, table, cols, user_id: str) -> int:
    total = 0
    for col in cols:
        total += len(session.exec(
            select(table.c.id if "id" in table.columns else table)
            .where(table.columns[col] == user_id)).all())
    return total


@pytest.fixture
def deleted_via_route(monkeypatch):
    with get_session() as s:
        for uid in (_GONE, _KEEP):
            for name, cols in _user_scoped_tables().items():
                table = SQLModel.metadata.tables[name]
                for col in cols:
                    s.exec(table.delete().where(table.columns[col] == uid))
        s.commit()
        _seed_one(s, _GONE)
        _seed_one(s, _KEEP)

    monkeypatch.setattr(server, "_require_user", lambda request: _GONE)
    result = server.delete_account(request=None)
    assert result["success"] is True
    yield


def test_every_seeded_table_is_emptied_for_the_deleted_user(deleted_via_route):
    """This is the assertion the hand-written list failed: not "some tables", all."""
    leftovers = {}
    with get_session() as s:
        for name, cols in _user_scoped_tables().items():
            table = SQLModel.metadata.tables[name]
            n = _rows_for(s, table, cols, _GONE)
            if n:
                leftovers[name] = n
    assert not leftovers, (
        f"rows survived account deletion for {_GONE}: {leftovers}. Every table "
        f"with a user_id column must be covered — the route enumerates the schema, "
        f"so a leftover means a table needs an entry in _EXTRA_OWNER_COLUMNS or a "
        f"deliberate exemption."
    )


def test_another_tenants_rows_are_untouched(deleted_via_route):
    """A schema-wide DELETE is only safe if it is still scoped."""
    with get_session() as s:
        for name, cols in _user_scoped_tables().items():
            table = SQLModel.metadata.tables[name]
            assert _rows_for(s, table, cols, _KEEP) > 0, (
                f"{name}: the surviving tenant's row was deleted too — a "
                f"schema-wide DELETE is only safe while it stays scoped")


def test_pending_questions_are_deleted_via_their_application(monkeypatch):
    """PendingQuestion has no user_id — it hangs off the application."""
    from app.db.models import PendingQuestion
    with get_session() as s:
        for name, cols in _user_scoped_tables().items():
            table = SQLModel.metadata.tables[name]
            for col in cols:
                s.exec(table.delete().where(table.columns[col] == _GONE))
        s.commit()
        aid = _seed_one(s, _GONE, tag="pq")
        s.add(PendingQuestion(application_id=aid, field_label="visa?",
                              field_selector="#v", field_type="text"))
        s.commit()
    monkeypatch.setattr(server, "_require_user", lambda request: _GONE)
    server.delete_account(request=None)
    with get_session() as s:
        assert not s.exec(select(PendingQuestion).where(
            PendingQuestion.application_id == aid)).all()


def test_deleting_a_system_account_is_refused(monkeypatch):
    """SHARED_POOL_USER owns the pool every tenant is served from. A schema-wide
    delete keyed on it would wipe the corpus for everyone."""
    from fastapi import HTTPException

    from app.discovery.pipeline import SHARED_POOL_USER
    monkeypatch.setattr(server, "_require_user", lambda request: SHARED_POOL_USER)
    with pytest.raises(HTTPException) as exc:
        server.delete_account(request=None)
    assert exc.value.status_code == 400


def test_the_schema_enumeration_is_not_empty():
    """Guard the guard — an import problem must not make the sweep vacuous."""
    tables = _user_scoped_tables()
    assert len(tables) >= 15, f"only found {len(tables)} user-scoped tables: {sorted(tables)}"
    for expected in ("job", "application", "user_card", "card_match_shadow",
                     "llm_spend", "user_notifications", "userpersonalmemory"):
        assert expected in tables, f"{expected} missing from the enumeration"


# ── Supabase storage + auth cleanup ──────────────────────────────────────────
# These two used to share one `except Exception: pass`, so a storage failure
# skipped the AUTH deletion silently and the route still answered success — the
# rows were gone but the login still worked, which reads as "it didn't delete".

class _FakeStorageBucket:
    def __init__(self, fail=False):
        self.fail = fail
        self.removed = None

    def list(self, uid):
        if self.fail:
            raise RuntimeError("storage listing exploded")
        return [{"name": "resume.pdf"}]

    def remove(self, paths):
        self.removed = paths


class _FakeAdmin:
    def __init__(self, fail=False):
        self.fail = fail
        self.deleted = None

    def delete_user(self, uid):
        if self.fail:
            raise RuntimeError("auth delete refused")
        self.deleted = uid


class _FakeSupabase:
    def __init__(self, storage_fail=False, auth_fail=False):
        self._bucket = _FakeStorageBucket(storage_fail)
        self.auth = type("A", (), {"admin": _FakeAdmin(auth_fail)})()

    def storage(self):  # pragma: no cover - attribute style below
        raise NotImplementedError

    def from_(self, _name):
        return self._bucket


def _run_delete_with(monkeypatch, storage_fail=False, auth_fail=False):
    fake = _FakeSupabase(storage_fail, auth_fail)
    fake.storage = type("S", (), {"from_": staticmethod(lambda n: fake._bucket)})()
    monkeypatch.setattr(server, "_require_user", lambda request: _GONE)
    # use_supabase is a computed property, so it has to be patched on the CLASS.
    from app.config import Settings, settings as _settings
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "service_client", lambda: fake)
    assert _settings.use_supabase is True
    return server.delete_account(request=None), fake


def test_auth_user_is_deleted_even_when_storage_cleanup_fails(monkeypatch):
    """The bug: a storage error skipped the auth delete, so the login survived."""
    result, fake = _run_delete_with(monkeypatch, storage_fail=True)
    assert fake.auth.admin.deleted == _GONE, (
        "the Supabase Auth user MUST still be deleted when résumé storage "
        "cleanup fails — otherwise the user can still sign in"
    )
    assert result["storage_deleted"] is False
    assert result["auth_deleted"] is True
    assert result["success"] is True


def test_a_failed_auth_delete_is_reported_not_swallowed(monkeypatch):
    """Data gone but sign-in alive is a PARTIAL deletion — never 'success'."""
    result, _ = _run_delete_with(monkeypatch, auth_fail=True)
    assert result["success"] is False
    assert result["partial"] is True
    assert result["auth_deleted"] is False
    assert "support@spotapply.ai" in result["message"]


def test_happy_path_removes_storage_and_auth(monkeypatch):
    result, fake = _run_delete_with(monkeypatch)
    assert fake._bucket.removed == [f"{_GONE}/resume.pdf"]
    assert fake.auth.admin.deleted == _GONE
    assert result["success"] is True and result["auth_deleted"] is True
