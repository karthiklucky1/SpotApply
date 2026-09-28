"""Account deletion must leave NOTHING behind — the leftovers production had.

Measured 2026-09-28 (counts only): one deleted account still owned 18,882 job
rows and 18,990 funnel events, plus 12 tailored documents in storage — every
other table was clean. Three bugs, each pinned here:

  * ``funnel_events.job_id`` references ``job.id`` and funnel events have no
    owner column, so the purge's job DELETE hit the foreign key. The purge
    skipped the failing table and committed the rest; the route said "All
    account data deleted". Every deletion since left the user's job pool.
  * Storage was listed ONE level deep (100 entries, folders not descended),
    so ``<uid>/tailored/app_N/*`` — 51 of 63 objects in production — survived.
  * The reconcile found tenants by profile/application/usage/subscription only,
    so a tenant with nothing left but jobs or files was invisible to it; and it
    accepted any 404 as proof an auth user was gone.

Rows written here use the ``lft-`` prefix or the fixed UUIDs below and are
removed by them; the fake auth listing always contains every OTHER tenant in
the shared test database, so the only orphans the reconcile can see are ours.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlmodel import SQLModel, delete, select

from app.analytics import journey
from app.api import server
from app.common import account_purge as ap
from app.config import Settings, settings
from app.db.init_db import engine, get_session
from app.db.models import (
    Application, ApplicationStatus, FunnelEvent, Job, JobSource, PendingQuestion,
    PlatformCounter, UserProfile,
)
from tests.supabase_fakes import FakeBucket, FakeStorage, user_not_found

_P = "lft-"
GONE = f"{_P}gone"
KEEP = f"{_P}keep"
# Storage folders are only candidates when they look like Supabase user ids.
ORPHAN_UUID = "0000feed-0000-4000-8000-00000000a001"
LIVE_UUID = "0000feed-0000-4000-8000-00000000a002"
_OURS = (GONE, KEEP, ORPHAN_UUID, LIVE_UUID)


def _owner_cols(table) -> list:
    """Worked out here, not with the purge's own helper — the cleanup must not
    depend on the code under test."""
    names = ["user_id"] + list(ap._EXTRA_OWNER_COLUMNS.get(table.name, ()))
    return [table.columns[c] for c in names if c in table.columns]


def _wipe() -> None:
    with get_session() as s:
        job_ids = select(Job.id).where(Job.user_id.in_(_OURS) | Job.user_id.like(f"{_P}%"))
        app_ids = select(Application.id).where(
            Application.user_id.in_(_OURS) | Application.user_id.like(f"{_P}%"))
        s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(job_ids)))
        s.exec(delete(PendingQuestion).where(PendingQuestion.application_id.in_(app_ids)))
        s.exec(delete(FunnelEvent).where(FunnelEvent.reason.in_(
            [f"user={u}" for u in _OURS] + list(_OURS)
            + [f"{m}:{journey.user_key(u)}" for u in _OURS for m in journey.MILESTONES])))
        for u in _OURS:
            s.exec(delete(PlatformCounter).where(PlatformCounter.name.endswith(f":user:{u}")))
        for table in reversed(SQLModel.metadata.sorted_tables):
            for col in _owner_cols(table):
                s.exec(table.delete().where(col.in_(_OURS) | col.like(f"{_P}%")))
        s.commit()


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    yield
    _wipe()


@pytest.fixture
def fk_enforced():
    """SQLite enforcing foreign keys, the way production Postgres does — the
    only way the funnel_events → job block reproduces at all."""
    if engine.dialect.name != "sqlite":
        pytest.skip("FK-enforcement rehearsal is SQLite-specific")

    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    engine.dispose()
    event.listen(engine, "connect", _fk_on)
    try:
        yield
    finally:
        event.remove(engine, "connect", _fk_on)
        # Pooled connections keep the pragma; drop them so later tests don't.
        engine.dispose()


def _seed(uid: str, jobs: int = 2) -> list[int]:
    """A profile, jobs + applications, and everything that points at them or
    names the user in text: placement events, a pending question, a score-reset
    marker, journey milestones and a per-user paid-call counter."""
    job_ids = []
    with get_session() as s:
        s.add(UserProfile(user_id=uid, professional_summary="private text"))
        for i in range(jobs):
            j = Job(source=JobSource.GREENHOUSE, external_id=f"{uid}-j{i}", company="LeftCo",
                    title="Engineer", url=f"https://x/{uid}/{i}", description="d", user_id=uid,
                    first_seen=datetime.utcnow(), discovered_at=datetime.utcnow())
            s.add(j)
            s.flush()
            a = Application(job_id=j.id, user_id=uid, status=ApplicationStatus.SHORTLISTED,
                            apply_track="manual")
            s.add(a)
            s.flush()
            s.add(FunnelEvent(job_id=j.id, stage="placement", passed=True, reason="created",
                              metadata_json=f'{{"user_id": "{uid}"}}'))
            s.add(PendingQuestion(application_id=a.id, field_label="visa?",
                                  field_selector="#v", field_type="text"))
            job_ids.append(j.id)
        s.add(FunnelEvent(job_id=None, stage="frozen_score_reset", passed=True,
                          reason=f"user={uid}"))
        s.add(FunnelEvent(job_id=None, stage="document_downloaded", passed=True, reason=uid))
        s.add(FunnelEvent(job_id=None, stage="profile_completed", passed=True, reason=uid,
                          metadata_json=f'{{"user_id": "{uid}"}}'))
        key = journey.user_key(uid)
        for m in ("signup", "active_day"):
            s.add(FunnelEvent(job_id=None, stage=journey.STAGE, passed=True, reason=f"{m}:{key}"))
        s.add(PlatformCounter(name=f"paid_calls:user:{uid}", day=date.today(), count=3))
        s.commit()
    return job_ids


def _left(uid: str) -> dict:
    key = journey.user_key(uid)
    with get_session() as s:
        job_ids = [j for j in s.exec(select(Job.id).where(Job.user_id == uid)).all()]
        app_ids = [a for a in s.exec(select(Application.id).where(Application.user_id == uid)).all()]
        return {
            "profiles": len(s.exec(select(UserProfile.id).where(UserProfile.user_id == uid)).all()),
            "jobs": len(job_ids),
            "apps": len(s.exec(select(Application.id).where(Application.user_id == uid)).all()),
            "events_on_jobs": len(s.exec(select(FunnelEvent.id).where(
                FunnelEvent.job_id.in_(job_ids))).all()) if job_ids else 0,
            "pending": len(s.exec(select(PendingQuestion.id).where(
                PendingQuestion.application_id.in_(app_ids))).all()) if app_ids else 0,
            "markers": len(s.exec(select(FunnelEvent.id).where(
                FunnelEvent.job_id.is_(None),
                FunnelEvent.reason.in_([uid, f"user={uid}"]))).all()),
            "journey": len(s.exec(select(FunnelEvent.id).where(
                FunnelEvent.stage == journey.STAGE,
                FunnelEvent.reason.like(f"%:{key}"))).all()),
            "counters": len(s.exec(select(PlatformCounter.id).where(
                PlatformCounter.name == f"paid_calls:user:{uid}")).all()),
        }


_NOTHING = {"profiles": 0, "jobs": 0, "apps": 0, "events_on_jobs": 0, "pending": 0,
            "markers": 0, "journey": 0, "counters": 0}


# ── rows ─────────────────────────────────────────────────────────────────────

def test_jobs_are_deleted_even_with_funnel_events_pointing_at_them(fk_enforced):
    """THE production leftover. With the FK enforced, the job DELETE fails
    unless the events that point at the jobs go first."""
    _seed(GONE, jobs=3)
    _seed(KEEP, jobs=1)
    # Counted, not assumed: the shared SQLite test DB reuses deleted ids, so
    # rows other tests left dangling can point at these new jobs too — and
    # they are exactly what the purge must remove with them.
    gone_before, keep_before = _left(GONE), _left(KEEP)
    assert gone_before["events_on_jobs"] >= 3 and gone_before["pending"] >= 3
    counts = ap.purge_user_data(GONE)
    assert _left(GONE) == _NOTHING
    assert counts["job"] == 3 and counts["application"] == 3
    assert counts["pendingquestion"] == gone_before["pending"]
    # every event on the user's jobs + 3 events naming the user + 2 journey milestones
    assert gone_before["markers"] == 3
    assert counts["funnel_events"] == gone_before["events_on_jobs"] + 3 + 2
    assert counts["platform_counter"] == 1
    assert _left(KEEP) == keep_before, "another tenant's rows were touched"


def test_every_event_that_names_a_user_without_a_job_is_deleted_with_them():
    """Source scan: a job-less FunnelEvent whose reason or metadata carries a
    user id must use a stage the purge deletes by (``_USER_REASON_STAGES``) —
    otherwise that id outlives the account. Events tied to a job go with the
    job (``_dependent_links``); journey rows carry a salted key, not the id."""
    import ast
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    missed, seen = [], set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            is_record = name.endswith(("FunnelTracker.record", "FunnelTracker.record_many"))
            if not (is_record or name.split(".")[-1] == "FunnelEvent"):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            job = (node.args[0] if node.args else kw.get("job_id")) if is_record else kw.get("job_id")
            stage = (node.args[1] if len(node.args) > 1 else kw.get("stage")) if is_record else kw.get("stage")
            names_user = any(re.search(r"\b(uid|user_id|who|user)\b", ast.unparse(kw[k]))
                             for k in ("reason", "metadata", "metadata_json") if k in kw)
            jobless = job is None or (isinstance(job, ast.Constant) and job.value is None)
            if not (names_user and jobless):
                continue
            stage_name = stage.value if isinstance(stage, ast.Constant) else ast.unparse(stage)
            seen.add(stage_name)
            if stage_name not in ap._USER_REASON_STAGES:
                missed.append(f"{path.relative_to(root.parent)}:{node.lineno} stage={stage_name}")
    assert not missed, f"events naming a user that account deletion never removes: {missed}"
    assert set(ap._USER_REASON_STAGES) <= seen, "the scan no longer sees the known writers"


def test_a_failed_statement_rolls_the_whole_purge_back():
    """All or nothing. A failure used to be skipped and the rest committed —
    and the route then answered "All account data deleted"."""
    _seed(GONE, jobs=2)
    before = _left(GONE)

    def _boom(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("DELETE FROM JOB "):
            raise RuntimeError("simulated statement timeout")

    event.listen(engine, "before_cursor_execute", _boom)
    try:
        with pytest.raises(ap.PurgeError) as exc:
            ap.purge_user_data(GONE)
    finally:
        event.remove(engine, "before_cursor_execute", _boom)
    assert exc.value.step == "job.user_id"
    assert GONE not in str(exc.value), "the error names the table, never the user"
    # Events, pending questions and applications were deleted BEFORE the job
    # statement failed — the rollback must have restored them.
    assert _left(GONE) == before and before["pending"] >= 2


def test_a_database_that_fails_before_any_delete_is_an_honest_purge_error():
    """A failure while reading what to delete must say "nothing deleted" too —
    not escape as a raw 500 the dialog can only call "Deletion failed"."""
    _seed(GONE, jobs=1)

    def _boom(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT APPLICATION.ID"):
            raise RuntimeError("could not connect to server")

    event.listen(engine, "before_cursor_execute", _boom)
    try:
        with pytest.raises(ap.PurgeError) as exc:
            ap.purge_user_data(GONE)
    finally:
        event.remove(engine, "before_cursor_execute", _boom)
    assert exc.value.step == "start"
    assert _left(GONE)["profiles"] == 1 and _left(GONE)["jobs"] == 1


def test_local_file_cleanup_can_never_fail_a_finished_purge(monkeypatch):
    """The rows are already gone when the disk is cleaned; an error there must
    not turn a completed deletion into a failure that skips sign-in removal."""
    import app.common.user_files as uf
    _seed(GONE, jobs=1)

    def _broken(*a, **k):
        raise RuntimeError("disk misconfigured")
    monkeypatch.setattr(uf, "tailored_dir", _broken)
    counts = ap.purge_user_data(GONE)
    assert counts["job"] == 1 and _left(GONE) == _NOTHING


def test_every_row_that_points_at_a_tenant_is_deleted_first():
    """The purge deletes rows that belong to a tenant only through what they
    point at — one level deep. A table pointing at one of THOSE would need a
    second level, and the purge would fail on it; this says so first."""
    links = {(c.name, cc.name, pc.table.name) for c, cc, pc, _o in ap._dependent_links()}
    assert ("funnel_events", "job_id", "job") in links
    assert ("pendingquestion", "application_id", "application") in links
    dependents = {c for c, _cc, _pt in links}
    for table in SQLModel.metadata.tables.values():
        for fk in table.foreign_keys:
            assert fk.column.table.name not in dependents, (
                f"{table.name} points at {fk.column.table.name}, which the purge only "
                f"deletes as a dependent — deleting it would hit this foreign key")


def test_text_keyed_rows_match_exactly():
    """``…:user:<uid>`` is matched as a literal suffix: another tenant whose
    id merely contains this one, or LIKE wildcards in an id, are never hit."""
    tricky = f"{_P}a_b%c"
    lookalike = f"{_P}aXbYc"
    with get_session() as s:
        for u in (tricky, lookalike, f"{tricky}-2"):
            s.add(PlatformCounter(name=f"paid_calls:user:{u}", day=date.today(), count=1))
        s.commit()
    ap.purge_user_data(tricky)
    with get_session() as s:
        names = set(s.exec(select(PlatformCounter.name).where(
            PlatformCounter.name.like(f"paid_calls:user:{_P}%"))).all())
    assert names == {f"paid_calls:user:{lookalike}", f"paid_calls:user:{tricky}-2"}
    with get_session() as s:
        for u in (lookalike, f"{tricky}-2"):
            s.exec(delete(PlatformCounter).where(PlatformCounter.name == f"paid_calls:user:{u}"))
        s.commit()


def test_local_files_go_with_the_rows(monkeypatch, tmp_path):
    """Tailored documents sit on this container's disk too, and each tenant has
    a FAISS index; the purge knew neither."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "faiss_index_path", tmp_path / "jobs.faiss")
    from app.common.user_files import tailored_dir, user_index_paths
    _seed(GONE, jobs=2)
    _seed(KEEP, jobs=1)
    with get_session() as s:
        gone_apps = list(s.exec(select(Application.id).where(Application.user_id == GONE)).all())
        keep_apps = list(s.exec(select(Application.id).where(Application.user_id == KEEP)).all())
    for app_id in gone_apps + keep_apps:
        d = tailored_dir(app_id)
        d.mkdir(parents=True)
        (d / "resume.docx").write_bytes(b"x")
    for uid in (GONE, KEEP):
        for p in user_index_paths(uid):
            p.write_bytes(b"x")
    (tmp_path / "jobs.faiss").write_bytes(b"shared")

    ap.purge_user_data(GONE)

    assert not any(tailored_dir(a).exists() for a in gone_apps)
    assert not any(p.exists() for p in user_index_paths(GONE))
    assert all(tailored_dir(a).exists() for a in keep_apps)
    assert all(p.exists() for p in user_index_paths(KEEP))
    assert (tmp_path / "jobs.faiss").exists(), "the shared default index was deleted"


