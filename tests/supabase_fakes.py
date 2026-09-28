"""In-memory stand-ins for the two Supabase surfaces account deletion touches.

They follow the REAL contracts the deletion relies on, because the fakes they
replaced did not: the old storage fake returned the same two files for any
prefix, ignored pagination and never emptied — so a one-level listing that
left every tailored document behind passed — and the old auth fake raised a
bare 404, which is exactly what a gateway's error page looks like.

Not a test module (no ``test_`` prefix); imported by the deletion tests.
"""
from __future__ import annotations

import uuid

try:                                    # the real SDK error, when installed
    from supabase_auth.errors import AuthApiError
except Exception:                       # pragma: no cover - same attributes
    class AuthApiError(Exception):      # type: ignore[no-redef]
        def __init__(self, message, status, code):
            super().__init__(message)
            self.message, self.status, self.code = message, status, code


def user_not_found() -> Exception:
    """What supabase_auth raises for ``get_user_by_id`` of a deleted user: it
    sends X-Supabase-Api-Version 2024-01-01, so the code is always set."""
    return AuthApiError("User not found", 404, "user_not_found")


class FakeBucket:
    """storage3 semantics.

    ``list(path, options)`` returns the DIRECT children of the folder ``path``
    (a folder is an entry whose ``id`` is None), sorted by name and paged by
    ``limit``/``offset`` (default limit 100). ``remove(paths)`` deletes exact
    object names and ignores the rest. ``refuse`` holds paths the API answers
    OK for but keeps — the case a deletion must not report as clean.
    """

    def __init__(self, objects=(), *, fail_list=False, fail_remove=False, refuse=()):
        self.objects: set[str] = set(objects)
        self.fail_list = fail_list
        self.fail_remove = fail_remove
        self.refuse = set(refuse)
        self.list_calls = 0
        self.removed: list[str] = []

    def list(self, path=None, options=None):
        self.list_calls += 1
        if self.fail_list:
            raise RuntimeError("storage listing exploded")
        opts = options or {}
        limit = int(opts.get("limit", 100))
        offset = int(opts.get("offset", 0))
        prefix = (path or "").strip("/")
        base = f"{prefix}/" if prefix else ""
        children: dict[str, str | None] = {}
        for obj in self.objects:
            if not obj.startswith(base):
                continue
            head, sep, _ = obj[len(base):].partition("/")
            if sep:
                children.setdefault(head, None)          # a folder
            else:
                children[head] = obj                     # a file
        entries = []
        for name in sorted(children):
            full = children[name]
            if full is None:
                entries.append({"name": name, "id": None, "metadata": None})
            else:
                entries.append({"name": name, "metadata": {"size": 1},
                                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, full))})
        return entries[offset:offset + limit]

    def remove(self, paths):
        if self.fail_remove:
            raise RuntimeError("storage remove refused")
        out = []
        for p in paths:
            self.removed.append(p)
            if p in self.objects and p not in self.refuse:
                self.objects.discard(p)
                out.append({"name": p})
        return out


class FakeStorage:
    """``sb.storage`` — ``from_(bucket)`` for the two per-user buckets."""

    def __init__(self, **buckets: FakeBucket):
        self.buckets = {"resume": FakeBucket(), "avatars": FakeBucket()}
        self.buckets.update(buckets)

    def from_(self, name):
        return self.buckets.setdefault(name, FakeBucket())
