"""Per-user LLM spend ledger — attribution, not accounting.

Every paid LLM call site records here so the owner can finally answer
"which user / day / feature is the money going to?" (the old cost dashboard
was a dead stub and per-user attribution was discarded after shortlisting).

Costs are flat per-call ESTIMATES: a Tier-2 final is a Haiku call with a large
cached résumé prefix, a Tier-1 prescore is a mini-model call, a tailor is a
multi-call generation pass. Good enough for trends and outlier users; not an
invoice. Recording must never break the caller — everything is wrapped."""
from __future__ import annotations

import logging
from datetime import date

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import LlmSpend

log = logging.getLogger(__name__)

# Flat per-call estimates (USD). Tune as models/prices change.
EST_COST_PER_CALL = {
    "score_final": 0.010,     # Claude final (cached prefix keeps it cheap)
    "score_prescore": 0.001,  # Tier-1 mini-model bulk score
    "score_local": 0.0,       # local fallback — free
    "tailor": 0.05,           # résumé + cover letter generation pass
}


def record_llm_spend(user_id: str | None, kind: str, calls: int = 1) -> None:
    """Upsert today's (user, kind) row. Safe to call from any lane/thread."""
    if calls <= 0:
        return
    uid = user_id or "local"
    est = EST_COST_PER_CALL.get(kind, 0.0) * calls
    try:
        with get_session() as session:
            row = session.exec(
                select(LlmSpend).where(
                    LlmSpend.user_id == uid,
                    LlmSpend.day == date.today(),
                    LlmSpend.kind == kind,
                )
            ).first()
            if row:
                row.calls += calls
                row.est_cost_usd += est
            else:
                row = LlmSpend(user_id=uid, day=date.today(), kind=kind,
                               calls=calls, est_cost_usd=est)
            session.add(row)
            session.commit()
    except Exception as e:  # never let bookkeeping break the money path
        log.debug("llm spend record failed (%s/%s): %s", uid, kind, e)


def spend_summary(days: int = 14) -> dict:
    """Owner overview: per-day totals + top users, over the last N days."""
    from datetime import timedelta
    since = date.today() - timedelta(days=days - 1)
    with get_session() as session:
        rows = session.exec(select(LlmSpend).where(LlmSpend.day >= since)).all()
    by_day: dict = {}
    by_user: dict = {}
    for r in rows:
        d = r.day.isoformat()
        by_day.setdefault(d, {"calls": 0, "est_cost_usd": 0.0})
        by_day[d]["calls"] += r.calls
        by_day[d]["est_cost_usd"] += r.est_cost_usd
        u = by_user.setdefault(r.user_id, {"calls": 0, "est_cost_usd": 0.0, "kinds": {}})
        u["calls"] += r.calls
        u["est_cost_usd"] += r.est_cost_usd
        k = u["kinds"].setdefault(r.kind, {"calls": 0, "est_cost_usd": 0.0})
        k["calls"] += r.calls
        k["est_cost_usd"] += r.est_cost_usd
    top_users = sorted(
        ({"user_id": u, **v} for u, v in by_user.items()),
        key=lambda x: -x["est_cost_usd"],
    )[:25]
    total = round(sum(v["est_cost_usd"] for v in by_day.values()), 4)
    return {
        "days": days,
        "total_est_cost_usd": total,
        "by_day": dict(sorted(by_day.items())),
        "top_users": top_users,
        "note": "Estimated from flat per-call rates (analytics/spend.py); "
                "covers scoring lane, pulse fast path, and tailoring.",
    }
