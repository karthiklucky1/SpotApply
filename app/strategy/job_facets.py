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
             pause: float = 0.15) -> int:
    """Stamp the facets on this user's rows that have never been stamped.

    To RE-stamp rows that already carry a verdict (what you want after loading
    new sponsorship data), use ``restamp_facets`` — it walks the primary key
    globally instead of filtering by user, for the indexing reason documented
    there.

    Loops until the user's pool is DONE (or a budget stops it), because a single
    bounded pass per boot does not finish: at 5,000 rows/boot a 171,000-row pool
    needs ~35 restarts, and until a row is stamped its card shows no pay or visa
    signal at all. The first review after launch found exactly that — every
    facet NULL for every user.

    Gentle on purpose. It reads `description` — the column this exists to stop
    the board from reading — so it works in small chunks with a pause between
    them and a wall-clock budget. The database this runs against is IO-budgeted;
    finishing an hour later is fine, starving the lanes is not.

    The `sponsorship_json IS NULL` predicate is what bounds each chunk: rows
    leave the queue as they are stamped, so `LIMIT 500` finds its 500 quickly
    against `ix_job_user_id` and the loop terminates.
    """
    import time as _time
    deadline = _time.monotonic() + max(1.0, max_seconds)
    done = 0
    while done < max_rows and _time.monotonic() < deadline:
        with get_session() as session:
            # TRUNCATED IN SQL, like matcher._candidate_columns(): the salary
            # regex reads at most 8,000 chars anyway, and pulling whole postings
            # for a whole pool is the egress bill this column exists to avoid
            # paying on every render. Doing it once at 8,000 chars keeps the
            # backfill from becoming the same problem.
            q = (select(Job.id, Job.title,
                        func.substr(Job.description, 1, 8000).label("desc"),
                        Job.company, Job.url, Job.location)
                 .where(Job.user_id == user_id, Job.sponsorship_json.is_(None)))
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
        log.info("job facets: stamped %d never-stamped job(s) for %s, %s left",
                 done, user_id or "local", remaining)
    return done


def restamp_facets(start_id: int = 0, max_rows: int = 250_000,
                   max_seconds: float = 600.0, chunk: int = _BATCH,
                   pause: float = 0.15) -> Tuple[int, int]:
    """Re-assess the facets on EVERY row, walking the primary key.

    Returns ``(rows_stamped, next_start_id)``. Feed ``next_start_id`` back in to
    continue; 0 means the table is done.

    ## Why this is global and pages on `id` alone

    The first version of this re-stamp was per-user and paged with
    ``WHERE user_id = :u AND id > :cursor ORDER BY id LIMIT 500``. That query
    has no index to serve it. `job` carries a dozen indexes and every one of
    them LEADS on user_id — `(user_id)`, `(user_id, is_closed)`,
    `(user_id, first_seen)`, `(user_id, discovered_at)`, `(user_id, company,
    title)`, both freshness indexes — and none is `(user_id, id)`. So the
    planner either takes `ix_job_user_id` and sorts six figures of rows to
    satisfy the ORDER BY, or takes `job_pkey` and heap-checks user_id row by
    row. On production it hit Supabase's 2-minute `statement_timeout` on the
    very FIRST chunk, every time — the step was never runnable.

    Paging on the primary key with no user predicate is a plain `job_pkey`
    range scan: each chunk reads exactly `chunk` index entries from the cursor
    and stops. Bounded regardless of table size, no new index required, and no
    sort. It also covers the shared pool, whose rows are adopted later and
    would otherwise carry the stale verdict into every future user copy.

    Closed rows are re-stamped too, deliberately. Filtering them would put an
    unindexed predicate back inside the LIMIT and reintroduce exactly the
    scan-until-N-found shape this exists to avoid; refreshing a dead row's
    facet is harmless and costs one CPU pass.
    """
    import time as _time
    deadline = _time.monotonic() + max(1.0, max_seconds)
    done = 0
    cursor = int(start_id or 0)
    while done < max_rows and _time.monotonic() < deadline:
        with get_session() as session:
            rows = list(session.exec(
                select(Job.id, Job.title,
                       func.substr(Job.description, 1, 8000).label("desc"),
                       Job.company, Job.url, Job.location)
                .where(Job.id > cursor)
                .order_by(Job.id)
                .limit(chunk)
            ).all())
        if not rows:
            cursor = 0                              # reached the end of the table
            break

        mappings = []
        for jid, title, desc, company, url, location in rows:
            salary, spons, cap_exempt = compute(title or "", desc or "", company or "",
                                                url or "", location or "")
            mappings.append({"id": int(jid),
                             "salary_text": salary,
                             "sponsorship_json": spons or "{}",
                             "is_cap_exempt": bool(cap_exempt)})
        next_cursor = max(int(r[0]) for r in rows)

        try:
            with get_session() as session:
                session.bulk_update_mappings(Job, mappings)
                session.commit()
            done += len(mappings)
            cursor = next_cursor
        except Exception as e:
            # Do NOT advance the cursor past a chunk that failed to write.
            log.warning("job facet re-stamp chunk failed at id>%d (stopping): %s",
                        cursor, e)
            break
        if len(rows) < chunk:
            cursor = 0                              # last partial chunk
            break
        if pause:
            _time.sleep(pause)

    log.info("job facets re-stamp: %d row(s) updated, %s",
             done, f"resume from id>{cursor}" if cursor else "table complete")
    return done, cursor


def restamp_all(max_seconds: float = 600.0, chunk: int = _BATCH,
                pause: float = 0.15, start_id: int = 0) -> int:
    """Re-assess the sponsorship facet across the whole table.

    Call this after ingesting a sponsor dataset — otherwise the new data only
    reaches jobs discovered from that moment on. Bounded by a wall-clock budget
    because it reads posting text; re-run to continue (see `restamp_facets` for
    the resume cursor).
    """
    done, _next = restamp_facets(start_id=start_id, max_seconds=max_seconds,
                                 chunk=chunk, pause=pause)
    return done
