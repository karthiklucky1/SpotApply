"""Hot lane: fetch-once/distribute-many, skills-aware routing, board rotation."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import CompanyRegistry, Job, JobSource, UserProfile
from app.discovery.base import RawJob


def _clean(session):
    session.exec(delete(Job))
    session.exec(delete(CompanyRegistry))
    session.exec(delete(UserProfile))
    session.commit()


def _mk_board(session, slug, ats=JobSource.GREENHOUSE, job_count=5, last_seen=None):
    session.add(CompanyRegistry(slug=slug, ats=ats, is_active=True,
                                job_count=job_count, source="test",
                                last_seen=last_seen))
    session.commit()


def test_hot_lane_fetch_once_distribute_by_role(monkeypatch):
    import app.strategy.hot_lane as hl

    with get_session() as session:
        _clean(session)
        _mk_board(session, "acme")
        session.add(UserProfile(user_id="u_backend", target_roles="backend engineer"))
        session.add(UserProfile(user_id="u_design", target_roles="product designer"))
        session.commit()

    # Two users, both "active"; board serves one backend + one design job.
    monkeypatch.setattr(hl, "_active_users", lambda: [
        {"user_id": "u_backend", "roles": ["backend engineer"]},
        {"user_id": "u_design", "roles": ["product designer"]},
    ])

    fetch_calls = {"n": 0}

    class FakeScraper:
        def fetch(self):
            fetch_calls["n"] += 1
            return [
                RawJob(source="greenhouse", external_id="1", company="Acme",
                       title="Senior Backend Engineer", location="Remote", remote=True,
                       url="https://boards.greenhouse.io/acme/jobs/1", description="Kafka",
                       posted_at=datetime.utcnow()),
                RawJob(source="greenhouse", external_id="2", company="Acme",
                       title="Product Designer", location="NYC", remote=False,
                       url="https://boards.greenhouse.io/acme/jobs/2", description="Figma",
                       posted_at=datetime.utcnow()),
            ]

    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, career_url=None: FakeScraper())
    # Isolate matching/alerts — the hot-lane routing is what we're testing here.
    monkeypatch.setattr("app.matching.pipeline.run_matching", lambda uid: [])
    monkeypatch.setattr("app.strategy.fresh_alerts.dispatch_fresh_alerts", lambda uid, ids: 0)

    stats = hl.run_hot_lane()

    # The board was fetched exactly ONCE despite two users (the cost win).
    assert fetch_calls["n"] == 1, "board must be fetched once, not per-user"
    assert stats["boards"] == 1 and stats["users"] == 2

    with get_session() as session:
        backend = session.exec(select(Job).where(Job.user_id == "u_backend")).all()
        design = session.exec(select(Job).where(Job.user_id == "u_design")).all()
    # Skills-aware routing: each user got only their matching title.
    assert [j.title for j in backend] == ["Senior Backend Engineer"]
    assert [j.title for j in design] == ["Product Designer"]


def test_select_hot_boards_bootstraps_new_and_keeps_productive():
    from app.strategy.hot_lane import select_hot_boards
    old = datetime.utcnow() - timedelta(days=3)
    new = datetime.utcnow()
    with get_session() as session:
        _clean(session)
        _mk_board(session, "never_scraped", job_count=0, last_seen=None)   # brand new
        _mk_board(session, "productive_stale", job_count=10, last_seen=old)
        _mk_board(session, "productive_fresh", job_count=10, last_seen=new)
        _mk_board(session, "dead_scraped", job_count=0, last_seen=old)      # scraped, empty
    boards = select_hot_boards(limit=10)
    slugs = [b.slug for b in boards]
    # Never-scraped board is included (was starved by the old ordering).
    assert "never_scraped" in slugs
    # Productive boards included; among them, stalest first.
    assert "productive_stale" in slugs and "productive_fresh" in slugs
    assert slugs.index("productive_stale") < slugs.index("productive_fresh")


def test_select_hot_boards_caps_bootstrap_and_prioritizes_productive():
    """Productive boards get the majority of each cycle; never-polled boards get
    only the small bootstrap slice (hot_lane_bootstrap_frac, default 20%), so
    tens of thousands of dead seeded slugs can't 404-storm the budget and starve
    boards that actually post."""
    from app.strategy.hot_lane import select_hot_boards
    with get_session() as session:
        _clean(session)
        for i in range(20):
            _mk_board(session, f"new_{i}", job_count=0, last_seen=None)
        for i in range(20):
            _mk_board(session, f"prod_{i}", job_count=5,
                      last_seen=datetime.utcnow() - timedelta(days=1))
    boards = select_hot_boards(limit=10)
    new_count = sum(1 for b in boards if b.slug.startswith("new_"))
    prod_count = sum(1 for b in boards if b.slug.startswith("prod_"))
    assert new_count == 2, f"bootstrap slice should be ~20% of 10, got {new_count}"
    assert prod_count == 8, f"productive boards should get the rest, got {prod_count}"


def test_select_hot_boards_yielders_win_main_budget_over_staler_quiet_boards():
    """Proven yielders (produced a NEW posting before) claim the main
    (non-bootstrap) budget ahead of productive-but-never-yielded boards, even
    when those quiet boards are staler — so polling concentrates where fresh
    jobs actually appear."""
    from app.strategy.hot_lane import select_hot_boards
    now = datetime.utcnow()
    with get_session() as session:
        _clean(session)
        for i in range(6):
            session.add(CompanyRegistry(slug=f"yield_{i}", ats=JobSource.GREENHOUSE,
                                        is_active=True, job_count=10, source="test",
                                        last_seen=now - timedelta(days=1),
                                        last_new_job_at=now - timedelta(hours=3)))
        for i in range(6):
            session.add(CompanyRegistry(slug=f"quiet_{i}", ats=JobSource.GREENHOUSE,
                                        is_active=True, job_count=10, source="test",
                                        last_seen=now - timedelta(days=5)))  # staler
        session.commit()
    boards = select_hot_boards(limit=5)   # bootstrap=1, main=4
    yield_count = sum(1 for b in boards if b.slug.startswith("yield_"))
    assert yield_count == 4, f"yielders should claim the main budget, got {[b.slug for b in boards]}"


def test_role_title_match_aliases():
    """The routing gate must catch title variants of the user's roles — the old
    exact-substring check dropped 'Senior ML Engineer' for a 'Machine Learning
    Engineer' user, which starved the hot lane of fresh jobs."""
    from app.discovery.title_filter import role_title_match as m

    ml_roles = ["Machine Learning Engineer", "AI Engineer"]
    for title in ("Senior ML Engineer", "MLOps Engineer", "Machine Learning Engineer II",
                  "AI Research Engineer", "GenAI Engineer", "Staff Engineer, Deep Learning",
                  "Artificial Intelligence Engineer"):
        assert m(title, ml_roles), title
    for title in ("Product Designer", "Sales Executive", "Mechanical Engineer",
                  "Chair Assembly Technician"):   # 'chair' must not match 'ai'
        assert not m(title, ml_roles), title

    # LLM role variants
    assert m("Member of Technical Staff - LLMs", ["LLM Engineer"])
    assert m("Generative AI Engineer", ["LLM Engineer"])

    # Non-tech user: distinctive tokens route, generic words don't
    design = ["Product Designer"]
    assert m("Senior Product Designer", design)
    assert not m("Senior Backend Engineer", design)

    # Empty roles → accept everything (unchanged behavior)
    assert m("Anything At All", [])
    assert m("Anything At All", None)


