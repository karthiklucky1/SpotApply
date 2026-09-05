"""One place that answers "what does this user's plan allow?".

Three lanes enforce the daily shortlist ceiling (matching/pipeline,
strategy/pulse_lane, strategy/scoring_lane) and the API enforces the tailor
ceiling. Each was reaching for `settings.daily_shortlist_limit` — a single flat
number for every plan — so a Free user and a Pro user got the same board.

The lazy import is load-bearing: server.py imports the lanes, so a top-level
`from app.api.server import _get_user_plan` would be circular. By the time any
lane cycle runs, server is fully loaded.

FAIL OPEN, deliberately. A billing hiccup or a Supabase blip must never silently
shrink someone's feed to zero — an unknown plan falls back to the global
`settings.daily_shortlist_limit`, which is a ceiling well above any plan's.
Failing closed here would look exactly like the product being broken.
"""
from __future__ import annotations

import logging
log = logging.getLogger(__name__)


def plan_limit(user_id: str | None, key: str, default: int | None) -> int | None:
    """`PLAN_LIMITS[plan][key]` for this user, or ``default`` if unknowable.

    ``default`` is returned for the anonymous case, the SQLite dev user, and any
    failure resolving the plan — never a zero, never a guess at a paid tier.
    """
    if not user_id or user_id == "local":
        return default
    try:
        from app.api.server import _get_user_plan
        from app.db.models import PLAN_LIMITS
        value = PLAN_LIMITS[_get_user_plan(user_id)].get(key)
    except Exception as e:                                  # pragma: no cover
        log.debug("plan lookup failed for %s (%s) — using default for %r",
                  user_id, e, key)
        return default
    return default if value is None else value


def shortlist_daily_limit(user_id: str | None) -> int:
    """How many jobs may reach this user's board today.

    A CEILING on delivery, not a target. Nothing in the pipeline tries to reach
    it: a day whose pool holds six jobs clearing the shortlist bar delivers six.
    """
    from app.config import settings
    fallback = int(settings.daily_shortlist_limit)
    value = plan_limit(user_id, "shortlist_daily", fallback)
    return max(0, int(value if value is not None else fallback))
