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
                        posted_at=now - timedelta(days=25),
                        first_seen=now - timedelta(days=25)))
        session.add(Job(user_id=None, source=JobSource.GREENHOUSE, external_id="fresh",
                        company="FreshCo", title="Fresh Low Score", url="https://x/f",
                        description="d", rerank_score=60, blended_score=60,
                        posted_at=now - timedelta(hours=3),
                        first_seen=now - timedelta(hours=3)))
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


def _seed_undated():
    """A source that publishes no posting date (RIPPLING/RECRUITEE/PINPOINT are
    100% undated in production) alongside one that does."""
    now = datetime.utcnow()
    with get_session() as session:
        session.exec(delete(Job))
        # Discovered seconds ago, but we have NO idea when it was posted.
        session.add(Job(user_id=None, source=JobSource.RIPPLING, external_id="undated",
                        company="UndatedCo", title="Undated Just Crawled",
                        url="https://x/u", description="d",
                        posted_at=None, first_seen=now - timedelta(minutes=1)))
        # Genuinely posted two days ago, by a source that tells us so.
        session.add(Job(user_id=None, source=JobSource.GREENHOUSE, external_id="dated",
                        company="DatedCo", title="Dated Two Days Old",
                        url="https://x/d", description="d",
                        posted_at=now - timedelta(days=2),
                        first_seen=now - timedelta(days=2)))
        session.commit()


def test_undated_job_does_not_outrank_a_dated_one(client):
    """The bug: coalesce(posted_at, first_seen) ranked an undated posting as
    though it were published the instant we crawled it, so 6% of intake held
    100% of the top slots and buried every source that publishes a real date."""
    _seed_undated()
    titles = [j["title"] for j in client.get("/api/jobs?sort=fresh&max_age_days=7").json()["jobs"]]
    assert titles[0] == "Dated Two Days Old"
    assert titles[1] == "Undated Just Crawled"


def test_undated_jobs_are_still_returned(client):
    """Demoted, not dropped — the max-age window still admits them on
    discovery recency."""
    _seed_undated()
    titles = [j["title"] for j in client.get("/api/jobs?sort=fresh&max_age_days=7").json()["jobs"]]
    assert "Undated Just Crawled" in titles


def test_posted_is_flagged_when_it_is_really_a_crawl_time(client):
    """`posted` still carries a value so the UI keeps a date to render, but the
    flag lets it say "first seen" instead of asserting a posting date the
    employer never published."""
    _seed_undated()
    by_title = {j["title"]: j for j in
                client.get("/api/jobs?sort=fresh&max_age_days=7").json()["jobs"]}
    assert by_title["Undated Just Crawled"]["posted_is_estimated"] is True
    assert by_title["Dated Two Days Old"]["posted_is_estimated"] is False


def test_undated_jobs_order_by_most_recently_seen(client):
    now = datetime.utcnow()
    with get_session() as session:
        session.exec(delete(Job))
        for ext, title, mins in (("u1", "Seen Later", 5), ("u2", "Seen Earlier", 600)):
            session.add(Job(user_id=None, source=JobSource.RIPPLING, external_id=ext,
                            company="UndatedCo", title=title, url=f"https://x/{ext}",
                            description="d", posted_at=None,
                            first_seen=now - timedelta(minutes=mins)))
        session.commit()
    titles = [j["title"] for j in client.get("/api/jobs?sort=fresh&max_age_days=7").json()["jobs"]]
    assert titles == ["Seen Later", "Seen Earlier"]


def test_jobs_carry_posted_and_is_new(client):
    """/api/jobs exposes posting age + the 'discovered <24h' flag so the UI can
    render 'New' badges and 'Xh ago' labels per row."""
    _seed()
    d = client.get("/api/jobs?sort=fresh").json()
    by_title = {j["title"]: j for j in d["jobs"]}
    fresh = by_title["Fresh Low Score"]
    old = by_title["Old High Score"]
    assert fresh["posted"] is not None and fresh["is_new"] is True
    assert old["is_new"] is False