def test_hot_lane_routes_title_variants(monkeypatch):
    """End-to-end: an ML user receives 'Senior ML Engineer' from the hot lane."""
    import app.strategy.hot_lane as hl

    with get_session() as session:
        _clean(session)
        _mk_board(session, "acme")
        session.add(UserProfile(user_id="u_ml", target_roles="Machine Learning Engineer"))
        session.commit()

    monkeypatch.setattr(hl, "_active_users", lambda: [
        {"user_id": "u_ml", "roles": ["machine learning engineer"]},
    ])

    class FakeScraper:
        def fetch(self):
            return [
                RawJob(source="greenhouse", external_id="10", company="Acme",
                       title="Senior ML Engineer", location="Remote", remote=True,
                       url="https://boards.greenhouse.io/acme/jobs/10", description="PyTorch",
                       posted_at=datetime.utcnow()),
                RawJob(source="greenhouse", external_id="11", company="Acme",
                       title="Account Executive", location="NYC", remote=False,
                       url="https://boards.greenhouse.io/acme/jobs/11", description="Sales",
                       posted_at=datetime.utcnow()),
            ]

    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, career_url=None: FakeScraper())
    monkeypatch.setattr("app.matching.pipeline.run_matching", lambda uid: [])
    monkeypatch.setattr("app.strategy.fresh_alerts.dispatch_fresh_alerts", lambda uid, ids: 0)

    hl.run_hot_lane()

    with get_session() as session:
        jobs = session.exec(select(Job).where(Job.user_id == "u_ml")).all()
    assert [j.title for j in jobs] == ["Senior ML Engineer"]


