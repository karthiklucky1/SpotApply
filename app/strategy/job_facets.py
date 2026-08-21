"""Card-face facets, derived once from the posting instead of on every render.

The Kanban board shows a salary chip and a sponsorship badge on every card. Both
were computed during the render — `_salary_of` regexes up to 8,000 characters of
the JD, `_sponsorship_of` assesses its full text — which is why the board query
had to load `Job.description` for up to ~260 rows on the page every user opens
first. The posting does not change between renders, so the answer belongs on the
row:

    write time  → `_upsert` stamps both from the text it already has in hand
    LLM parse   → the insights route refreshes `salary_text` when Claude reads a
                  better number out of the posting than the regex found
    old rows    → `backfill()` at boot, same shape as `on_role`

With these two columns the JD is read only when someone opens a drawer.
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Tuple

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job

log = logging.getLogger(__name__)

_BATCH = 500


def compute(title: str, description: str, company: str = "", url: str = "",
            location: str = "") -> Tuple[Optional[str], Optional[str], bool]:
    """(salary_text, sponsorship_json, cap_exempt) for one posting.

    Best-effort: a facet that cannot be derived is None, and the card simply
    renders without that chip — exactly what happened before when the regex
    found nothing.
    """
    salary = None
    spons_json = None
    cap_exempt = False
    desc = description or ""

    try:
        from app.api.server import _salary_from_text
        salary = _salary_from_text(desc)
    except Exception as e:
        log.debug("salary facet failed for %r: %s", title, e)

    try:
        from app.intelligence.sponsorship import assess
        a = assess(company=company or "", description=desc,
                   url=url or "", location=location or "")
        if a is not None:
            cap_exempt = bool(a.cap_exempt)
            spons_json = json.dumps({
                "cap_exempt": cap_exempt,
                "tone": a.tone,
                "badge": a.badge,
                "reason": a.reason,
                "refuses": bool(a.explicitly_refuses),
            })
    except Exception as e:
        log.debug("sponsorship facet failed for %r: %s", title, e)

    return salary, spons_json, cap_exempt


def backfill(user_id: Optional[str], limit: int = 5000) -> int:
    """Stamp the facets on this user's rows that have never had them.

    Reads `description` — the very column this exists to stop the board from
    reading — so it is bounded, batched, and runs off the request path. It is a
    one-time cost per row against a per-render cost for every card.
    """
    with get_session() as session:
        rows = list(session.exec(
            select(Job.id, Job.title, Job.description, Job.company, Job.url, Job.location)
            .where(Job.user_id == user_id, Job.sponsorship_json.is_(None))
            .limit(limit)
        ).all())

    mappings = []
    for jid, title, desc, company, url, location in rows:
        salary, spons, cap_exempt = compute(title or "", desc or "", company or "",
                                            url or "", location or "")
        if spons is None and salary is None:
            continue
        m = {"id": int(jid), "salary_text": salary, "sponsorship_json": spons}
        if cap_exempt:
            m["is_cap_exempt"] = True
        mappings.append(m)

    done = 0
    for start in range(0, len(mappings), _BATCH):
        batch = mappings[start:start + _BATCH]
        try:
            with get_session() as session:
                session.bulk_update_mappings(Job, batch)
                session.commit()
            done += len(batch)
        except Exception as e:
            log.warning("job facet backfill batch %d failed (non-fatal): %s", start, e)
    if done:
        log.info("job facets: stamped %d job(s) for %s", done, user_id or "local")
    return done
