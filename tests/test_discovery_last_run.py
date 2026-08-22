"""/api/discovery/last-run must reflect the run that actually fed the pool.

Scheduled discovery is one GLOBAL pass writing to the shared pool; a user's own
DiscoveryRun rows exist only for manual runs. The old user-only filter served
the user's last manual run forever — production showed run #716, status=error,
from six weeks earlier while shared runs completed the same day, making the
dashboard look dead. And shared runs never shortlist (that is per-user lane
work after adoption), so their structural 0 must not render as a count.
"""
from __future__ import annotations

import pytest
from sqlmodel import delete

from app.db.init_db import get_session
from app.db.models import DiscoveryRun
from app.discovery.pipeline import SHARED_POOL_USER


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    with get_session() as session:
        session.exec(delete(DiscoveryRun))
        session.commit()
    yield
    with get_session() as session:
        session.exec(delete(DiscoveryRun))
        session.commit()


def _add_run(user_id, status="done", shortlisted=0):
    from datetime import datetime
    with get_session() as session:
        row = DiscoveryRun(user_id=user_id, status=status,
                           started_at=datetime.utcnow(),
                           finished_at=datetime.utcnow(),
                           total_fetched=10, total_inserted=5,
                           total_shortlisted=shortlisted, source_counts="{}")
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_local_mode_still_sees_newest_run(client):
    _add_run(None, status="error")
    newest = _add_run(None, status="done")
    d = client.get("/api/discovery/last-run").json()
    assert d["run"]["id"] == newest


def test_shared_run_shortlisted_is_null_not_zero(client):
    """total_shortlisted=0 on a shared run means 'not applicable', not
    'discovery found nothing good' — the API must say so."""
    _add_run(SHARED_POOL_USER, status="done", shortlisted=0)
    d = client.get("/api/discovery/last-run").json()
    run = d["run"]
    assert run["scope"] == "shared"
    assert run["total_shortlisted"] is None


def test_own_run_keeps_its_shortlist_count(client):
    _add_run(None, status="done", shortlisted=7)
    d = client.get("/api/discovery/last-run").json()
    assert d["run"]["total_shortlisted"] == 7


def test_freshness_stats_carries_the_canonical_counter_and_honest_pulse(client):
    """Four different 'new in 24h' numbers wore the same label in production
    (user pool 2,611 / pulse ticks 12,936 / all rows 19,071 / by discovered_at
    18,921). The payload must carry ONE canonical pipeline number — distinct
    shared-pool postings — plus the labelled per-user figure, and the pulse
    block must expose whether the hourly-floor promise is actually holding."""
    from datetime import datetime
    from app.db.models import Job, JobSource
    from app.discovery.pipeline import SHARED_POOL_USER
    with get_session() as session:
        session.exec(delete(Job))
        # 2 distinct shared postings + 1 per-user adopted copy of one of them.
        for i in range(2):
            session.add(Job(user_id=SHARED_POOL_USER, source=JobSource.GREENHOUSE,
                            external_id=f"s{i}", company="Co", title="T",
                            url=f"https://x/{i}", description="d",
                            first_seen=datetime.utcnow()))
        session.add(Job(user_id=None, source=JobSource.GREENHOUSE,
                        external_id="s0", company="Co", title="T",
                        url="https://x/0", description="d",
                        first_seen=datetime.utcnow()))
        session.commit()
    d = client.get("/api/freshness-stats").json()
    assert d["shared_pool_new_24h"] == 2      # copies never inflate it
    assert d["your_pool_new_24h"] == d["jobs_discovered_24h"]
    if d["pulse"].get("enabled"):
        assert "overdue_pct" in d["pulse"] or "live_boards" not in d["pulse"]
    with get_session() as session:
        session.exec(delete(Job))
        session.commit()
