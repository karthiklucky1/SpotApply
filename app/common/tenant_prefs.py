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


def geo_prefs(profile, user_id: str | None = None):
    """The saved location preferences the eligibility decision reads
    (app/common/eligibility.py) — country through the SAME resolution the
    scoring prompt uses, plus remote/relocation/home area. One helper so every
    door builds the identical object."""
    from app.common.eligibility import GeoPrefs
    from app.common.geo import norm_country
    return GeoPrefs(
        country=norm_country(effective_country(profile, user_id)),
        remote_ok=effective_remote_ok(profile),
        open_to_relocation=bool(getattr(profile, "open_to_relocation", False)),
        home_location=(getattr(profile, "location", "") or "").strip(),
    )


def geo_prefs_for_user(user_id: str | None):
    """Load the profile columns the decision needs for ONE user — projected,
    no profile is created as a side effect. None/"local" is the local user.

    Returns None when the profile could NOT be read. A user with no profile
    row gets the same defaults every door gives them; a user whose profile
    read failed gets no decision at all — deciding their rows against the
    platform default country would stamp INELIGIBLE (a destructive, queue-
    draining write) from a country they never chose.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserProfile

    uid = user_id or "local"
    try:
        with get_session() as session:
            row = session.exec(
                select(UserProfile.preferred_country, UserProfile.remote_ok,
                       UserProfile.open_to_relocation, UserProfile.location)
                .where(UserProfile.user_id == uid)
            ).first()
    except Exception as e:
        log.debug("geo prefs unavailable for %s: %s", uid, e)
        return None

    class _P:
        pass

    p = _P()
    if row is not None:
        p.preferred_country, p.remote_ok, p.open_to_relocation, p.location = row
    return geo_prefs(p if row is not None else None, uid)


def reset_state() -> None:
    """Forget which users we have logged the country fallback for. Tests only."""
    _warned.clear()