# ── storage ──────────────────────────────────────────────────────────────────

def _sb(storage: FakeStorage, **kw):
    return SimpleNamespace(storage=storage, **kw)


def test_storage_walks_every_folder_and_every_page():
    uid = ORPHAN_UUID
    mine = ({f"{uid}/resume.pdf"}
            | {f"{uid}/tailored/app_{i}/{f}" for i in range(1, 151)
               for f in ("resume.docx", "cover_letter.docx", "report.json")}
            | {f"{uid}/loose_{n:03}.txt" for n in range(250)})       # > 2 pages in one folder
    theirs = {f"{LIVE_UUID}/resume.pdf", f"{LIVE_UUID}/tailored/app_9/resume.docx"}
    bucket = FakeBucket(mine | theirs)
    out = ap.purge_user_storage(uid, _sb(FakeStorage(resume=bucket)))
    assert out == {"resume": True, "avatars": True}
    assert bucket.objects == theirs, "an object was left behind or another tenant's removed"


def test_a_uid_that_prefixes_another_never_touches_it():
    bucket = FakeBucket({f"{_P}a/cv.pdf", f"{_P}ab/cv.pdf", f"{_P}ab/tailored/app_1/r.docx"})
    out = ap.purge_user_storage(f"{_P}a", _sb(FakeStorage(resume=bucket)))
    assert out["resume"] is True
    assert bucket.objects == {f"{_P}ab/cv.pdf", f"{_P}ab/tailored/app_1/r.docx"}
    assert all(p.startswith(f"{_P}a/") for p in bucket.removed)


