"""Remove everything we hold for one tenant — and find the tenants nobody removed.

Two callers, ONE deletion:

  * ``DELETE /api/account`` (server.delete_account) — the user asked.
  * ``purge_orphaned_accounts`` — the daily reconcile. The Supabase Auth user is
    gone but our rows or files are not. Audit 2026-09: one auth user no longer
    existed, yet its userprofile (with professional-summary text), 2,860
    applications, 33,192 job copies, 13 storage objects, 25 usage rows and a
    coupon subscription row were all still here. Either the route was never
    used or the user was deleted straight in Supabase Auth; either way nothing
    compared auth against our tables, and the route only cleaned the "resume"
    bucket while an "avatars" bucket exists too.

The deletion is SCHEMA-DRIVEN on purpose (see ``purge_user_data``): a hand-kept
table list fell behind the schema — 7 named while 18 carried a ``user_id``.
tests/test_account_deletion.py fails the moment a new user-scoped table appears
without a deletion story.

It is also ALL-OR-NOTHING (2026-09-28). It used to skip a table that failed and
commit the rest, and the route answered "All account data deleted". In
production that table was ``job``, for EVERY deletion: ``funnel_events.job_id``
references ``job.id`` and funnel events carry no owner column, so the job
DELETE hit the foreign key and was skipped — the reconcile above removed that
tenant's profile, applications and usage and left 18,882 job copies and 18,990
events behind, invisible to a reconcile that did not look at ``job``. Rows that
point at a tenant's rows, and rows that name the tenant in text, are now
deleted first; any failure rolls the whole purge back and raises
``PurgeError``, so a caller can never report a partial purge as done.

Storage is walked, not glanced at: objects live at ``<uid>/tailored/app_N/…``
three levels down, and a one-level listing (100 entries, folders not descended)
removed the résumé and left every tailored document — 51 of the 63 objects in
production were nested.

The reconcile is built to do NOTHING when in doubt. Its input is a listing of
every auth user; a listing that raised, came back empty, or did not paginate to
the end is not "no users" — it is "we do not know", and acting on it would
delete every tenant. Each candidate must then be confirmed by Auth's own
``user_not_found`` verdict about THAT user. It never purges more than
``max_users`` per run, and it logs COUNTS ONLY: no user id, no email, ever.
"""
from __future__ import annotations

import logging
import re
import shutil
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

# Storage buckets that hold per-user objects under a ``<uid>/`` prefix. Both are
# private. The route cleaned only the first for as long as the second existed.
STORAGE_BUCKETS = ("resume", "avatars")

# Tables that own user data under a column OTHER than ``user_id``. Everything
# with a plain ``user_id`` column is found from the schema instead of being
# listed, so a new user-scoped table is covered the moment it exists.
_EXTRA_OWNER_COLUMNS = {
    "candidateintro": ("candidate_user_id", "recruiter_user_id"),
    "intromessage": ("sender_user_id",),
    "introrating": ("rater_user_id", "ratee_user_id"),
}

# Identities that are never a tenant. SHARED_POOL_USER owns the pool every
# tenant is served from, so "delete this user" for it would wipe the corpus;
# "local" is the SQLite dev identity; the rest are the shapes a missing id
# takes on its way through the code.
_SENTINEL_STRINGS = frozenset({"", "local", "shared"})

# Hard ceiling on auth-listing pages. per_page is 1000, so this is a million
# users — far past anything real; hitting it means the pagination is looping,
# and a loop is "did not complete", not "complete".
_MAX_LIST_PAGES = 1000
_LIST_PER_PAGE = 1000

# Storage. Supabase lists ONE folder level per call (default and page size
# 100). Both ceilings are loop guards, not budgets — a listing that never ends
# is "did not finish", never "empty": a real tenant needs about one call per
# tailored application, and the largest in production holds 40 objects. A
# small fixed cap (the 1,000-call version reviewed on 2026-09-18) would have
# made every tenant past ~500 applications undeletable.
_STORAGE_PAGE = 100
_STORAGE_REMOVE_CHUNK = 100
_STORAGE_MAX_PAGES = 5_000          # per folder
_STORAGE_MAX_ENTRIES = 500_000      # per tenant tree
# list → remove → list again. A bucket is clean only when a listing comes back
# EMPTY, so an early page stop, an offset shifted by the removal or an object
# the API refused is caught by the next round instead of being reported clean.
_STORAGE_ROUNDS = 3

