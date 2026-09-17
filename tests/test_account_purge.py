"""app/common/account_purge.py — remove a tenant, and find the tenants nobody removed.

Audit 2026-09-16: one Supabase Auth user no longer existed, yet its userprofile
(with professional-summary text), 2,860 applications, 33,192 job copies, 13
storage objects, 25 usage rows and a coupon subscription row were all still in
our tables. Nothing compared auth against our rows, and the deletion route only
cleaned the "resume" bucket while "avatars" exists too.

The reconcile is built to do NOTHING when in doubt, and that is what most of
these tests pin: a listing that raised, came back empty, did not paginate to the
end, or implies more orphans than live accounts must delete nothing at all.

Rows this file writes carry the `purge-` prefix and are removed by it. The fake
auth listing always includes every OTHER tenant already in the shared test
database, so the only orphans the reconcile can ever see are this file's own.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.common import account_purge as ap
from app.config import Settings, settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile, UserSubscription

_P = "purge-"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            for a in s.exec(select(Application).where(Application.user_id.like(f"{_P}%"))).all():
                s.delete(a)
            for j in s.exec(select(Job).where(Job.user_id.like(f"{_P}%"))).all():
                s.delete(j)
            s.exec(delete(UserSubscription).where(UserSubscription.user_id.like(f"{_P}%")))
            s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
            s.commit()
    _wipe()
    yield
    _wipe()


def _seed_tenant(uid: str, jobs: int = 2) -> None:
    with get_session() as s:
        s.add(UserProfile(user_id=uid, professional_summary="private text"))
        for i in range(jobs):
            j = Job(source=JobSource.REMOTEOK, external_id=f"{uid}-j{i}", company="PurgeCo",
                    title="Role", url="http://p", description="d", user_id=uid,
                    first_seen=datetime.utcnow(), discovered_at=datetime.utcnow())
            s.add(j)
            s.flush()
            s.add(Application(job_id=j.id, user_id=uid, status=ApplicationStatus.SHORTLISTED,
                              apply_track="manual"))
        s.commit()


def _rows_for(uid: str) -> dict:
    with get_session() as s:
        return {
            "profiles": len(s.exec(select(UserProfile).where(UserProfile.user_id == uid)).all()),
            "jobs": len(s.exec(select(Job).where(Job.user_id == uid)).all()),
            "apps": len(s.exec(select(Application).where(Application.user_id == uid)).all()),
        }


class _Bucket:
    def __init__(self, name, log):
        self.name, self.log = name, log

    def list(self, prefix):
        return [{"name": "resume.pdf"}, {"name": "photo.png"}]

    def remove(self, paths):
        self.log.append((self.name, tuple(paths)))


class _FakeSupabase:
    """auth.admin.list_users pages like supabase-py; storage.from_(bucket)."""

    def __init__(self, auth_ids, *, raise_on_page=None, per_page_short=True):
        self.auth_ids = list(auth_ids)
        self.raise_on_page = raise_on_page
        self.per_page_short = per_page_short
        self.removed: list = []
        fake = self

        class _Admin:
            def list_users(self, page=1, per_page=1000):
                if fake.raise_on_page == page:
                    raise RuntimeError("auth down")
                if not fake.per_page_short:
                    # Every page, whatever its number, comes back exactly full
                    # — pagination can never complete.
                    return ([SimpleNamespace(id=u) for u in fake.auth_ids] * per_page)[:per_page]
                start = (page - 1) * per_page
                chunk = fake.auth_ids[start:start + per_page]
                return [SimpleNamespace(id=u) for u in chunk]

        self.auth = SimpleNamespace(admin=_Admin())
        self.storage = SimpleNamespace(from_=lambda name: _Bucket(name, fake.removed))


def _wire(monkeypatch, fake):
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    monkeypatch.setattr(settings, "orphan_purge_enabled", True, raising=False)
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "service_client", lambda: fake)


def _others() -> set:
    """Every tenant id already in the shared test DB that is not ours."""
    return {u for u in ap._tenant_ids() if not u.startswith(_P)}


# ── the one deletion ─────────────────────────────────────────────────────────

def test_purge_user_data_removes_every_row_the_tenant_owns_and_nobody_elses():
    _seed_tenant(f"{_P}gone", jobs=3)
    _seed_tenant(f"{_P}stays", jobs=1)
    counts = ap.purge_user_data(f"{_P}gone")
    assert counts.get("job") == 3 and counts.get("application") == 3
    assert counts.get("userprofile") == 1
    assert _rows_for(f"{_P}gone") == {"profiles": 0, "jobs": 0, "apps": 0}
    assert _rows_for(f"{_P}stays") == {"profiles": 1, "jobs": 1, "apps": 1}


@pytest.mark.parametrize("uid", [None, "", "local", "shared", "  "])
def test_sentinel_identities_are_refused(uid):
    with pytest.raises(ValueError):
        ap.purge_user_data(uid)


def test_the_shared_pool_owner_is_a_sentinel():
    from app.discovery.pipeline import SHARED_POOL_USER
    assert ap.is_sentinel(SHARED_POOL_USER)
    with pytest.raises(ValueError):
        ap.purge_user_data(SHARED_POOL_USER)


def test_storage_cleanup_covers_both_buckets_independently():
    fake = _FakeSupabase([])
    out = ap.purge_user_storage(f"{_P}x", fake)
    assert out == {"resume": True, "avatars": True}
    assert {b for b, _ in fake.removed} == {"resume", "avatars"}
    for _bucket, paths in fake.removed:
        assert all(p.startswith(f"{_P}x/") for p in paths)


# ── the reconcile: doing nothing when in doubt ───────────────────────────────

def test_a_listing_that_raises_deletes_nothing(monkeypatch):
    _seed_tenant(f"{_P}gone")
    _wire(monkeypatch, _FakeSupabase(sorted(_others()), raise_on_page=1))
    out = ap.purge_orphaned_accounts()
    assert out["aborted"] and out["purged"] == []
    assert _rows_for(f"{_P}gone")["profiles"] == 1


def test_an_empty_listing_never_means_delete_everyone(monkeypatch):
    _seed_tenant(f"{_P}gone")
    _wire(monkeypatch, _FakeSupabase([]))
    out = ap.purge_orphaned_accounts()
    assert out["aborted"] == "auth listing returned zero users"
    assert _rows_for(f"{_P}gone")["profiles"] == 1


def test_a_listing_that_never_completes_deletes_nothing(monkeypatch):
    _seed_tenant(f"{_P}gone")
    fake = _FakeSupabase(sorted(_others()) + [f"{_P}live"], per_page_short=False)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(ap, "_MAX_LIST_PAGES", 5)     # the loop guard, sooner
    out = ap.purge_orphaned_accounts()
    assert out["aborted"] and "did not complete" in out["aborted"]
    assert _rows_for(f"{_P}gone")["profiles"] == 1


def test_more_orphans_than_live_accounts_is_a_truncated_listing_not_a_purge(monkeypatch):
    for i in range(3):
        _seed_tenant(f"{_P}gone{i}", jobs=1)
    # Only OUR one live account in auth, none of the pre-existing tenants: the
    # reconcile must read that as a broken listing and refuse.
    _seed_tenant(f"{_P}live", jobs=0)
    _wire(monkeypatch, _FakeSupabase([f"{_P}live"]))
    out = ap.purge_orphaned_accounts()
    assert out["aborted"] and "exceed" in out["aborted"]
    for i in range(3):
        assert _rows_for(f"{_P}gone{i}")["profiles"] == 1


def test_disabled_or_not_supabase_does_nothing(monkeypatch):
    _seed_tenant(f"{_P}gone")
    monkeypatch.setattr(settings, "orphan_purge_enabled", False, raising=False)
    assert ap.purge_orphaned_accounts()["aborted"] == "disabled"
    monkeypatch.setattr(settings, "orphan_purge_enabled", True, raising=False)
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: False))
    assert ap.purge_orphaned_accounts()["aborted"] == "not a Supabase deployment"
    assert _rows_for(f"{_P}gone")["profiles"] == 1


# ── the reconcile: doing the job when it can ────────────────────────────────

def test_a_tenant_whose_auth_user_is_gone_is_purged_and_live_tenants_are_not(monkeypatch):
    _seed_tenant(f"{_P}gone", jobs=2)
    _seed_tenant(f"{_P}live1", jobs=1)
    _seed_tenant(f"{_P}live2", jobs=1)
    fake = _FakeSupabase(sorted(_others()) + [f"{_P}live1", f"{_P}live2"])
    _wire(monkeypatch, fake)
    out = ap.purge_orphaned_accounts()
    assert out["aborted"] is None
    assert out["candidates"] == 1 and len(out["purged"]) == 1
    assert out["purged"][0]["rows"] >= 5            # profile + 2 jobs + 2 apps
    assert out["purged"][0]["storage"] == {"resume": True, "avatars": True}
    assert _rows_for(f"{_P}gone") == {"profiles": 0, "jobs": 0, "apps": 0}
    assert _rows_for(f"{_P}live1")["profiles"] == 1 and _rows_for(f"{_P}live2")["profiles"] == 1
    # Counts only in the result — never an id or an email.
    assert f"{_P}gone" not in repr(out)


def test_auth_ids_are_matched_case_insensitively(monkeypatch):
    _seed_tenant(f"{_P}Mixed", jobs=1)
    fake = _FakeSupabase(sorted(_others()) + [f"{_P}mixed"])
    _wire(monkeypatch, fake)
    out = ap.purge_orphaned_accounts()
    assert out["aborted"] is None and out["candidates"] == 0
    assert _rows_for(f"{_P}Mixed")["profiles"] == 1


def test_the_per_run_bound_holds(monkeypatch):
    for i in range(3):
        _seed_tenant(f"{_P}gone{i}", jobs=1)
    for i in range(4):
        _seed_tenant(f"{_P}live{i}", jobs=0)
    fake = _FakeSupabase(sorted(_others()) + [f"{_P}live{i}" for i in range(4)])
    _wire(monkeypatch, fake)
    out = ap.purge_orphaned_accounts(max_users=1)
    assert out["candidates"] == 3 and len(out["purged"]) == 1
    remaining = sum(_rows_for(f"{_P}gone{i}")["profiles"] for i in range(3))
    assert remaining == 2


def test_a_subscription_row_with_no_profile_is_still_a_tenant_to_reconcile(monkeypatch):
    with get_session() as s:
        s.add(UserSubscription(user_id=f"{_P}subonly"))
        s.commit()
    _seed_tenant(f"{_P}live", jobs=0)
    fake = _FakeSupabase(sorted(_others()) + [f"{_P}live"])
    _wire(monkeypatch, fake)
    out = ap.purge_orphaned_accounts()
    assert out["aborted"] is None and out["candidates"] == 1
    with get_session() as s:
        assert s.exec(select(UserSubscription).where(
            UserSubscription.user_id == f"{_P}subonly")).first() is None


def test_the_listing_shape_reader_accepts_every_shape_supabase_py_has_used():
    users = [SimpleNamespace(id="a")]
    assert ap._users_from_listing(users) == users
    assert ap._users_from_listing(SimpleNamespace(users=users)) == users
    assert ap._users_from_listing({"users": users}) == users
    assert ap._users_from_listing(None) is None
    assert ap._users_from_listing("garbage") is None