def test_a_heavy_account_is_still_deletable():
    """The 2026-09-18 review found a 1,000-call listing cap that stranded any
    tenant past ~500 tailored applications. 600 here, two files each."""
    uid = ORPHAN_UUID
    bucket = FakeBucket({f"{uid}/tailored/app_{i}/{f}" for i in range(600)
                         for f in ("resume.docx", "cover_letter.docx")})
    out = ap.purge_user_storage(uid, _sb(FakeStorage(resume=bucket)))
    assert out["resume"] is True and not bucket.objects


def test_an_object_that_survives_removal_is_never_reported_clean():
    uid = ORPHAN_UUID
    stuck = f"{uid}/tailored/app_1/resume.docx"
    bucket = FakeBucket({f"{uid}/resume.pdf", stuck}, refuse={stuck})
    out = ap.purge_user_storage(uid, _sb(FakeStorage(resume=bucket)))
    assert out["resume"] is False
    assert bucket.objects == {stuck}


def test_one_bucket_failing_does_not_skip_the_other():
    uid = ORPHAN_UUID
    avatars = FakeBucket({f"{uid}/photo.png"})
    storage = FakeStorage(resume=FakeBucket({f"{uid}/resume.pdf"}, fail_list=True), avatars=avatars)
    out = ap.purge_user_storage(uid, _sb(storage))
    assert out == {"resume": False, "avatars": True}
    assert not avatars.objects