# Job-less funnel events that name ONE user in ``reason`` — the bare id
# (``document_downloaded``, ``profile_completed``) or ``user=<id>`` (the
# per-user score-reset markers in matching/pipeline.py). Listed by STAGE so the
# delete rides ix_funnel_events_stage: filtering the ~840k job-less rows by
# reason alone walks all of them to find one or two. The source scan in
# tests/test_deletion_leftovers.py fails when a writer names a user under a
# stage missing from this list.
_USER_REASON_STAGES = ("stale_sponsorship_reset", "frozen_score_reset",
                       "document_downloaded", "profile_completed")

# Supabase Auth user ids. A storage folder is only a candidate tenant if its
# name is one — never "public", "tailored" or whatever a bug might create.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Distinct job owners by loose index scan — one index descent per owner
# (~13 in production) instead of a DISTINCT over ~1.5M rows. Same shape as
# scoring_lane._SKIP_SCAN_OWNERS, on ix_job_user_id (user_id).
_JOB_OWNERS_SKIP_SCAN = """
WITH RECURSIVE owners AS (
    SELECT (SELECT j.user_id FROM job j WHERE j.user_id IS NOT NULL
             ORDER BY j.user_id LIMIT 1) AS user_id
    UNION ALL
    SELECT (SELECT j.user_id FROM job j WHERE j.user_id > o.user_id
             ORDER BY j.user_id LIMIT 1)
      FROM owners o WHERE o.user_id IS NOT NULL
)
SELECT user_id FROM owners WHERE user_id IS NOT NULL
"""


class PurgeError(RuntimeError):
    """The row purge failed and was ROLLED BACK: nothing was deleted.

    ``step`` names the table (and column) that failed — never the user."""

    def __init__(self, step: str, cause: BaseException):
        super().__init__(f"account purge rolled back at {step}: {type(cause).__name__}")
        self.step = step


def _shared_pool_user() -> str:
    from app.discovery.pipeline import SHARED_POOL_USER
    return SHARED_POOL_USER


def is_sentinel(uid: Any) -> bool:
    """True for every identity that must never be purged."""
    if uid is None:
        return True
    if not isinstance(uid, str):
        return True
    s = uid.strip()
    return s in _SENTINEL_STRINGS or s == _shared_pool_user()


# ── the one deletion ─────────────────────────────────────────────────────────

def _owner_columns(table) -> list:
    """The columns through which ``table`` belongs to a user (may be empty)."""
    cols = [table.columns[c] for c in ("user_id",) if c in table.columns]
    cols += [table.columns[c] for c in _EXTRA_OWNER_COLUMNS.get(table.name, ())
             if c in table.columns]
    return cols


def _dependent_links() -> list[tuple]:
    """``(child, child_col, parent_col, parent_owner_col)`` for every foreign key
    from a table WITHOUT an owner column into a table WITH one.

    Those rows belong to the tenant only through what they point at, and they
    block the parent's delete: ``pendingquestion`` → ``application`` and
    ``funnel_events`` → ``job``. The second was missed for as long as the purge
    existed (see the module docstring). Derived from the schema so a new one
    cannot be missed the same way.
    """
    from sqlmodel import SQLModel
    links = []
    for child in reversed(SQLModel.metadata.sorted_tables):
        if _owner_columns(child):
            continue
        for fk in child.foreign_keys:
            parent = fk.column.table
            for owner in _owner_columns(parent):
                links.append((child, fk.parent, fk.column, owner))
    return links