def test_select_hot_boards_prefers_yielding_boards():
    """Among productive boards, one that recently produced a NEW posting beats
    a staler board that never yielded — polling concentrates on active boards."""
    from app.strategy.hot_lane import select_hot_boards
    now = datetime.utcnow()
    with get_session() as session:
        _clean(session)
        session.add(CompanyRegistry(slug="never_yield", ats=JobSource.GREENHOUSE,
                                    is_active=True, job_count=10, source="test",
                                    last_seen=now - timedelta(days=5)))
        session.add(CompanyRegistry(slug="yielder", ats=JobSource.GREENHOUSE,
                                    is_active=True, job_count=10, source="test",
                                    last_seen=now - timedelta(days=3),
                                    last_new_job_at=now - timedelta(hours=2)))
        session.commit()
    boards = select_hot_boards(limit=1)
    assert [b.slug for b in boards] == ["yielder"]


def test_hot_lane_inserts_even_when_lock_busy(monkeypatch):
    """A multi-hour full-discovery run must NOT starve the hot lane: fetching
    and inserting run without the lock; only matching is deferred."""
    import app.strategy.hot_lane as hl
    from app.common.discovery_lock import _LOCK

    with get_session() as session:
        _clean(session)
        _mk_board(session, "acme")
        session.add(UserProfile(user_id="u_ml", target_roles="Machine Learning Engineer"))
        session.commit()

    monkeypatch.setattr(hl, "_active_users", lambda: [
        {"user_id": "u_ml", "roles": ["machine learning engineer"]},
    ])

    class FakeScraper:
        def fetch(self):
            return [RawJob(source="greenhouse", external_id="20", company="Acme",
                           title="ML Engineer", location="Remote", remote=True,
                           url="https://boards.greenhouse.io/acme/jobs/20",
                           description="PyTorch", posted_at=datetime.utcnow())]

    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, career_url=None: FakeScraper())

    def _explode(uid):
        raise AssertionError("matching must not run while the lock is busy")
    monkeypatch.setattr("app.matching.pipeline.run_matching", _explode)

    assert _LOCK.acquire(blocking=False), "test setup: lock must be free"
    try:
        stats = hl.run_hot_lane()
    finally:
        _LOCK.release()

    # Jobs were fetched and INSERTED despite the busy lock; matching deferred.
    assert stats["inserted_jobs"] == 1
    assert "skipped" in stats["matching"]
    with get_session() as session:
        jobs = session.exec(select(Job).where(Job.user_id == "u_ml")).all()
    assert [j.title for j in jobs] == ["ML Engineer"]


def test_dead_board_deactivated_on_404():
    """A 404 means the company's board is gone — it must be retired immediately
    so future cycles stop burning budget on it. A transient error only counts
    toward the consecutive-failure threshold."""
    from app.strategy.hot_lane import _mark_polled
    with get_session() as session:
        _clean(session)
        _mk_board(session, "goneco")
        _mk_board(session, "flakyco")

    _mark_polled("goneco", JobSource.GREENHOUSE, job_count=None, ok=False,
                 error="Client error '404 Not Found' for url 'https://x'")
    _mark_polled("flakyco", JobSource.GREENHOUSE, job_count=None, ok=False,
                 error="timeout")

    with get_session() as session:
        gone = session.exec(select(CompanyRegistry)
                            .where(CompanyRegistry.slug == "goneco")).first()
        flaky = session.exec(select(CompanyRegistry)
                             .where(CompanyRegistry.slug == "flakyco")).first()
    assert gone.is_active is False and "404" in (gone.inactive_reason or "")
    assert flaky.is_active is True and flaky.failure_count == 1

    # Repeated transient failures eventually retire the board too…
    from app.discovery.pipeline import BOARD_DEACTIVATE_AFTER_FAILURES
    for _ in range(BOARD_DEACTIVATE_AFTER_FAILURES - 1):
        _mark_polled("flakyco", JobSource.GREENHOUSE, job_count=None, ok=False,
                     error="timeout")
    with get_session() as session:
        flaky = session.exec(select(CompanyRegistry)
                             .where(CompanyRegistry.slug == "flakyco")).first()
    assert flaky.is_active is False

    # …and a success resets the failure counter for healthy boards.
    with get_session() as session:
        _clean(session)
        _mk_board(session, "healthyco")
    _mark_polled("healthyco", JobSource.GREENHOUSE, job_count=None, ok=False, error="timeout")
    _mark_polled("healthyco", JobSource.GREENHOUSE, job_count=3, ok=True)
    with get_session() as session:
        healthy = session.exec(select(CompanyRegistry)
                               .where(CompanyRegistry.slug == "healthyco")).first()
    assert healthy.is_active is True and healthy.failure_count == 0


