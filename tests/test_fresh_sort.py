"""'Fresh first' job view — sort by posted date + max-age filter, so newly
posted roles aren't buried under older high-scoring ones ('where are the fresh
jobs?')."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete

from app.db.init_db import get_session
from app.db.models import Job, JobSource


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _seed():
    now = datetime.utcnow()
    with get_session() as session:
        session.exec(delete(Job))
        session.add(Job(user_id=None, source=JobSource.GREENHOUSE, external_id="old",
                        company="OldCo", title="Old High Score", url="https://x/o",
                        description="d", rerank_score=95, blended_score=95,
                        posted_at=now - timedelta(days=25)))
        session.add(Job(user_id=None, source=JobSource.GREENHOUSE, external_id="fresh",
                        company="FreshCo", title="Fresh Low Score", url="https://x/f",
                        description="d", rerank_score=60, blended_score=60,
                        posted_at=now - timedelta(hours=3)))
        session.commit()


def test_default_sort_is_priority(client):
    _seed()
    d = client.get("/api/jobs").json()
    assert d["jobs"][0]["title"] == "Old High Score"


def test_fresh_sort_surfaces_new_and_filters_old(client):
    _seed()
    d = client.get("/api/jobs?sort=fresh&max_age_days=7").json()
    titles = [j["title"] for j in d["jobs"]]
    assert titles[0] == "Fresh Low Score"       # newest first
    assert "Old High Score" not in titles       # 25d old excluded by max_age_days
    assert d["total"] == 1


def test_max_age_filter_counts_correctly(client):
    _seed()
    d = client.get("/api/jobs?max_age_days=30").json()
    assert d["total"] == 2  # both within 30 days
