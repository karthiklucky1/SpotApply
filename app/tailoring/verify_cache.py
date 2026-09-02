"""Grounding verdict cache — ground once per unique (evidence, claim, verifier).

A fact-check is a pure function of three things: the evidence it was judged
against, the exact text being judged, and the verifier doing the judging. Ask
the same question again and the honest answer is the one already on file. The
old path did not know that, so a user who re-tailored the same résumé for a
second job at the same company paid twice for identical verdicts, and a rebuild
attempt re-verified every bullet it had just verified.

The key is therefore ``(evidence_id, patch_hash, verifier_version)``:

  evidence_id       sha256 of the normalized master résumé. A new upload, a
                    hand edit, one changed word — different id, no reuse.
  patch_hash        hash of the generated claim TOGETHER with the source span it
                    was matched to. The same sentence judged against different
                    evidence is a different question.
  verifier_version  prompt/logic revision + model name. Change how we ask, or
                    who we ask, and every stored answer is invalidated at once.

Deliberately NO TTL. Everything that can change the verdict is already in the
key, so an expiry can only ever re-buy an answer that is still correct — see the
docstring on ``GroundingVerdict``.

Two layers, because they fail differently: a process-local dict absorbs the
within-request repeats (a rebuild re-checking the same bullets), and the table
survives deploys. Neither is allowed to break a tailor: every DB touch here is
best-effort and a failure degrades to "call the LLM", never to an exception.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from app.config import settings

log = logging.getLogger(__name__)

Key = Tuple[str, str, str]   # (evidence_id, patch_hash, verifier_version)

_LOCAL: Dict[Key, bool] = {}
_LOCAL_MAX = 4096
_LOCK = threading.Lock()


def _enabled() -> bool:
    return bool(getattr(settings, "grounding_cache_enabled", True))


def clear_local() -> None:
    """Drop the in-process layer (tests, and the manual re-verify path)."""
    with _LOCK:
        _LOCAL.clear()


def lookup(keys: Iterable[Key]) -> Dict[Key, bool]:
    """Verdicts already on file for the given keys. Missing keys are absent."""
    keys = list(keys)
    if not keys or not _enabled():
        return {}

    found: Dict[Key, bool] = {}
    with _LOCK:
        for k in keys:
            if k in _LOCAL:
                found[k] = _LOCAL[k]

    remaining = [k for k in keys if k not in found]
    if not remaining:
        return found

    try:
        from sqlmodel import select

        from app.db.init_db import get_session
        from app.db.models import GroundingVerdict

        wanted = set(remaining)
        with get_session() as session:
            rows = session.exec(
                select(GroundingVerdict).where(
                    GroundingVerdict.patch_hash.in_(sorted({k[1] for k in remaining}))
                )
            ).all()
        # patch_hash is the selective column, so the query filters on it alone;
        # the evidence and version halves of the key are matched here rather
        # than widening the WHERE clause into a three-way IN.
        for row in rows:
            key: Key = (row.evidence_id, row.patch_hash, row.verifier_version)
            if key in wanted:
                found[key] = bool(row.supported)
        if found:
            with _LOCK:
                _LOCAL.update({k: v for k, v in found.items()})
    except Exception as e:      # a cold cache is correct, just not free
        log.debug("grounding verdict cache lookup skipped: %s", e)

    return found


def store(entries: List[Tuple[Key, bool]]) -> int:
    """Persist fresh verdicts. Returns how many reached the database."""
    if not entries or not _enabled():
        return 0

    with _LOCK:
        if len(_LOCAL) >= _LOCAL_MAX:
            _LOCAL.clear()
        _LOCAL.update(dict(entries))

    try:
        from datetime import datetime

        from sqlmodel import select

        from app.db.init_db import get_session
        from app.db.models import GroundingVerdict

        written = 0
        with get_session() as session:
            for (evidence_id, patch, version), supported in entries:
                existing = session.exec(
                    select(GroundingVerdict).where(
                        GroundingVerdict.evidence_id == evidence_id,
                        GroundingVerdict.patch_hash == patch,
                        GroundingVerdict.verifier_version == version,
                    )
                ).first()
                if existing is not None:
                    if existing.supported != supported:
                        existing.supported = supported
                        existing.created_at = datetime.utcnow()
                        session.add(existing)
                        written += 1
                    continue
                session.add(GroundingVerdict(
                    evidence_id=evidence_id, patch_hash=patch,
                    verifier_version=version, supported=supported,
                ))
                written += 1
            session.commit()
        return written
    except Exception as e:      # never fail a tailor over an optimisation
        log.debug("grounding verdict cache store skipped: %s", e)
        return 0


def invalidate_evidence(evidence_id: str) -> int:
    """Drop every verdict tied to one master résumé.

    Called when a user replaces their résumé. Strictly speaking this is
    unnecessary — a new résumé has a new ``evidence_id``, so its verdicts are
    simply never looked up — but leaving orphans to accumulate forever is not a
    cache, it is a leak.
    """
    with _LOCK:
        for k in [k for k in _LOCAL if k[0] == evidence_id]:
            _LOCAL.pop(k, None)
    try:
        from sqlmodel import delete

        from app.db.init_db import get_session
        from app.db.models import GroundingVerdict
        with get_session() as session:
            result = session.exec(
                delete(GroundingVerdict).where(GroundingVerdict.evidence_id == evidence_id)
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)
    except Exception as e:
        log.debug("grounding verdict cache invalidation skipped: %s", e)
        return 0


def local_size() -> int:
    with _LOCK:
        return len(_LOCAL)


def cached_lookup_key(evidence_id: str, patch: str, version: str) -> Key:
    return (evidence_id, patch, version)


def get(evidence_id: str, patch: str, version: str) -> Optional[bool]:
    """Single-key convenience wrapper."""
    key = (evidence_id, patch, version)
    return lookup([key]).get(key)
