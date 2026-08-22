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

from sqlalchemy import func
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


def backfill(user_id: Optional[str], max_rows: int = 250_000,
             max_seconds: float = 600.0, chunk: int = _BATCH,
             pause: float = 0.15) -> int:
    """Stamp the facets on this user's rows that have never had them.

    Loops until the user's pool is DONE (or a budget stops it), because a single
    bounded pass per boot does not finish: at 5,000 rows/boot a 171,000-row pool
    needs ~35 restarts, and until a row is stamped its card shows no pay or visa
    signal at all. The first review after launch found exactly that — every
    facet NULL for every user.

    Gentle on purpose. It reads `description` — the column this exists to stop
    the board from reading — so it works in small chunks with a pause between
    them and a wall-clock budget. The database this runs against is IO-budgeted;
    finishing an hour later is fine, starving the lanes is not.
    """
    import time as _time
    deadline = _time.monotonic() + max(1.0, max_seconds)
    done = 0
    while done < max_rows and _time.monotonic() < deadline:
        with get_session() as session:
            rows = list(session.exec(
                # TRUNCATED IN SQL, like matcher._candidate_columns(): the
                # salary regex reads at most 8,000 chars anyway, and pulling
                # whole postings for a whole pool is the egress bill this column
                # exists to avoid paying on every render. Doing it once at
                # 8,000 chars keeps the backfill from becoming the same problem.
                select(Job.id, Job.title,
                       func.substr(Job.description, 1, 8000).label("desc"),
                       Job.company, Job.url, Job.location)
                .where(Job.user_id == user_id, Job.sponsorship_json.is_(None))
                .limit(chunk)
            ).all())
        if not rows:
            break                                   # pool is fully stamped

        mappings = []
        for jid, title, desc, company, url, location in rows:
            salary, spons, cap_exempt = compute(title or "", desc or "", company or "",
                                                url or "", location or "")
            m = {"id": int(jid),
                 "salary_text": salary,
                 # Always write SOMETHING for sponsorship, even "{}": this column
                 # is the loop's own progress marker, and a row left NULL because
                 # its assessment came back empty would be re-read forever.
                 "sponsorship_json": spons or "{}"}
            if cap_exempt:
                m["is_cap_exempt"] = True
            mappings.append(m)

        try:
            with get_session() as session:
                session.bulk_update_mappings(Job, mappings)
                session.commit()
            done += len(mappings)
        except Exception as e:
            log.warning("job facet backfill chunk failed for %s (stopping): %s",
                        user_id or "local", e)
            break
        if len(rows) < chunk:
            break                                   # last partial chunk
        if pause:
            _time.sleep(pause)

    if done:
        with get_session() as session:
            remaining = session.exec(
                select(func.count(Job.id)).where(
                    Job.user_id == user_id, Job.sponsorship_json.is_(None))
            ).first()
        remaining = remaining[0] if isinstance(remaining, tuple) else (remaining or 0)
        log.info("job facets: stamped %d job(s) for %s, %s left",
                 done, user_id or "local", remaining)
    return done