# ── the route ────────────────────────────────────────────────────────────────

class _Admin:
    def __init__(self):
        self.deleted = None

    def delete_user(self, uid):
        self.deleted = uid


def _route(monkeypatch, uid, sb=None, client_error=False):
    monkeypatch.setattr(server, "_require_user", lambda request: uid)
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    import app.db.supabase_client as sc

    def _client():
        if client_error:
            raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY missing")
        return sb
    monkeypatch.setattr(sc, "service_client", _client)
    return server.delete_account(request=None)


def _json(result) -> dict:
    import json
    return json.loads(result.body) if hasattr(result, "body") else result


def test_no_admin_client_deletes_nothing(monkeypatch):
    """It used to delete the rows, skip storage and sign-in, and say success."""
    _seed(GONE, jobs=1)
    result = _route(monkeypatch, GONE, client_error=True)
    assert result.status_code == 503
    assert _json(result)["success"] is False and "nothing was" in _json(result)["message"]
    assert _left(GONE)["profiles"] == 1 and _left(GONE)["jobs"] == 1


def test_a_failed_row_purge_touches_neither_files_nor_sign_in(monkeypatch):
    _seed(GONE, jobs=1)
    bucket = FakeBucket({f"{GONE}/resume.pdf"})
    sb = _sb(FakeStorage(resume=bucket), auth=SimpleNamespace(admin=_Admin()))

    def _fail(uid):
        raise ap.PurgeError("job.user_id", RuntimeError("timeout"))
    monkeypatch.setattr(ap, "purge_user_data", _fail)
    result = _route(monkeypatch, GONE, sb=sb)
    assert result.status_code == 503 and _json(result)["success"] is False
    assert "still works" in _json(result)["message"]
    assert bucket.objects == {f"{GONE}/resume.pdf"}, "files deleted after the rows failed"
    assert sb.auth.admin.deleted is None, "sign-in removed although nothing was deleted"


