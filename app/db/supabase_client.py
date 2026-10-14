"""Supabase client helpers.

Provides two clients:
- anon_client()        — uses SUPABASE_ANON_KEY, respects Row Level Security
- service_client()     — uses SUPABASE_SERVICE_ROLE_KEY, bypasses RLS (server-only)

Both are lazy singletons. If Supabase is not configured (no SUPABASE_URL),
calling these raises RuntimeError so callers know to fall back to SQLite.

Usage:
    from app.db.supabase_client import service_client
    sb = service_client()
    sb.storage.from_("resume").upload(...)
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from functools import lru_cache
from typing import Optional

log = logging.getLogger(__name__)

# Short-lived positive cache for verify_jwt. The SAME bearer token is presented
# on nearly every request (the dashboard polls /api/notifications every 60s,
# each page load fires ~6 authed fetches), and verify_jwt makes a blocking
# round-trip to Supabase Auth each time. Without caching that is both slow and
# fragile: a brief Auth hiccup 401s every logged-in user at once (the auth_failure
# storm seen in prod). Only SUCCESSFUL validations are cached — never failures —
# so a revoked token stops working within the TTL and a transient error never
# locks out a valid session.
_JWT_CACHE: dict[str, tuple[float, dict]] = {}   # token -> (expires_monotonic, payload)
# A verified token is trusted until it EXPIRES (capped), not for 60 s. The old
# minute-long TTL meant one Auth round-trip (100-500 ms, blocking a thread) per
# user per minute — and a page load fires ~20 authed fetches at once, so after
# any idle minute all of them missed TOGETHER and each made its own call
# (2026-10-10 latency review). Supabase access tokens are stateless JWTs that
# stay valid until `exp` anyway; `forget_user` still evicts a deleted account
# at once, and a revoked-but-unexpired token is bounded by the cap below.
_JWT_CACHE_TTL = 60.0                 # floor, and the TTL when `exp` is unreadable
_JWT_CACHE_MAX_TTL = 900.0            # cap: at most 15 min between re-verifications
_JWT_CACHE_MAX = 4096
_JWT_INFLIGHT: dict[str, threading.Lock] = {}   # single-flight per token
_JWT_INFLIGHT_GUARD = threading.Lock()


def _jwt_exp(token: str) -> float | None:
    """The token's `exp` (epoch seconds) read WITHOUT verifying — only used to
    bound how long a verification we already made is trusted."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        exp = payload.get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


def _jwt_cache_ttl(token: str) -> float:
    exp = _jwt_exp(token)
    if exp is None:
        return _JWT_CACHE_TTL
    remaining = exp - time.time()
    return max(_JWT_CACHE_TTL, min(_JWT_CACHE_MAX_TTL, remaining))


def _jwt_cache_put(token: str, payload: dict, now: float) -> None:
    if len(_JWT_CACHE) >= _JWT_CACHE_MAX:
        # Evict expired entries first; if still full, reset (rare, bounded).
        for k in [k for k, (exp, _) in _JWT_CACHE.items() if exp <= now]:
            _JWT_CACHE.pop(k, None)
        if len(_JWT_CACHE) >= _JWT_CACHE_MAX:
            _JWT_CACHE.clear()
    _JWT_CACHE[token] = (now + _jwt_cache_ttl(token), payload)


def _inflight_lock(token: str) -> threading.Lock:
    with _JWT_INFLIGHT_GUARD:
        if len(_JWT_INFLIGHT) > 512:
            _JWT_INFLIGHT.clear()
        lock = _JWT_INFLIGHT.get(token)
        if lock is None:
            lock = _JWT_INFLIGHT[token] = threading.Lock()
        return lock


def forget_user(uid: str) -> int:
    """Drop every cached verification for ``uid``. Called when the account is
    deleted: without it the deleted user's open tab stays authenticated for up
    to ``_JWT_CACHE_TTL`` — long enough for a poll to act as a live tenant."""
    stale = [t for t, (_exp, payload) in list(_JWT_CACHE.items())
             if (payload or {}).get("sub") == uid]
    for t in stale:
        _JWT_CACHE.pop(t, None)
    return len(stale)

try:
    from supabase import Client, create_client
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False


@lru_cache(maxsize=1)
def anon_client() -> "Client":
    """Public client — safe for use with user JWT tokens (respects RLS)."""
    from app.config import settings
    if not _SUPABASE_AVAILABLE:
        raise RuntimeError("supabase package not installed. Run: pip install supabase")
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env")
    return create_client(settings.supabase_url, settings.supabase_anon_key)


@lru_cache(maxsize=1)
def service_client() -> "Client":
    """Service role client — server-side only, bypasses Row Level Security."""
    from app.config import settings
    if not _SUPABASE_AVAILABLE:
        raise RuntimeError("supabase package not installed. Run: pip install supabase")
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def verify_jwt(token: str) -> Optional[dict]:
    """Verify a Supabase JWT by calling the Supabase auth API (get_user).

    This is the only reliable approach — avoids needing the raw JWT secret.
    Returns the user payload dict or None on failure.
    """
    from app.config import settings
    if not settings.supabase_url:
        return None
    now = time.monotonic()
    hit = _JWT_CACHE.get(token)
    if hit and hit[0] > now:
        return hit[1]
    # Single-flight: concurrent misses on the SAME token (a page load's parallel
    # fetches) wait for one verification instead of each making an Auth call.
    with _inflight_lock(token):
        hit = _JWT_CACHE.get(token)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            sb = service_client()
            result = sb.auth.get_user(token)
            if result and result.user:
                u = result.user
                payload = {"sub": u.id, "email": getattr(u, "email", None),
                           "email_confirmed": bool(getattr(u, "email_confirmed_at", None)),
                           "phone_confirmed": bool(getattr(u, "phone_confirmed_at", None))}
                _jwt_cache_put(token, payload, time.monotonic())
                return payload
        except Exception as e:
            log.debug("JWT verification failed: %s", e)
    return None


def get_user_id_from_token(token: str) -> Optional[str]:
    """Extract Supabase user UUID from a JWT bearer token."""
    payload = verify_jwt(token)
    if payload:
        return payload.get("sub")
    return None
