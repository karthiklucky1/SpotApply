"""Keyword-search sources must cap board count so a large registry (~56K boards
after seeding) doesn't blow the pipeline's 45s per-source timeout — the bug that
made Greenhouse/Lever keyword search show ⚠️ after the registry was seeded."""
from __future__ import annotations

import asyncio

from sqlmodel import delete

from app.config import settings
from app.db.init_db import get_session
from app.db.models import CompanyRegistry, JobSource


class _FakeClient:
    def __init__(self, counter, **kw):
        self.counter = counter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        self.counter["n"] += 1

        class R:
            status_code = 200

            def json(self):
                return {"jobs": []}
        return R()


def _seed(ats, n):
    with get_session() as session:
        session.exec(delete(CompanyRegistry))
        for i in range(n):
            session.add(CompanyRegistry(slug=f"{ats.value}{i}", ats=ats,
                                        is_active=True, job_count=i % 7, source="test"))
        session.commit()


def test_greenhouse_keyword_search_capped(monkeypatch):
    import app.discovery.sources.greenhouse_search as m
    _seed(JobSource.GREENHOUSE, 2000)
    counter = {"n": 0}
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: _FakeClient(counter, **kw))
    asyncio.run(m.GreenhouseKeywordSource(keywords=["engineer"]).fetch_jobs())
    assert counter["n"] <= settings.keyword_search_max_slugs, counter["n"]
    # And it actually used most of the budget (not near-zero from a bug)
    assert counter["n"] >= settings.keyword_search_max_slugs - 60


def test_lever_keyword_search_capped(monkeypatch):
    import app.discovery.sources.lever_search as m
    _seed(JobSource.LEVER, 2000)
    counter = {"n": 0}
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: _FakeClient(counter, **kw))
    asyncio.run(m.LeverKeywordSource(keywords=["engineer"]).fetch_jobs())
    assert counter["n"] <= settings.keyword_search_max_slugs, counter["n"]