def test_the_route_removes_everything_and_signs_the_user_out(monkeypatch, fk_enforced):
    _seed(GONE, jobs=2)
    bucket = FakeBucket({f"{GONE}/resume.pdf", f"{GONE}/resume.md",
                         f"{GONE}/tailored/app_1/resume.docx", f"{GONE}/tailored/app_1/cover.docx"})
    sb = _sb(FakeStorage(resume=bucket), auth=SimpleNamespace(admin=_Admin()))
    import app.db.supabase_client as sc
    sc._JWT_CACHE["lft-token"] = (time.monotonic() + 60, {"sub": GONE})
    sc._JWT_CACHE["lft-token-other"] = (time.monotonic() + 60, {"sub": KEEP})
    try:
        result = _route(monkeypatch, GONE, sb=sb)
        assert result["success"] is True and result["storage_deleted"] is True
        assert result["message"] == "All account data deleted."
        assert _left(GONE) == _NOTHING
        assert not bucket.objects
        assert sb.auth.admin.deleted == GONE
        # A deleted account's open tab stops authenticating at once.
        assert "lft-token" not in sc._JWT_CACHE and "lft-token-other" in sc._JWT_CACHE
    finally:
        sc._JWT_CACHE.pop("lft-token", None)
        sc._JWT_CACHE.pop("lft-token-other", None)


