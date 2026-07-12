"""Hot lane — poll the boards that matter to active users every few minutes so
brand-new postings reach shortlists (and fresh alerts) within minutes, not the
days a full-registry rotation takes.

Architecture — **fetch once, match many** (the cost-efficient model):

    Naive per-user discovery fetches the SAME ATS board once per user, so HTTP
    cost is O(boards × users). The hot lane fetches each board ONCE per cycle
    (shared), then routes each posting only to the users whose target roles it
    matches — HTTP cost is O(boards), independent of user count. Matching and
    per-user job rows stay per-user (scores are personal), but those are cheap,
    local operations; the expensive, rate-limit-bearing part (the network fetch)
    is done a single time.

Board selection is skills-aware at two levels:
  - Board level: we poll active boards that actually produce jobs, least-recently
    polled first, capped at ``hot_lane_max_boards`` — a rotating hot set.
  - Job level: each fetched posting is distributed only to users whose target
    roles appear in its title (cheap keyword routing), so a user's pool only
    grows with jobs relevant to their skills.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import CompanyRegistry, UserProfile

log = logging.getLogger(__name__)


def _active_users() -> list[dict]:
    """Users with a resume + target roles — {user_id, roles:[lowercased]}."""
    from app.api.server import _user_has_resume, _get_target_roles
    out = []
    with get_session() as session:
        profiles = session.exec(select(UserProfile)).all()
    for p in profiles:
        uid = p.user_id
        if not uid or not _user_has_resume(uid):
            continue
        roles = [r.lower() for r in (_get_target_roles(uid) or [])]
        out.append({"user_id": uid, "roles": roles})
    return out


def select_hot_boards(limit: int) -> list[CompanyRegistry]:
    """The rotating hot set: active boards known to produce jobs, least-recently
    polled first so every board is swept over successive cycles."""
    # Split each cycle so BOTH goals are served without starving either:
    #  - half to never-polled boards (last_seen IS NULL) → bootstrap the tens of
    #    thousands of freshly-seeded companies so they start producing jobs fast.
    #    (The old "productive boards first" order left new boards permanently at
    #    the back of a 400-cap queue, so they were never scraped at all.)
    #  - half to productive + stale boards → keep fresh jobs flowing from boards
    #    already known to post. A board scraped once gets last_seen set, so dead
    #    boards fall out after a single attempt.
    half = max(1, limit // 2)
    with get_session() as session:
        never = session.exec(
            select(CompanyRegistry)
            .where(CompanyRegistry.is_active == True,  # noqa: E712
                   CompanyRegistry.last_seen == None)  # noqa: E711
            .limit(half)
        ).all()
        need = limit - len(never)
        # Yield-aware: boards that have EVER produced a new posting sort ahead
        # of merely non-empty ones, both rotating stalest-first — so as yield
        # data accumulates, polling concentrates on boards that actually post.
        productive = session.exec(
            select(CompanyRegistry)
            .where(CompanyRegistry.is_active == True,  # noqa: E712
                   CompanyRegistry.last_seen != None,  # noqa: E711
                   CompanyRegistry.job_count > 0)
            .order_by(CompanyRegistry.last_new_job_at.is_(None),
                      CompanyRegistry.last_seen.asc())
            .limit(need)
        ).all()
        boards = list(never) + list(productive)
        # Backfill if either bucket was thin (e.g. few productive boards yet).
        if len(boards) < limit:
            seen = {b.id for b in boards}
            extra = session.exec(
                select(CompanyRegistry)
                .where(CompanyRegistry.is_active == True)  # noqa: E712
                .order_by(CompanyRegistry.last_seen.asc().nulls_first())
                .limit(limit)
            ).all()
            for b in extra:
                if b.id not in seen and len(boards) < limit:
                    boards.append(b)
                    seen.add(b.id)
    return boards[:limit]


def _title_matches(title: str, roles: list[str]) -> bool:
    """Skills-aware routing: alias- and token-based (see role_title_match), so
    'Senior ML Engineer' reaches a 'Machine Learning Engineer' user. The old
    exact-substring check dropped most relevant fresh postings at fetch time.
    Empty roles → accept (user hasn't narrowed), matching current discovery."""
    from app.discovery.title_filter import role_title_match
    return role_title_match(title, roles)


def run_hot_lane() -> dict:
    """One hot-lane cycle. Returns a small stats dict for logging/telemetry.
    Skips (rather than waits) if a full/fresh discovery pass is already running,
    so it never stacks a second model + job pool in memory (OOM guard)."""
    from app.common.discovery_lock import discovery_guard
    with discovery_guard(blocking=False, label="hot lane") as ran:
        if not ran:
            return {"boards": 0, "users": 0, "reason": "another pass running"}
        return _run_hot_lane_locked()


def _run_hot_lane_locked() -> dict:
    from app.discovery.pipeline import scraper_for, _upsert
    from app.matching.pipeline import run_matching
    from app.strategy.fresh_alerts import dispatch_fresh_alerts

    limit = int(getattr(settings, "hot_lane_max_boards", 400))
    users = _active_users()
    if not users:
        return {"boards": 0, "users": 0, "reason": "no active users"}

    boards = select_hot_boards(limit)
    if not boards:
        return {"boards": 0, "users": len(users), "reason": "no active boards"}

    now = datetime.utcnow()
    fetched_jobs = 0
    matched_jobs = 0    # postings that routed to at least one user's roles
    inserted_jobs = 0   # NEW rows actually written (post-dedupe), all users
    users_touched: set = set()

    # Fetch boards CONCURRENTLY — the fetches are pure I/O, and doing 400 of
    # them sequentially held the global discovery lock for the whole sweep,
    # blocking the fresh lane, the 6h scheduler, and the manual Discover
    # button for many minutes at a time. DB writes stay serial below.
    from concurrent.futures import ThreadPoolExecutor as _Pool

    def _fetch_board(board):
        scraper = scraper_for(board.ats, board.slug, board.career_url)
        if scraper is None:
            return board, None, "unsupported"
        try:
            return board, scraper.fetch(), None  # ONE network call, shared across all users
        except Exception as e:
            return board, None, str(e)

    with _Pool(max_workers=min(12, max(1, len(boards)))) as _pool:
        fetch_results = list(_pool.map(_fetch_board, boards))

    for board, raw, err in fetch_results:
        if raw is None:
            if err != "unsupported":
                log.debug("hot lane fetch failed %s/%s: %s", board.ats, board.slug, err)
                _mark_polled(board.slug, board.ats, job_count=None, ok=False)
            continue
        fetched_jobs += len(raw)
        if not raw:
            _mark_polled(board.slug, board.ats, job_count=0, ok=True)
            continue

        # Distribute each posting only to users whose target roles it matches.
        routed_ids: set = set()
        board_new = 0
        for u in users:
            relevant = [r for r in raw if _title_matches(r.title, u["roles"])]
            if not relevant:
                continue
            routed_ids.update(r.external_id for r in relevant)
            try:
                new = _upsert(relevant, user_id=u["user_id"])
                inserted_jobs += new
                board_new += new
                if new:
                    users_touched.add(u["user_id"])
            except Exception as e:
                log.debug("hot lane upsert failed for %s: %s", u["user_id"], e)
        matched_jobs += len(routed_ids)
        _mark_polled(board.slug, board.ats, job_count=len(raw), ok=True,
                     new_jobs=board_new)

    # Match + alert only for users who actually received new postings.
    alerts = 0
    for uid in users_touched:
        try:
            shortlisted = run_matching(uid) or []
            alerts += dispatch_fresh_alerts(uid, shortlisted)
        except Exception as e:
            log.warning("hot lane match/alert failed for %s: %s", uid, e)

    stats = {
        "boards": len(boards),
        "users": len(users),
        "fetched_jobs": fetched_jobs,
        "matched_jobs": matched_jobs,
        "inserted_jobs": inserted_jobs,
        "users_with_new_jobs": len(users_touched),
        "alerts": alerts,
        "at": now.isoformat(),
    }
    log.info("Hot lane: %s", stats)
    # Heartbeat: record each run so the dashboard can show the hot lane is alive
    # and producing (answers "is the hot lane even running?").
    try:
        import json as _json
        from app.db.models import FunnelEvent
        with get_session() as session:
            session.add(FunnelEvent(
                job_id=None, stage="hot_lane_run", passed=fetched_jobs > 0,
                reason=f"boards={len(boards)} jobs={fetched_jobs} alerts={alerts}",
                metadata_json=_json.dumps(stats),
            ))
            session.commit()
    except Exception as e:
        log.debug("hot lane heartbeat write failed: %s", e)
    return stats


def _mark_polled(slug: str, ats, job_count: Optional[int], ok: bool,
                 new_jobs: int = 0) -> None:
    """Record a poll so board rotation stays fair, failures decay a board, and
    yield (new postings produced) steers future polling toward active boards."""
    try:
        with get_session() as session:
            row = session.exec(
                select(CompanyRegistry).where(
                    CompanyRegistry.slug == slug, CompanyRegistry.ats == ats)
            ).first()
            if not row:
                return
            row.last_seen = datetime.utcnow()
            if ok:
                if job_count is not None:
                    row.job_count = job_count
                row.failure_count = 0
                row.new_jobs_last_poll = new_jobs
                if new_jobs > 0:
                    row.last_new_job_at = datetime.utcnow()
            else:
                row.failure_count = (row.failure_count or 0) + 1
            session.add(row)
            session.commit()
    except Exception:
        pass