def _text_keyed_deletes(uid: str) -> Iterator[tuple[str, Any]]:
    """``(step, DELETE)`` for rows that name the tenant in TEXT, not a column.

    * job-less funnel events whose ``reason`` is the user id or
      ``user=<uid>`` (``_USER_REASON_STAGES``);
    * journey milestones, ``reason='<milestone>:<user key>'`` — the key is a
      salted hash, recomputable here and nowhere outside the server;
    * per-user daily counters, ``platform_counter.name='…:user:<uid>'``
      (``compute_policy.reserve_paid_call``).
    """
    from sqlmodel import delete as sql_delete

    from app.analytics import journey
    from app.db.models import FunnelEvent, PlatformCounter

    yield ("funnel_events.reason:user", sql_delete(FunnelEvent).where(
        FunnelEvent.stage.in_(_USER_REASON_STAGES), FunnelEvent.job_id.is_(None),
        FunnelEvent.reason.in_([uid, f"user={uid}"])))
    key = journey.user_key(uid)
    yield ("funnel_events.reason:journey", sql_delete(FunnelEvent).where(
        FunnelEvent.stage == journey.STAGE,
        FunnelEvent.reason.in_([f"{m}:{key}" for m in journey.MILESTONES])))
    yield ("platform_counter.name", sql_delete(PlatformCounter).where(
        PlatformCounter.name.endswith(f":user:{uid}", autoescape=True)))


def purge_user_data(uid: str) -> dict[str, int]:
    """Delete every row this tenant owns. Returns ``{table: rows_deleted}``.

    ALL OR NOTHING: one transaction; the first statement that fails rolls
    everything back and raises ``PurgeError``. The old per-statement SAVEPOINT
    kept going past a failure, committed the rest and let the route say "All
    account data deleted" — with the tenant's whole job pool still there.

    Order: rows that point at the tenant's rows (``_dependent_links``), rows
    that name the tenant in text (``_text_keyed_deletes``), then every table
    with an owner column CHILDREN FIRST — ``sorted_tables`` is FK-dependency
    order (parents first), and Postgres, unlike the SQLite the tests run on,
    enforces the FK and rejected ``job`` while ``application`` still pointed
    at it.

    Then, once the rows are gone, the tenant's files on this container's disk
    (tailored documents, FAISS index) — best effort, never a reason to fail.

    Refuses sentinel identities by raising ValueError — a caller that wants a
    400 (the route) or a skip (the reconcile) decides that itself; this
    function never turns a refusal into a silent no-op.
    """
    if is_sentinel(uid):
        raise ValueError("refusing to purge a system identity")

    from sqlmodel import SQLModel, delete as sql_delete, select

    from app.db.init_db import get_session
    from app.db.models import Application

    deleted: dict[str, int] = {}

    def _count(table_name: str, result) -> None:
        deleted[table_name] = deleted.get(table_name, 0) + (result.rowcount or 0)

    app_ids: list[int] = []
    with get_session() as session:
        step = "start"
        try:
            app_ids = [a for a in session.exec(
                select(Application.id).where(Application.user_id == uid)).all() if a is not None]
            for child, child_col, parent_col, owner_col in _dependent_links():
                step = f"{child.name}.{child_col.name}"
                _count(child.name, session.exec(sql_delete(child).where(
                    child_col.in_(select(parent_col).where(owner_col == uid)))))
            for text_step, stmt in _text_keyed_deletes(uid):
                step = text_step
                _count(text_step.split(".")[0], session.exec(stmt))
            for table in reversed(SQLModel.metadata.sorted_tables):
                for col in _owner_columns(table):
                    step = f"{table.name}.{col.name}"
                    _count(table.name, session.exec(sql_delete(table).where(col == uid)))
            session.commit()
        except Exception as e:
            session.rollback()
            log.error("Account purge rolled back at %s — nothing deleted: %s",
                      step, type(e).__name__)
            raise PurgeError(step, e) from e

    _remove_local_files(uid, app_ids)
    return deleted


def _remove_local_files(uid: str, app_ids: list[int]) -> int:
    """Delete the tenant's files on THIS container's disk. Best effort: the
    disk is ephemeral (a redeploy wipes it), so a failure here is logged and
    never fails a deletion that already removed the rows — it NEVER raises."""
    try:
        return _remove_local_files_unguarded(uid, app_ids)
    except Exception as e:
        log.warning("Account purge: local file cleanup skipped: %s", type(e).__name__)
        return 0


