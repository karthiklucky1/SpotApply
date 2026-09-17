"""Remove everything we hold for one tenant — and find the tenants nobody removed.

Two callers, ONE deletion:

  * ``DELETE /api/account`` (server.delete_account) — the user asked.
  * ``purge_orphaned_accounts`` — the daily reconcile. The Supabase Auth user is
    gone but our rows are not. Audit 2026-09: one auth user no longer existed,
    yet its userprofile (with professional-summary text), 2,860 applications,
    33,192 job copies, 13 storage objects, 25 usage rows and a coupon
    subscription row were all still here. Either the route was never used or
    the user was deleted straight in Supabase Auth; either way nothing compared
    auth against our tables, and the route only cleaned the "resume" bucket
    while an "avatars" bucket exists too.

The deletion is SCHEMA-DRIVEN on purpose (see ``purge_user_data``): a hand-kept
table list fell behind the schema — 7 named while 18 carried a ``user_id``.
tests/test_account_deletion.py fails the moment a new user-scoped table appears
without a deletion story.

The reconcile is built to do NOTHING when in doubt. Its input is a listing of
every auth user; a listing that raised, came back empty, or did not paginate to
the end is not "no users" — it is "we do not know", and acting on it would
delete every tenant. It also never purges more than ``max_users`` per run, so a
wrong answer that slips through is bounded, and it logs COUNTS ONLY: no user id,
no email, ever.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

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

def purge_user_data(uid: str) -> dict[str, int]:
    """Delete every row this tenant owns. Returns ``{table: rows_deleted}``.

    Refuses sentinel identities by raising ValueError — a caller that wants a
    400 (the route) or a skip (the reconcile) decides that itself; this
    function never turns a refusal into a silent no-op.

    CHILDREN FIRST: ``sorted_tables`` is FK-dependency order (parents first), so
    deleting in reverse removes referencing rows before the rows they point at.
    Declaration order put ``job`` ahead of ``application``, and Postgres — which
    enforces the FK, unlike the SQLite the tests run on — raised on the first
    delete, poisoned the transaction, and deleted nothing.

    SAVEPOINT per statement: on Postgres a failed statement poisons the whole
    transaction, so without this the first error made every later delete AND
    the final commit fail — the route 500'd and nothing at all was deleted,
    while the except quietly logged "one table failed".
    """
    if is_sentinel(uid):
        raise ValueError("refusing to purge a system identity")

    from sqlmodel import SQLModel, delete as sql_delete, select

    from app.db.init_db import get_session
    from app.db.models import Application, PendingQuestion

    deleted: dict[str, int] = {}
    with get_session() as session:
        # PendingQuestion hangs off the application, not the user.
        app_ids = list(session.exec(
            select(Application.id).where(Application.user_id == uid)).all())
        # Chunked IN: one production tenant owned 2,860 applications, and an
        # unbounded parameter list is the kind of statement that grows with the
        # tenant until it trips a planner or driver limit.
        for start in range(0, len(app_ids), 500):
            chunk = app_ids[start:start + 500]
            r = session.exec(sql_delete(PendingQuestion).where(
                PendingQuestion.application_id.in_(chunk)))
            deleted["pendingquestion"] = deleted.get("pendingquestion", 0) + (r.rowcount or 0)

        for table in reversed(SQLModel.metadata.sorted_tables):
            name = table.name
            cols = table.columns
            owner_cols = [cols[c] for c in ("user_id",) if c in cols]
            owner_cols += [cols[c] for c in _EXTRA_OWNER_COLUMNS.get(name, ())
                           if c in cols]
            if not owner_cols:
                continue
            for col in owner_cols:
                try:
                    with session.begin_nested():
                        r = session.exec(sql_delete(table).where(col == uid))
                        deleted[name] = deleted.get(name, 0) + (r.rowcount or 0)
                except Exception as e:
                    # One undeletable table must not abandon the rest half-done.
                    log.exception("Account purge: %s.%s failed for %s: %s",
                                  name, col.name, uid, e)
        session.commit()
    return deleted


def purge_user_storage(uid: str, sb) -> dict[str, Optional[bool]]:
    """Remove the tenant's objects from every per-user bucket.

    Returns ``{bucket: True|False}`` — True when the bucket is clean, False when
    listing or removal raised. Buckets are independent: a failure in one never
    skips the next, and the caller reports the overall result honestly.
    """
    if is_sentinel(uid):
        raise ValueError("refusing to purge a system identity")
    out: dict[str, Optional[bool]] = {}
    for bucket in STORAGE_BUCKETS:
        try:
            files = sb.storage.from_(bucket).list(uid) or []
            paths = [f"{uid}/{f['name']}" for f in files if f.get("name")]
            if paths:
                sb.storage.from_(bucket).remove(paths)
            out[bucket] = True
        except Exception as e:
            out[bucket] = False
            log.exception("Account purge: %s storage cleanup failed for %s: %s",
                          bucket, uid, e)
    return out


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


def _tenant_ids() -> set[str]:
    """Distinct identities our user-owned tables claim exist.

    Profiles and subscriptions name every onboarded tenant; applications and
    usage rows are added because a tenant can lose their profile and keep
    thousands of applications (the audit's deleted user kept 2,860). ``job`` is
    deliberately NOT walked: a DISTINCT over 1.48M rows is the statement the
    scoring lane's skip scan exists to avoid, and a tenant with jobs but no
    application, profile or usage row has never been scored for.
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
    """
    try:
        resp = sb.auth.admin.get_user_by_id(uid)
    except Exception as e:
        text = str(e).lower()
        status = getattr(e, "status", None) or getattr(e, "code", None)
        if status in (404, "404") or "not found" in text or "user_not_found" in text:
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

    Returns ``{"auth_users": n, "candidates": k, "purged": [per-user counts],
    "kept": m, "unconfirmed": u, "aborted": reason | None}``. Nothing in it,
    and nothing logged from it, identifies a person.

    Two gates, both required: the bulk listing nominates a candidate, and a
    per-user ``get_user_by_id`` must then answer "not found" for THAT user
    (``_auth_user_gone``). A candidate the listing missed but Auth still knows
    is ``kept``; one Auth could not answer for is ``unconfirmed``. Neither is
    ever deleted.
    """
    from app.config import settings

    out: dict[str, Any] = {"auth_users": 0, "candidates": 0, "purged": [],
                           "kept": 0, "unconfirmed": 0, "aborted": None}

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

    tenants = _tenant_ids()
    candidates = sorted(
        uid for uid in tenants
        if not is_sentinel(uid) and uid.lower() not in auth_ids
    )
    out["candidates"] = len(candidates)
    if len(candidates) > len(auth_ids):
        # Orphans outnumbering live accounts is the signature of a truncated
        # listing, not of a product whose users have mostly left. The daily
        # bound alone would still let it eat five real tenants a day.
        return _abort(f"{len(candidates)} orphan candidates exceed {len(auth_ids)} auth users")

    for uid in candidates[:max(0, int(max_users))]:
        gone = _auth_user_gone(sb, uid)
        if gone is False:
            out["kept"] += 1          # the listing missed a live user — never purge
            continue
        if gone is None:
            out["unconfirmed"] += 1   # Auth could not say — leave it for tomorrow
            continue
        try:
            counts = purge_user_data(uid)
        except Exception as e:
            log.exception("Orphan purge: row deletion failed for one tenant: %s", type(e).__name__)
            continue
        try:
            storage = purge_user_storage(uid, sb)
        except Exception as e:
            storage = {b: False for b in STORAGE_BUCKETS}
            log.exception("Orphan purge: storage cleanup failed for one tenant: %s", type(e).__name__)
        out["purged"].append({
            "rows": sum(counts.values()),
            "tables": {k: v for k, v in sorted(counts.items()) if v},
            "storage": storage,
        })

    if out["kept"]:
        log.warning("Orphan purge: the auth listing missed %d live user(s) that "
                    "get_user_by_id still knows — nothing deleted for them; the "
                    "listing is not complete", out["kept"])
    log.info("Orphan purge: %d auth users, %d orphan candidates, purged %d this run "
             "(%d rows), %d kept, %d unconfirmed",
             out["auth_users"], out["candidates"], len(out["purged"]),
             sum(p["rows"] for p in out["purged"]), out["kept"], out["unconfirmed"])
    return out


def _summary_counts(summary: dict) -> dict:
    """The reconcile result reduced to numbers, for a log line elsewhere."""
    return {
        "auth_users": summary.get("auth_users", 0),
        "candidates": summary.get("candidates", 0),
        "purged": len(summary.get("purged") or []),
        "kept": summary.get("kept", 0),
        "unconfirmed": summary.get("unconfirmed", 0),
        "aborted": summary.get("aborted"),
    }


__all__ = [
    "STORAGE_BUCKETS", "is_sentinel", "purge_user_data", "purge_user_storage",
    "purge_orphaned_accounts",
]
