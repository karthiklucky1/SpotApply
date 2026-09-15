"""The intake preferences every door into a user's pool must agree on.

There are four doors — full discovery, the fresh lane, adoption, and the pulse
lane's per-user route — and before 2026-09-12 they each resolved the user's
country differently, or not at all. The expensive version of that bug is not
"a filter is missing"; it is a filter DISAGREEING with the scorer:

    profile.preferred_country == ""   ->  adoption applied no country gate
    app/matching/reranker.py          ->  told Claude "wants jobs in United States"
                                          and scored foreign postings 0-30

So the pipeline admitted postings for free and then paid Tier-1 (and sometimes
Claude) to reject them. 73% of the queue drained below the Tier-1 gate.

This module is the one place that answers "which country is this user shopping
in", and both the gate and the prompt read it.
"""
from __future__ import annotations

import logging

from app.config import settings

log = logging.getLogger(__name__)

_warned: set = set()


def effective_country(profile, user_id: str | None = None) -> str:
    """The country to filter AND to prompt with. '' only when the default is ''.

    ``profile`` is a UserProfile (or anything with ``preferred_country``); None
    is allowed and falls back to the default.
    """
    raw = (getattr(profile, "preferred_country", "") or "").strip()
    if raw:
        return raw
    fallback = (settings.default_intake_country or "").strip()
    if fallback and user_id and user_id not in _warned:
        _warned.add(user_id)
        log.info("Intake country: user %s has no preferred_country — assuming %r "
                 "(the same assumption the scoring prompt makes)", user_id, fallback)
    return fallback


def effective_remote_ok(profile) -> bool:
    """Whether fully-remote roles open to the user's own country are kept."""
    return bool(getattr(profile, "remote_ok", True))


def reset_state() -> None:
    """Forget which users we have logged the country fallback for. Tests only."""
    _warned.clear()
