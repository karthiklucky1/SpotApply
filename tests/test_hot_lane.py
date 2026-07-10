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


def test_select_hot_boards_prioritizes_productive_and_stale():
    from app.strategy.hot_lane import select_hot_boards
    old = datetime.utcnow() - timedelta(days=3)
    new = datetime.utcnow()
    with get_session() as session:
        _clean(session)
        _mk_board(session, "productive_stale", job_count=10, last_seen=old)
        _mk_board(session, "empty_stale", job_count=0, last_seen=old)
        _mk_board(session, "productive_fresh", job_count=10, last_seen=new)
    boards = select_hot_boards(limit=10)
    slugs = [b.slug for b in boards]
    # Productive boards rank above empty ones; among productive, stalest first.
    assert slugs.index("productive_stale") < slugs.index("empty_stale")
    assert slugs.index("productive_stale") < slugs.index("productive_fresh")


def test_hot_lane_no_active_users():
    import app.strategy.hot_lane as hl
    with get_session() as session:
        _clean(session)
    out = hl.run_hot_lane()
    assert out["boards"] == 0 and "no active users" in out["reason"]