# ── the reconcile ────────────────────────────────────────────────────────────

class _Auth:
    """list_users pages; get_user_by_id answers the SDK's user_not_found for
    anyone not in ``known`` — or ``lookup_error()`` when given."""

    def __init__(self, listed, known=None, lookup_error=None):
        self.listed = list(listed)
        self.known = {u.lower() for u in (self.listed if known is None else known)}
        self.lookup_error = lookup_error
        self.lookups: list = []
        outer = self

        class _A:
            def list_users(self, page=1, per_page=1000):
                chunk = outer.listed[(page - 1) * per_page: page * per_page]
                return [SimpleNamespace(id=u) for u in chunk]

            def get_user_by_id(self, uid):
                outer.lookups.append(uid)
                if uid.lower() in outer.known:
                    return SimpleNamespace(user=SimpleNamespace(id=uid))
                raise (outer.lookup_error() if outer.lookup_error else user_not_found())

            def delete_user(self, uid):          # pragma: no cover - never called
                raise AssertionError("the reconcile never deletes auth users")

        self.admin = _A()


def _reconcile(monkeypatch, storage: FakeStorage, live=(), lookup_error=None, **kw):
    others = sorted(u for u in ap._tenant_ids() if u not in _OURS and not u.startswith(_P))
    auth = _Auth(others + list(live), lookup_error=lookup_error)
    sb = SimpleNamespace(auth=auth, storage=storage)
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    monkeypatch.setattr(settings, "orphan_purge_enabled", True, raising=False)
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "service_client", lambda: sb)
    return ap.purge_orphaned_accounts(**kw), auth


def _job_only_leftover(uid: str) -> None:
    """What every purge left in production: jobs and the events on them."""
    _seed(uid, jobs=2)
    with get_session() as s:
        s.exec(delete(PendingQuestion).where(PendingQuestion.application_id.in_(
            select(Application.id).where(Application.user_id == uid))))
        s.exec(delete(Application).where(Application.user_id == uid))
        s.exec(delete(UserProfile).where(UserProfile.user_id == uid))
        s.commit()