def test_hot_lane_writes_shared_pool(monkeypatch):
    """Scrape once, serve many: EVERY fetched posting lands in the shared pool
    (for future adoption), while each user's pool only gets role matches."""
    import app.strategy.hot_lane as hl
    from app.discovery.pipeline import SHARED_POOL_USER

    with get_session() as session:
        _clean(session)
        _mk_board(session, "acme")
        session.add(UserProfile(user_id="u_ml", target_roles="Machine Learning Engineer"))
        session.commit()

    monkeypatch.setattr(hl, "_active_users", lambda: [
        {"user_id": "u_ml", "roles": ["machine learning engineer"]},
    ])

    class FakeScraper:
        def fetch(self):
            return [
                RawJob(source="greenhouse", external_id="30", company="Acme",
                       title="Senior ML Engineer", location="Remote", remote=True,
                       url="https://boards.greenhouse.io/acme/jobs/30",
                       description="PyTorch", posted_at=datetime.utcnow()),
                RawJob(source="greenhouse", external_id="31", company="Acme",
                       title="Backend Engineer", location="Remote", remote=True,
                       url="https://boards.greenhouse.io/acme/jobs/31",
                       description="Go", posted_at=datetime.utcnow()),
            ]

    monkeypatch.setattr("app.discovery.pipeline.scraper_for",
                        lambda ats, slug, career_url=None: FakeScraper())
    monkeypatch.setattr("app.matching.pipeline.run_matching", lambda uid: [])
    monkeypatch.setattr("app.strategy.fresh_alerts.dispatch_fresh_alerts", lambda uid, ids: 0)

    stats = hl.run_hot_lane()
    assert stats["shared_inserted"] == 2

    with get_session() as session:
        shared = session.exec(select(Job).where(Job.user_id == SHARED_POOL_USER)).all()
        mine = session.exec(select(Job).where(Job.user_id == "u_ml")).all()
    assert sorted(j.title for j in shared) == ["Backend Engineer", "Senior ML Engineer"]
    assert [j.title for j in mine] == ["Senior ML Engineer"]  # role-routed only


def test_unsupported_ats_board_is_retired(monkeypatch):
    """A registered board whose ATS has no scraper (e.g. iCIMS/Jobvite) can never
    yield jobs. If left active with last_seen=NULL it re-clogs the never-polled
    bootstrap slice forever, so the hot lane must retire it on contact."""
    import app.strategy.hot_lane as hl

    with get_session() as session:
        _clean(session)
        # ICIMS has no scraper in scraper_for → _fetch_board returns "unsupported".
        _mk_board(session, "noscraper", ats=JobSource.ICIMS, job_count=0, last_seen=None)
        session.add(UserProfile(user_id="u_ml", target_roles="Machine Learning Engineer"))
        session.commit()

    monkeypatch.setattr(hl, "_active_users", lambda: [
        {"user_id": "u_ml", "roles": ["machine learning engineer"]},
    ])

    hl.run_hot_lane()

    with get_session() as session:
        row = session.exec(select(CompanyRegistry)
                           .where(CompanyRegistry.slug == "noscraper")).first()
    assert row.is_active is False
    assert row.last_seen is not None            # no longer "never polled"
    assert "unsupported" in (row.inactive_reason or "")


def test_hot_lane_no_active_users():
    import app.strategy.hot_lane as hl
    with get_session() as session:
        _clean(session)
    out = hl.run_hot_lane()
    assert out["boards"] == 0 and "no active users" in out["reason"]


def test_hot_lane_records_heartbeat_even_when_cycle_crashes(monkeypatch):
    """Regression: a cycle that raised mid-run used to write NO hot_lane_run
    event, so the dashboard showed a permanent 'idle' while the lane was firing
    and crashing every interval. run_hot_lane() must now always leave a
    heartbeat carrying the error reason, and must not re-raise."""
    import app.strategy.hot_lane as hl
    from app.db.models import FunnelEvent

    with get_session() as session:
        _clean(session)
        session.exec(delete(FunnelEvent))
        session.commit()

    # Force a crash deep in the cycle (after the wrapper, before _finish_cycle).
    def _boom():
        raise RuntimeError("boards query exploded")
    monkeypatch.setattr(hl, "_active_users", lambda: [{"user_id": "u", "roles": ["x"]}])
    monkeypatch.setattr(hl, "select_hot_boards", lambda limit: _boom())

    stats = hl.run_hot_lane()  # must NOT raise

    assert "error" in stats["reason"] and "boards query exploded" in stats["reason"]
    with get_session() as session:
        events = session.exec(
            select(FunnelEvent).where(FunnelEvent.stage == "hot_lane_run")
        ).all()
    assert len(events) == 1, "a heartbeat must be written even on crash"
    assert "error" in (events[0].reason or "")