def _remove_local_files_unguarded(uid: str, app_ids: list[int]) -> int:
    from app.common.user_files import tailored_dir, user_index_paths
    from app.config import settings

    removed = 0
    for app_id in app_ids:
        d = tailored_dir(app_id)
        try:
            if d.is_dir():
                shutil.rmtree(d)
                removed += 1
        except OSError as e:
            log.warning("Account purge: a tailored-documents dir was not removed: %s",
                        type(e).__name__)
    index_path, id_map_path = user_index_paths(uid)
    if index_path != settings.faiss_index_path:     # never the shared default
        for p in (index_path, id_map_path):
            try:
                if p.exists():
                    p.unlink()
                    removed += 1
            except OSError as e:
                log.warning("Account purge: an index file was not removed: %s", type(e).__name__)
    return removed


# ── storage ──────────────────────────────────────────────────────────────────

def _list_folder(bucket, folder: str) -> Iterator[dict]:
    """Every entry directly inside ``folder``, across pages."""
    offset = 0
    for _ in range(_STORAGE_MAX_PAGES):
        page = bucket.list(folder, {"limit": _STORAGE_PAGE, "offset": offset,
                                    "sortBy": {"column": "name", "order": "asc"}})
        if not isinstance(page, list):
            raise RuntimeError("unrecognised storage listing")
        for item in page:
            if not isinstance(item, dict):
                raise RuntimeError("unrecognised storage entry")
            yield item
        if len(page) < _STORAGE_PAGE:
            return
        offset += len(page)
    raise RuntimeError("storage listing did not finish")


def _object_paths(bucket, root: str) -> list[str]:
    """Every object path under ``root/``, descending into folders.

    Supabase reports a folder as an entry whose ``id`` is null. Anything this
    cannot read raises: an unreadable listing is "unknown", never "empty".
    """
    paths: list[str] = []
    folders, seen, entries = [root], set(), 0
    while folders:
        folder = folders.pop()
        if folder in seen:
            raise RuntimeError("storage listing revisited a folder")
        seen.add(folder)
        for item in _list_folder(bucket, folder):
            entries += 1
            if entries > _STORAGE_MAX_ENTRIES:
                raise RuntimeError("storage listing did not finish")
            name = item.get("name")
            if not isinstance(name, str) or not name or name in (".", "..") or "/" in name:
                raise RuntimeError("unrecognised storage entry")
            path = f"{folder}/{name}"
            (folders if item.get("id") is None else paths).append(path)
    return paths


def purge_user_storage(uid: str, sb) -> dict[str, Optional[bool]]:
    """Remove the tenant's objects from every per-user bucket.

    Returns ``{bucket: True|False}`` — True only when a listing of the whole
    ``<uid>/`` tree came back EMPTY, False when listing or removal raised or
    objects remained after ``_STORAGE_ROUNDS`` passes. Buckets are
    independent: a failure in one never skips the next, and the caller reports
    the overall result honestly.
    """
    if is_sentinel(uid):
        raise ValueError("refusing to purge a system identity")
    root = uid.strip()
    out: dict[str, Optional[bool]] = {}
    for bucket_name in STORAGE_BUCKETS:
        try:
            bucket = sb.storage.from_(bucket_name)
            clean = False
            for _ in range(_STORAGE_ROUNDS):
                paths = _object_paths(bucket, root)
                if not paths:
                    clean = True
                    break
                for start in range(0, len(paths), _STORAGE_REMOVE_CHUNK):
                    chunk = paths[start:start + _STORAGE_REMOVE_CHUNK]
                    if not all(p.startswith(f"{root}/") for p in chunk):
                        raise RuntimeError("refusing to remove a path outside the tenant")
                    bucket.remove(chunk)
            out[bucket_name] = clean
            if not clean:
                log.warning("Account purge: %s storage still had objects after %d passes",
                            bucket_name, _STORAGE_ROUNDS)
        except Exception as e:
            out[bucket_name] = False
            log.warning("Account purge: %s storage cleanup failed: %s",
                        bucket_name, type(e).__name__)
    return out