def test_the_reconcile_finds_a_tenant_that_only_has_jobs_left(monkeypatch, fk_enforced):
    _job_only_leftover(GONE)
    _seed(KEEP, jobs=1)
    out, auth = _reconcile(monkeypatch, FakeStorage(), live=[KEEP])
    assert out["aborted"] is None and out["candidates"] == 1
    assert len(out["purged"]) == 1 and out["rows_failed"] == 0
    assert _left(GONE) == _NOTHING
    assert _left(KEEP)["jobs"] == 1
    assert GONE not in repr(out), "the reconcile result must never carry an id"


def test_the_reconcile_finds_files_whose_rows_are_already_gone(monkeypatch):
    """The other production leftover: a deleted account's tailored documents,
    with no row anywhere naming it. Only the bucket root still does."""
    resume = FakeBucket({f"{ORPHAN_UUID}/resume.pdf",
                         f"{ORPHAN_UUID}/tailored/app_7/resume.docx",
                         f"{ORPHAN_UUID}/tailored/app_7/cover_letter.docx",
                         f"{LIVE_UUID}/resume.pdf",
                         f"{LIVE_UUID}/tailored/app_3/resume.docx",
                         "public/logo.png"})
    out, auth = _reconcile(monkeypatch, FakeStorage(resume=resume), live=[LIVE_UUID])
    assert out["aborted"] is None and out["storage_scan"] == "ok"
    assert out["storage_only"] == 1 and out["storage_failed"] == 0
    assert ORPHAN_UUID in auth.lookups, "a storage-only candidate must be confirmed too"
    assert resume.objects == {f"{LIVE_UUID}/resume.pdf",
                              f"{LIVE_UUID}/tailored/app_3/resume.docx",
                              "public/logo.png"}


def test_a_gateway_404_never_deletes_a_folder(monkeypatch):
    class _Gateway404(Exception):
        status = 404

    resume = FakeBucket({f"{ORPHAN_UUID}/resume.pdf"})
    out, _auth = _reconcile(monkeypatch, FakeStorage(resume=resume), live=[LIVE_UUID],
                            lookup_error=lambda: _Gateway404("<html>404 Not Found</html>"))
    assert out["unconfirmed"] == 1 and out["purged"] == []
    assert resume.objects == {f"{ORPHAN_UUID}/resume.pdf"}


def test_a_failed_row_purge_still_removes_a_confirmed_orphans_files(monkeypatch):
    _job_only_leftover(GONE)
    resume = FakeBucket({f"{GONE}/tailored/app_1/resume.docx"})

    def _fail(uid):
        raise ap.PurgeError("job.user_id", RuntimeError("timeout"))
    monkeypatch.setattr(ap, "purge_user_data", _fail)
    out, _auth = _reconcile(monkeypatch, FakeStorage(resume=resume), live=[LIVE_UUID])
    assert out["rows_failed"] == 1 and out["purged"] == []
    assert not resume.objects
    assert _left(GONE)["jobs"] == 2, "rows were rolled back, so they are retried tomorrow"


def test_the_job_owner_scan_matches_a_plain_distinct():
    _seed(GONE, jobs=2)
    _seed(KEEP, jobs=1)
    with get_session() as s:
        fast = set(ap._job_owner_ids(s))
        plain = {r for r in s.exec(select(Job.user_id).distinct()).all() if isinstance(r, str)}
    assert fast == plain and {GONE, KEEP} <= fast


def test_the_counts_summary_carries_the_new_failure_counters():
    summary = {"auth_users": 3, "candidates": 2, "storage_only": 1, "purged": [{}],
               "kept": 0, "unconfirmed": 0, "rows_failed": 1, "storage_failed": 1,
               "storage_scan": "ok", "aborted": None}
    counts = ap._summary_counts(summary)
    assert counts["rows_failed"] == 1 and counts["storage_failed"] == 1
    assert counts["storage_only"] == 1 and counts["purged"] == 1

