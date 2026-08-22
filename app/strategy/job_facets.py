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
                # Provenance — what the verdict rests on and how old it is, so
                # the card can show its working instead of a bare badge. Added
                # after the review found the badge asserting a "public USCIS
                # record" for employers that had only matched a curated name
                # list. Readers of this blob must tolerate these keys being
                # absent: rows stamped before this shipped do not carry them.
                "likelihood": getattr(a.likelihood, "value", str(a.likelihood)),
                "source": a.source,
                "as_of": a.as_of,
                "confidence": a.confidence,
                "contradictory": bool(a.contradictory),
                "signals": a.signals,
            })
    except Exception as e:
        log.debug("sponsorship facet failed for %r: %s", title, e)

    return salary, spons_json, cap_exempt


def backfill(user_id: Optional[str], max_rows: int = 250_000,
             max_seconds: float = 600.0, chunk: int = _BATCH,
             pause: float = 0.15, only_missing: bool = True) -> int:
    """Stamp the facets on this user's rows.

    ``only_missing=True`` (the default) fills rows that have never been stamped.
    ``only_missing=False`` RE-STAMPS every row, which is what you want after
    loading new sponsorship data — see the note below.

    Loops until the user's pool is DONE (or a budget stops it), because a single
    bounded pass per boot does not finish: at 5,000 rows/boot a 171,000-row pool
    needs ~35 restarts, and until a row is stamped its card shows no pay or visa
    signal at all. The first review after launch found exactly that — every
    facet NULL for every user.

    ## Why the re-stamp mode exists

    Facets were write-once: `_upsert` stamps them on INSERT only (the
    description-changed branch does not re-stamp), and this function's queue
    predicate was `sponsorship_json IS NULL` while it always wrote at least
    "{}". Between them there was NO path by which an employer's sponsorship
    verdict could ever change on an existing row. Uploading a USCIS dataset
    updated nothing a user could see — only jobs discovered afterwards picked
    it up — which reads exactly like the upload silently failing.

    Gentle on purpose. It reads `description` — the column this exists to stop
    the board from reading — so it works in small chunks with a pause between
    them and a wall-clock budget. The database this runs against is IO-budgeted;
    finishing an hour later is fine, starving the lanes is not.
    """
    import time as _time
    deadline = _time.monotonic() + max(1.0, max_seconds)
    done = 0
    # A re-stamp cannot use "IS NULL" as its own progress marker (every row it
    # touches still matches), so it walks the table by primary key instead.
    cursor = 0
    while done < max_rows and _time.monotonic() < deadline:
        with get_session() as session:
            # TRUNCATED IN SQL, like matcher._candidate_columns(): the salary
            # regex reads at most 8,000 chars anyway, and pulling whole postings
            # for a whole pool is the egress bill this column exists to avoid
            # paying on every render. Doing it once at 8,000 chars keeps the
            # backfill from becoming the same problem.
            q = select(Job.id, Job.title,
                       func.substr(Job.description, 1, 8000).label("desc"),
                       Job.company, Job.url, Job.location).where(Job.user_id == user_id)
            if only_missing:
                q = q.where(Job.sponsorship_json.is_(None))
            else:
                q = q.where(Job.id > cursor).order_by(Job.id)
            rows = list(session.exec(q.limit(chunk)).all())
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
                 "sponsorship_json": spons or "{}",
                 # Written unconditionally, both True AND False. It used to be
                 # set only when True, so a row that was once judged cap-exempt
                 # kept the no-lottery badge forever even when a re-stamp
                 # disagreed — the stale value could never be cleared.
                 "is_cap_exempt": bool(cap_exempt)}
            mappings.append(m)
        cursor = max(int(r[0]) for r in rows)

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
        log.info("job facets: stamped %d job(s) for %s (%s), %s never-stamped left",
                 done, user_id or "local",
                 "missing only" if only_missing else "re-stamp", remaining)
    return done


def restamp_all(max_seconds: float = 600.0, chunk: int = _BATCH,
                pause: float = 0.15) -> int:
    """Re-assess the sponsorship facet for EVERY user's pool.

    Call this after ingesting a sponsor dataset — otherwise the new data only
    reaches jobs discovered from that moment on. Bounded by a wall-clock budget
    because it reads posting text; run it again to continue.
    """
    from sqlmodel import select as _select
    from app.db.models import Job as _Job
    with get_session() as session:
        owners = [r for (r,) in session.exec(
            _select(_Job.user_id).distinct()).all()]
    total = 0
    import time as _time
    deadline = _time.monotonic() + max(1.0, max_seconds)
    for uid in owners:
        if _time.monotonic() >= deadline:
            log.info("job facets re-stamp: budget spent, %d row(s) done", total)
            break
        total += backfill(uid, max_seconds=max(1.0, deadline - _time.monotonic()),
                          chunk=chunk, pause=pause, only_missing=False)
    return total