def _storage_tenant_ids(sb) -> tuple[set[str], bool]:
    """Top-level folder names that look like Supabase user ids, across every
    bucket, and whether every bucket could be listed.

    A tenant whose ROWS are already gone is invisible to ``_tenant_ids`` — which
    is exactly the tenant a one-level storage listing left behind: in
    production, one deleted account's 12 tailored documents outlived all of its
    rows. The bucket root is the only place such a tenant still appears.
    """
    found: set[str] = set()
    complete = True
    for bucket_name in STORAGE_BUCKETS:
        try:
            bucket = sb.storage.from_(bucket_name)
            for item in _list_folder(bucket, ""):
                name = item.get("name")
                if item.get("id") is None and isinstance(name, str) and _UUID_RE.match(name):
                    found.add(name)
        except Exception as e:
            complete = False
            log.warning("Orphan purge: could not list the %s bucket: %s",
                        bucket_name, type(e).__name__)
    return found, complete


# ── reconcile: tenants whose auth user is gone ───────────────────────────────

def _users_from_listing(page: Any) -> Optional[list]:
    """The user list inside one ``list_users`` page, whatever shape it took.

    supabase-py has returned a bare list of User objects and, in other
    versions, an object carrying ``.users``. Anything else is "unrecognised"
    (None), which the caller treats as a reason to abort — never as empty.
    """
    if page is None:
        return None
    if isinstance(page, list):
        return page
    users = getattr(page, "users", None)
    if isinstance(users, list):
        return users
    if isinstance(page, dict) and isinstance(page.get("users"), list):
        return page["users"]
    return None


def _user_id_of(user: Any) -> Optional[str]:
    uid = getattr(user, "id", None)
    if uid is None and isinstance(user, dict):
        uid = user.get("id")
    return str(uid).strip().lower() if uid else None


def _list_auth_user_ids(sb) -> tuple[set[str], Optional[str]]:
    """Every auth user id, lower-cased, or ``(set(), reason)`` when the listing
    cannot be trusted. Pages until one comes back short of ``per_page``."""
    ids: set[str] = set()
    for page_no in range(1, _MAX_LIST_PAGES + 1):
        try:
            page = sb.auth.admin.list_users(page=page_no, per_page=_LIST_PER_PAGE)
        except Exception as e:
            return set(), f"auth listing raised on page {page_no}: {type(e).__name__}"
        users = _users_from_listing(page)
        if users is None:
            return set(), f"auth listing page {page_no} had an unrecognised shape"
        for u in users:
            uid = _user_id_of(u)
            if uid:
                ids.add(uid)
        if len(users) < _LIST_PER_PAGE:
            return ids, None
    return set(), "auth listing pagination did not complete"


def _job_owner_ids(session) -> list[str]:
    """Distinct ``job.user_id`` values by skip scan, under a statement ceiling
    on Postgres. A failure returns [] — the owners are found on the next run;
    the plain DISTINCT is the 1.5M-row scan this exists to avoid."""
    from sqlalchemy import text
    try:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '20s'"))
        rows = session.execute(text(_JOB_OWNERS_SKIP_SCAN)).all()
        return [r[0] for r in rows if isinstance(r[0], str)]
    except Exception as e:
        log.warning("Orphan purge: job-owner scan failed, skipped this run: %s",
                    type(e).__name__)
        session.rollback()
        return []


def _tenant_ids() -> set[str]:
    """Distinct identities our user-owned tables claim exist.

    Profiles and subscriptions name every onboarded tenant; applications and
    usage rows are added because a tenant can lose their profile and keep
    thousands of applications (the audit's deleted user kept 2,860). ``job`` is
    walked by skip scan: a tenant whose purge removed everything BUT its jobs
    (every purge, while funnel_events blocked the job delete) appears nowhere
    else.
    """
    from sqlmodel import select

    from app.db.init_db import get_session
    from app.db.models import Application, UserProfile, UserSubscription, UserUsage

    found: set[str] = set()
    with get_session() as session:
        for col in (UserProfile.user_id, UserSubscription.user_id,
                    Application.user_id, UserUsage.user_id):
            for row in session.exec(select(col).distinct()).all():
                uid = row[0] if isinstance(row, tuple) else row
                if isinstance(uid, str) and uid.strip():
                    found.add(uid.strip())
        found.update(u.strip() for u in _job_owner_ids(session) if u.strip())
    return found


