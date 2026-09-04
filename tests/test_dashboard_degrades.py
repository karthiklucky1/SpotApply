"""A slow database must cost a panel, never a page — and never a sign-in.

2026-09-04, production. The open-jobs count over the 764k-row `job` table
stopped finishing inside Postgres's own 120s statement timeout while discovery
was writing:

    GET /api/pipeline/live   500  123730ms   QueryCanceled: statement timeout
    GET /dashboard           499  103946ms   client gave up

`/dashboard` is where every sign-in lands, and a browser keeps painting the
previous document until the next one arrives — so an unanswerable dashboard
presented to the user as an auth callback frozen forever on "Signing you in…".

These tests pin the fail-safe: every dashboard read is bounded, and when one
expires the rest of the page still renders and says so.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api import server
from app.config import settings


def _timeout() -> OperationalError:
    """What psycopg2 raises when the server cancels a statement."""
    return OperationalError(
        "SELECT count(job.id) FROM job", {},
        Exception("canceling statement due to statement timeout"))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_get_user_id", lambda request: "local")
    return TestClient(server.app)


# ── the bound itself ─────────────────────────────────────────────────────────

class _FakeSession:
    def __init__(self):
        self.rollbacks = 0

    def get_bind(self):
        class _B:
            dialect = type("D", (), {"name": "sqlite"})()
        return _B()

    def rollback(self):
        self.rollbacks += 1


def test_a_timed_out_read_returns_the_default_and_flags_the_page():
    s = _FakeSession()
    reads = server._BoundedReads(s, 5)
    assert reads.get(["fallback"], lambda: ["real"]) == ["real"]
    assert reads.degraded is False

    def _boom():
        raise _timeout()

    assert reads.get(["fallback"], _boom) == ["fallback"]
    assert reads.degraded is True
    assert s.rollbacks == 1, (
        "a timed-out statement aborts its transaction — without a rollback "
        "every LATER panel fails too, which is the whole page again"
    )


def test_later_panels_still_load_after_one_times_out():
    s = _FakeSession()
    reads = server._BoundedReads(s, 5)

    def _boom():
        raise _timeout()

    reads.get(None, _boom)
    assert reads.get([], lambda: ["loaded"]) == ["loaded"], (
        "the panels after a degraded one must still render"
    )


def test_a_zero_budget_disables_the_bound():
    """The pre-2026-09 behaviour stays reachable by config, and when it is
    chosen the error propagates rather than being silently swallowed."""
    reads = server._BoundedReads(_FakeSession(), 0)
    with pytest.raises(OperationalError):
        reads.get("fallback", lambda: (_ for _ in ()).throw(_timeout()))


def test_the_budget_is_armed_as_a_transaction_local_setting(monkeypatch):
    """SET LOCAL, never SET: these connections are pooled, and a session-level
    timeout would follow the connection into unrelated requests — including the
    background lanes, whose queries are legitimately long."""
    issued = []

    class _PgSession(_FakeSession):
        def get_bind(self):
            class _B:
                dialect = type("D", (), {"name": "postgresql"})()
            return _B()

        def execute(self, stmt):
            issued.append(str(stmt))

    server._BoundedReads(_PgSession(), 5)
    assert issued and "SET LOCAL statement_timeout = 5000" in issued[0], issued


# ── the routes ───────────────────────────────────────────────────────────────

def test_the_dashboard_still_renders_when_a_panel_times_out(client, monkeypatch):
    """The page a sign-in lands on must answer even when the database will
    not — this is the request that hung for 104 seconds."""
    monkeypatch.setattr(server, "_scalar", lambda v: (_ for _ in ()).throw(_timeout()))
    r = client.get("/dashboard")
    assert r.status_code == 200, r.text[:300]
    assert "could not load in time" in r.text, (
        "the board degraded silently — the user sees an empty board and "
        "concludes they have no matches"
    )


def test_pipeline_live_answers_instead_of_500ing(client, monkeypatch):
    """This endpoint returned 500 after 123.7s. Now it answers, and marks the
    count it could not produce as unavailable rather than as zero."""
    monkeypatch.setattr(server, "_scalar", lambda v: (_ for _ in ()).throw(_timeout()))
    r = client.get("/api/pipeline/live")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["degraded"] is True
    assert body["counts"]["pool"] is None, (
        "a timed-out count must not render as 0 — that tells the user their "
        "job pool emptied"
    )


def test_a_healthy_dashboard_is_not_marked_degraded(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "could not load in time" not in r.text
    live = client.get("/api/pipeline/live").json()
    assert live["degraded"] is False
    assert live["counts"]["pool"] is not None


def test_the_budget_default_is_short_enough_to_beat_a_user_leaving():
    """Postgres's own statement_timeout was 120s, which is far past the point
    a person abandons a sign-in (production: gave up at 48s)."""
    assert 0 < settings.dashboard_query_timeout_seconds <= 10, (
        f"dashboard_query_timeout_seconds={settings.dashboard_query_timeout_seconds} "
        f"is not a fail-safe a waiting user would ever benefit from"
    )