def _auth_user_gone(sb, uid: str) -> Optional[bool]:
    """Positively confirm ONE candidate against Auth: True = the user is
    explicitly not found (purge), False = the user exists (keep), None = could
    not tell (keep, count as unconfirmed).

    The bulk listing only NOMINATES candidates; it cannot prove absence. A
    gateway that clamps ``per_page``, or one signup landing between two
    offset-paginated pages, leaves live users out of the listing — and each of
    those would have been an irreversible purge. Deleting a tenant needs the
    server to say, about that user, "not found".

    Only Auth's own verdict counts: supabase_auth raises ``AuthApiError`` with
    ``code == "user_not_found"`` (it sends X-Supabase-Api-Version 2024-01-01,
    so the code is always present). This used to accept ANY 404 or any message
    containing "not found" — a gateway's HTML 404 page or a DNS "host not
    found" would have deleted a live account.
    """
    try:
        resp = sb.auth.admin.get_user_by_id(uid)
    except Exception as e:
        if getattr(e, "code", None) == "user_not_found":
            return True
        return None
    user = getattr(resp, "user", resp)
    found_id = _user_id_of(user)
    if found_id and found_id == uid.strip().lower():
        return False
    return None


def purge_orphaned_accounts(max_users: int = 5) -> dict:
    """Purge tenants whose Supabase Auth user no longer exists. Bounded, and
    aborts — deleting nothing — on any doubt about the auth listing.

    Returns ``{"auth_users", "candidates", "storage_only", "storage_scan",
    "purged": [per-user counts], "kept", "unconfirmed", "rows_failed",
    "storage_failed", "aborted"}``. Nothing in it, and nothing logged from it,
    identifies a person.

    Candidates come from our tables AND from the storage buckets' top-level
    folders. Two gates, both required: the bulk listing nominates a candidate,
    and a per-user ``get_user_by_id`` must then answer ``user_not_found`` for
    THAT user (``_auth_user_gone``). A candidate the listing missed but Auth
    still knows is ``kept``; one Auth could not answer for is ``unconfirmed``.
    Neither is ever deleted.

    For a confirmed candidate the rows go first, then storage. A storage
    failure is not a reason to keep the rows (which hold far more than the
    files): the folder is still at the bucket root tomorrow, and that is where
    the next run finds it.
    """
    from app.config import settings

    out: dict[str, Any] = {"auth_users": 0, "candidates": 0, "storage_only": 0,
                           "storage_scan": None, "purged": [], "kept": 0,
                           "unconfirmed": 0, "rows_failed": 0, "storage_failed": 0,
                           "aborted": None}

    def _abort(reason: str) -> dict:
        out["aborted"] = reason
        log.warning("Orphan purge aborted, nothing deleted: %s", reason)
        return out

    if not getattr(settings, "orphan_purge_enabled", True):
        return _abort("disabled")
    if not settings.use_supabase:
        return _abort("not a Supabase deployment")
    try:
        from app.db.supabase_client import service_client
        sb = service_client()
    except Exception as e:
        return _abort(f"no Supabase client: {type(e).__name__}")

    auth_ids, reason = _list_auth_user_ids(sb)
    if reason:
        return _abort(reason)
    if not auth_ids:
        # An empty listing must never mean "delete everyone".
        return _abort("auth listing returned zero users")
    out["auth_users"] = len(auth_ids)

    def _orphans(ids) -> set[str]:
        return {u for u in ids if not is_sentinel(u) and u.strip().lower() not in auth_ids}

    db_tenants = {u for u in _tenant_ids() if not is_sentinel(u)}
    db_candidates = _orphans(db_tenants)
    storage_ids, storage_complete = _storage_tenant_ids(sb)
    out["storage_scan"] = "ok" if storage_complete else "incomplete"
    storage_candidates = _orphans(storage_ids)
    db_lower = {u.strip().lower() for u in db_candidates}
    storage_only = {u for u in storage_candidates if u.strip().lower() not in db_lower}
    candidates = sorted(db_candidates | storage_only)
    out["candidates"] = len(candidates)
    out["storage_only"] = len(storage_only)
    if len(candidates) > len(auth_ids):
        # Orphans outnumbering live accounts is the signature of a truncated
        # listing, not of a product whose users have mostly left. The daily
        # bound alone would still let it eat five real tenants a day.
        return _abort(f"{len(candidates)} orphan candidates exceed {len(auth_ids)} auth users")
    # A listing from ANOTHER Supabase project (SUPABASE_URL and DATABASE_URL
    # pointing at different projects) is complete and non-empty, and that
    # Auth truthfully answers user_not_found for every one of our tenants —
    # every check above passes. Most of our tenants missing at once is its
    # signature; accounts are deleted a few at a time.
    known = {u.strip().lower() for u in db_tenants} | {u.strip().lower() for u in storage_ids}
    if len(candidates) > max(3, len(known) // 5):
        return _abort(f"{len(candidates)} of {len(known)} tenants are missing from the auth "
                      f"listing — is it another project's?")

    for uid in candidates[:max(0, int(max_users))]:
        gone = _auth_user_gone(sb, uid)
        if gone is False:
            out["kept"] += 1          # the listing missed a live user — never purge
            continue
        if gone is None:
            out["unconfirmed"] += 1   # Auth could not say — leave it for tomorrow
            continue
        rows_ok = True
        try:
            counts = purge_user_data(uid)
        except Exception as e:
            rows_ok, counts = False, {}
            out["rows_failed"] += 1
            log.error("Orphan purge: row deletion failed for one tenant (rolled back): %s",
                      type(e).__name__)
        try:
            storage = purge_user_storage(uid, sb)
        except Exception as e:
            storage = {b: False for b in STORAGE_BUCKETS}
            log.error("Orphan purge: storage cleanup failed for one tenant: %s", type(e).__name__)
        if not all(storage.values()):
            out["storage_failed"] += 1
        if rows_ok:
            out["purged"].append({
                "rows": sum(counts.values()),
                "tables": {k: v for k, v in sorted(counts.items()) if v},
                "storage": storage,
            })

    if out["kept"]:
        log.warning("Orphan purge: the auth listing missed %d live user(s) that "
                    "get_user_by_id still knows — nothing deleted for them; the "
                    "listing is not complete", out["kept"])
    if out["rows_failed"] or out["storage_failed"]:
        log.error("Orphan purge: %d tenant(s) kept their rows (rolled back), %d still "
                  "have stored files — retried on the next run",
                  out["rows_failed"], out["storage_failed"])
    log.info("Orphan purge: %d auth users, %d orphan candidates (%d storage-only), purged "
             "%d this run (%d rows), %d kept, %d unconfirmed, storage scan %s",
             out["auth_users"], out["candidates"], out["storage_only"], len(out["purged"]),
             sum(p["rows"] for p in out["purged"]), out["kept"], out["unconfirmed"],
             out["storage_scan"])
    return out


def _summary_counts(summary: dict) -> dict:
    """The reconcile result reduced to numbers, for a log line elsewhere."""
    return {
        "auth_users": summary.get("auth_users", 0),
        "candidates": summary.get("candidates", 0),
        "storage_only": summary.get("storage_only", 0),
        "purged": len(summary.get("purged") or []),
        "kept": summary.get("kept", 0),
        "unconfirmed": summary.get("unconfirmed", 0),
        "rows_failed": summary.get("rows_failed", 0),
        "storage_failed": summary.get("storage_failed", 0),
        "storage_scan": summary.get("storage_scan"),
        "aborted": summary.get("aborted"),
    }


__all__ = [
    "PurgeError", "STORAGE_BUCKETS", "is_sentinel", "purge_user_data",
    "purge_user_storage", "purge_orphaned_accounts",
]
