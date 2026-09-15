"""The pre-delivery liveness gate.

Two properties this file exists to defend, in order of importance:

  1. A posting is NEVER blocked because an endpoint refused to answer. 429,
     403, 401, timeouts, connection errors and blocked hosts all deliver.
  2. A posting IS blocked when the evidence is conclusive: 404 on the exact
     permalink, 410, or explicit removal wording.

Plus the operational guarantees: one check per posting no matter how many
users receive it, one dead candidate never stops the others, and the daily
target is a maximum that is never forced.

Rows are prefixed `dlgtest-` and cleaned up by that prefix.
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import (
    Application, Job, JobLiveness, JobLivenessState, JobSource,
)
from app.discovery import liveness as lv
from app.strategy import delivery_gate as gate

_PREFIX = "dlgtest-"


@pytest.fixture(autouse=True)
def _clean():
    gate.metrics_snapshot(reset=True)
    yield
    with get_session() as s:
        s.exec(delete(JobLiveness).where(JobLiveness.external_id.like(f"{_PREFIX}%")))
        # select(<one column>) yields scalars, not 1-tuples.
        jids = list(s.exec(
            select(Job.id).where(Job.external_id.like(f"{_PREFIX}%"))).all())
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(Job).where(Job.id.in_(jids)))
        s.commit()
    gate.metrics_snapshot(reset=True)


def _mk_job(ext: str, user_id: str, *, source=JobSource.GREENHOUSE,
            url: str = "https://job-boards.greenhouse.io/acme/jobs/1") -> int:
    with get_session() as s:
        j = Job(source=source, external_id=ext, company="Acme", title="Engineer",
                url=url, user_id=user_id, rerank_score=90.0)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _patch_fetch(monkeypatch, *, status=None, final_url="", body="", error=None):
    def _fake(url, timeout):
        return status, (final_url or url), body, error
    monkeypatch.setattr(gate, "_fetch", _fake)


# ══════════════════════════════════════════════════════════════════════════
# 1. A refusal NEVER kills a live job
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status,error,expected", [
    (429, None, JobLivenessState.RATE_LIMITED.value),
    (403, None, JobLivenessState.BLOCKED.value),
    (401, None, JobLivenessState.BLOCKED.value),
    (500, None, JobLivenessState.UNKNOWN.value),
    (503, None, JobLivenessState.UNKNOWN.value),
    (None, "ConnectTimeout", JobLivenessState.UNKNOWN.value),
    (None, "ReadTimeout", JobLivenessState.UNKNOWN.value),
    (None, "ConnectError", JobLivenessState.UNKNOWN.value),
    (None, "blocked_host", JobLivenessState.UNKNOWN.value),
])
def test_a_refusal_is_never_a_death(monkeypatch, status, error, expected):
    _patch_fetch(monkeypatch, status=status, error=error)
    ext = f"{_PREFIX}refuse-{status}-{error}"
    assert gate.verified_dead(JobSource.GREENHOUSE, ext,
                              "https://job-boards.greenhouse.io/acme/jobs/1") is False
    state, _ = lv.load_states([("greenhouse", ext)]).get(("greenhouse", ext), (None, None))
    assert state == expected


def test_a_429_after_a_live_verdict_keeps_the_live_verdict(monkeypatch):
    """Requirement 8: conclusive evidence outlives inconclusive attempts."""
    ext = f"{_PREFIX}live-then-429"
    lv.record("greenhouse", ext, JobLivenessState.LIVE.value, reason="http_200")
    _patch_fetch(monkeypatch, status=429)
    # Force a re-check by making the stored evidence stale.
    with get_session() as s:
        row = s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).one()
        row.checked_at = None
        s.add(row)
        s.commit()
    assert gate.verified_dead(JobSource.GREENHOUSE, ext, "https://x/jobs/1") is False
    with get_session() as s:
        row = s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).one()
    assert row.state == JobLivenessState.LIVE.value
    assert row.inconclusive_streak == 1


def test_an_unverifiable_source_is_delivered_not_blocked(monkeypatch):
    """An aggregator URL is a redirect, so a 404 on it proves nothing."""
    called = {"n": 0}

    def _boom(url, timeout):
        called["n"] += 1
        return 404, url, "", None

    monkeypatch.setattr(gate, "_fetch", _boom)
    ext = f"{_PREFIX}aggregator"
    assert gate.verified_dead(JobSource.INDEED, ext, "https://indeed.example/rc/clk?x=1") is False
    assert called["n"] == 0                      # no request is even made
    assert gate.metrics_snapshot()["unverifiable_source"] == 1


# ══════════════════════════════════════════════════════════════════════════
# 2. Conclusive evidence DOES block delivery
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status,body,expected", [
    (404, "", JobLivenessState.REMOVED.value),
    (410, "", JobLivenessState.EXPIRED.value),
    (200, "<h1>This position is no longer active</h1>", JobLivenessState.REMOVED.value),
    (200, "Sorry, this position has been filled.", JobLivenessState.REMOVED.value),
])
def test_conclusive_evidence_blocks_delivery(monkeypatch, status, body, expected):
    _patch_fetch(monkeypatch, status=status, body=body)
    ext = f"{_PREFIX}dead-{status}-{len(body)}"
    assert gate.verified_dead(JobSource.GREENHOUSE, ext,
                              "https://job-boards.greenhouse.io/acme/jobs/1") is True
    state, _ = lv.load_states([("greenhouse", ext)]).get(("greenhouse", ext), (None, None))
    assert state == expected
    assert gate.metrics_snapshot()["blocked_before_delivery"] == 1


def test_a_live_posting_is_delivered(monkeypatch):
    _patch_fetch(monkeypatch, status=200, body="Senior Engineer. Apply now.")
    ext = f"{_PREFIX}alive"
    assert gate.verified_dead(JobSource.GREENHOUSE, ext, "https://x/jobs/1") is False
    assert gate.metrics_snapshot()[f"state:{JobLivenessState.LIVE.value}"] == 1


# ══════════════════════════════════════════════════════════════════════════
# 3. Fresh vs stale evidence (needs_check)
# ══════════════════════════════════════════════════════════════════════════

def test_fresh_conclusive_evidence_avoids_the_network(monkeypatch):
    calls = {"n": 0}

    def _count(url, timeout):
        calls["n"] += 1
        return 200, url, "", None

    monkeypatch.setattr(gate, "_fetch", _count)
    ext = f"{_PREFIX}fresh"
    gate.verify_for_delivery(JobSource.GREENHOUSE, ext, "https://x/jobs/1")
    assert calls["n"] == 1
    # Second call inside the recheck window must not touch the network.
    state, how = gate.verify_for_delivery(JobSource.GREENHOUSE, ext, "https://x/jobs/1")
    assert calls["n"] == 1
    assert how == "cached_fresh"
    assert gate.metrics_snapshot()["avoided_check_fresh_evidence"] == 1


def test_stale_evidence_triggers_one_recheck(monkeypatch):
    from datetime import datetime, timedelta
    calls = {"n": 0}

    def _count(url, timeout):
        calls["n"] += 1
        return 200, url, "", None

    monkeypatch.setattr(gate, "_fetch", _count)
    ext = f"{_PREFIX}stale"
    lv.record("greenhouse", ext, JobLivenessState.LIVE.value, reason="http_200")
    with get_session() as s:
        row = s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).one()
        row.checked_at = datetime.utcnow() - timedelta(days=30)
        s.add(row)
        s.commit()
    _state, how = gate.verify_for_delivery(JobSource.GREENHOUSE, ext, "https://x/jobs/1")
    assert how == "checked" and calls["n"] == 1


def test_a_posting_already_known_dead_needs_no_request(monkeypatch):
    def _boom(url, timeout):
        raise AssertionError("must not re-check a posting already known dead")

    monkeypatch.setattr(gate, "_fetch", _boom)
    ext = f"{_PREFIX}known-dead"
    lv.record("greenhouse", ext, JobLivenessState.REMOVED.value, reason="http_404")
    assert gate.verified_dead(JobSource.GREENHOUSE, ext, "https://x/jobs/1") is True
    assert gate.metrics_snapshot()["avoided_check_already_dead"] == 1


def test_the_gate_can_be_switched_off(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "liveness_gate_enabled", False)

    def _boom(url, timeout):
        raise AssertionError("no request when the gate is disabled")

    monkeypatch.setattr(gate, "_fetch", _boom)
    assert gate.verified_dead(JobSource.GREENHOUSE, f"{_PREFIX}off", "https://x/jobs/1") is False


# ══════════════════════════════════════════════════════════════════════════
# 4. One posting, ten users, ONE check
# ══════════════════════════════════════════════════════════════════════════

def test_ten_users_receiving_one_posting_cause_one_check(monkeypatch):
    ext = f"{_PREFIX}shared-check"
    calls = {"n": 0}
    started = threading.Barrier(10)

    def _slow(url, timeout):
        calls["n"] += 1
        time.sleep(0.25)          # hold the flight open so the others pile up
        return 200, url, "", None

    monkeypatch.setattr(gate, "_fetch", _slow)
    results = []

    def _worker():
        started.wait(timeout=5)
        results.append(gate.verify_for_delivery(JobSource.GREENHOUSE, ext, "https://x/jobs/1"))

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert calls["n"] == 1, f"expected a single flight, got {calls['n']}"
    assert len(results) == 10
    hows = [h for _s, h in results]
    assert hows.count("checked") == 1
    assert gate.metrics_snapshot()["checks_deduplicated"] == 9
    with get_session() as s:
        rows = s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).all()
    assert len(rows) == 1                      # keyed by (source, external_id)


def test_the_single_flight_releases_even_when_the_fetch_raises(monkeypatch):
    ext = f"{_PREFIX}flight-release"

    def _raise(url, timeout):
        raise RuntimeError("boom")

    monkeypatch.setattr(gate, "_fetch", _raise)
    # The gate catches it, and the in-flight entry must not leak.
    state, how = gate.verify_for_delivery(JobSource.GREENHOUSE, ext, "https://x/jobs/1")
    assert how == "unverifiable"
    assert gate._inflight == {}


# ══════════════════════════════════════════════════════════════════════════
# 5. The in-session backstop in slate.place()
# ══════════════════════════════════════════════════════════════════════════

def test_place_refuses_a_posting_known_dead():
    from app.strategy.slate import place
    ext = f"{_PREFIX}place-dead"
    uid = f"{_PREFIX}u1"
    jid = _mk_job(ext, uid)
    lv.record("greenhouse", ext, JobLivenessState.REMOVED.value, reason="http_404")
    with get_session() as s:
        job = s.get(Job, jid)
        res = place(s, job, 95.0, user_id=uid)
        s.commit()
    assert res.created is False
    assert res.outcome == "dead"
    with get_session() as s:
        assert s.exec(select(Application).where(Application.job_id == jid)).first() is None


@pytest.mark.parametrize("state", [
    JobLivenessState.RATE_LIMITED.value,
    JobLivenessState.BLOCKED.value,
    JobLivenessState.UNKNOWN.value,
    JobLivenessState.WRONG_PAGE.value,
    JobLivenessState.LIVE.value,
])
def test_place_delivers_for_every_non_dead_state(state):
    from app.strategy.slate import place
    ext = f"{_PREFIX}place-{state}"
    uid = f"{_PREFIX}u-{state}"
    jid = _mk_job(ext, uid)
    lv.record("greenhouse", ext, state, reason="test")
    with get_session() as s:
        job = s.get(Job, jid)
        res = place(s, job, 95.0, user_id=uid)
        s.commit()
    assert res.created is True, f"{state} must not block delivery"


def test_place_delivers_a_posting_with_no_liveness_row_at_all():
    from app.strategy.slate import place
    ext = f"{_PREFIX}place-never-checked"
    uid = f"{_PREFIX}u-never"
    jid = _mk_job(ext, uid)
    with get_session() as s:
        job = s.get(Job, jid)
        res = place(s, job, 95.0, user_id=uid)
        s.commit()
    assert res.created is True


# ══════════════════════════════════════════════════════════════════════════
# 6. One dead candidate does not stop the others, and never forces the quota
# ══════════════════════════════════════════════════════════════════════════

def test_a_dead_candidate_does_not_stop_the_remaining_candidates():
    from app.strategy.slate import place
    uid = f"{_PREFIX}u-batch"
    dead_ext, live_a, live_b = (f"{_PREFIX}b-dead", f"{_PREFIX}b-live1", f"{_PREFIX}b-live2")
    ids = {e: _mk_job(e, uid, url=f"https://x/jobs/{e}") for e in (dead_ext, live_a, live_b)}
    lv.record("greenhouse", dead_ext, JobLivenessState.REMOVED.value, reason="http_404")

    created = []
    for ext in (dead_ext, live_a, live_b):       # dead one FIRST
        with get_session() as s:
            job = s.get(Job, ids[ext])
            res = place(s, job, 90.0, user_id=uid)
            s.commit()
        if res.created:
            created.append(ext)
    assert created == [live_a, live_b], "a dead candidate must not halt the loop"


def test_a_dead_candidate_never_lowers_the_bar_to_refill_the_slot():
    """Requirement 10: the target is a maximum. Blocking a dead job frees a
    slot, it does not license delivering something below the bar."""
    from app.strategy.slate import place
    uid = f"{_PREFIX}u-quota"
    dead_ext = f"{_PREFIX}q-dead"
    jid = _mk_job(dead_ext, uid)
    lv.record("greenhouse", dead_ext, JobLivenessState.REMOVED.value, reason="http_404")
    with get_session() as s:
        job = s.get(Job, jid)
        res = place(s, job, 95.0, user_id=uid)
        s.commit()
    assert res.created is False
    # Nothing was delivered in its place and no application row exists.
    with get_session() as s:
        n = len(s.exec(select(Application).where(Application.user_id == uid)).all())
    assert n == 0


# ══════════════════════════════════════════════════════════════════════════
# 7. Free board-absence evidence
# ══════════════════════════════════════════════════════════════════════════

def test_board_absence_from_a_complete_fetch_blocks_later_delivery():
    from app.strategy.slate import place
    ext = f"{_PREFIX}board-gone"
    uid = f"{_PREFIX}u-board"
    jid = _mk_job(ext, uid)
    lv.record_board_absence("greenhouse", present_ids=[f"{_PREFIX}still-there"],
                            known_ids=[ext], board_complete=True)
    with get_session() as s:
        job = s.get(Job, jid)
        res = place(s, job, 95.0, user_id=uid)
        s.commit()
    assert res.created is False and res.outcome == "dead"


def test_board_absence_from_an_incomplete_fetch_does_not_block_delivery():
    from app.strategy.slate import place
    ext = f"{_PREFIX}board-partial"
    uid = f"{_PREFIX}u-partial"
    jid = _mk_job(ext, uid)
    lv.record_board_absence("greenhouse", present_ids=[], known_ids=[ext],
                            board_complete=False)
    with get_session() as s:
        job = s.get(Job, jid)
        res = place(s, job, 95.0, user_id=uid)
        s.commit()
    assert res.created is True


# ══════════════════════════════════════════════════════════════════════════
# 8. Observability: aggregate only, no identifiers in keys
# ══════════════════════════════════════════════════════════════════════════

def test_metrics_carry_no_job_or_external_identifiers(monkeypatch):
    _patch_fetch(monkeypatch, status=200, body="hello")
    ext = f"{_PREFIX}metrics-id-check"
    gate.verify_for_delivery(JobSource.GREENHOUSE, ext, "https://x/jobs/1")
    snap = gate.metrics_snapshot()
    assert snap["checks_attempted"] == 1
    for key in snap:
        assert ext not in key
        assert _PREFIX not in key
        assert "https://" not in key
    # by_source is bounded by the ATS families, which is a safe label space.
    assert any(k.startswith("by_source:greenhouse:") for k in snap)


def test_metrics_record_latency(monkeypatch):
    def _slow(url, timeout):
        time.sleep(0.05)
        return 200, url, "", None

    monkeypatch.setattr(gate, "_fetch", _slow)
    gate.verify_for_delivery(JobSource.GREENHOUSE, f"{_PREFIX}lat", "https://x/jobs/1")
    snap = gate.metrics_snapshot()
    assert snap["latency_samples"] == 1
    assert snap["latency_ms_avg"] >= 40


def test_the_fetch_is_ssrf_guarded():
    """Job URLs can come from POST /api/jobs/submit, so the gate must not be a
    server-side request forgery primitive."""
    import inspect
    src = inspect.getsource(gate._fetch)
    assert "guarded_request" in src
    assert "follow_redirects=False" in src


# ══════════════════════════════════════════════════════════════════════════
# 9. The gate can never stall the delivery pipeline
# ══════════════════════════════════════════════════════════════════════════

def test_a_spent_cycle_budget_stops_making_requests(monkeypatch):
    calls = {"n": 0}

    def _slow(url, timeout):
        calls["n"] += 1
        time.sleep(0.06)
        return 200, url, "", None

    monkeypatch.setattr(gate, "_fetch", _slow)
    budget = gate.CycleBudget(seconds=0.05)          # one check exhausts it
    assert gate.verified_dead(JobSource.GREENHOUSE, f"{_PREFIX}bud1",
                              "https://x/jobs/1", budget=budget) is False
    assert budget.exhausted
    # Every later candidate in this cycle is decided on cached evidence only.
    for i in range(5):
        assert gate.verified_dead(JobSource.GREENHOUSE, f"{_PREFIX}bud-{i}",
                                  "https://x/jobs/2", budget=budget) is False
    assert calls["n"] == 1
    assert gate.metrics_snapshot()["checks_skipped_cycle_budget"] == 5


def test_a_spent_budget_still_blocks_a_posting_already_known_dead(monkeypatch):
    """Falling back to cached evidence must not mean falling back to nothing."""
    def _boom(url, timeout):
        raise AssertionError("budget exhausted: no request may be made")

    monkeypatch.setattr(gate, "_fetch", _boom)
    ext = f"{_PREFIX}bud-dead"
    lv.record("greenhouse", ext, JobLivenessState.REMOVED.value, reason="http_404")
    budget = gate.CycleBudget(seconds=0.0001)
    budget.charge(10.0)
    assert budget.exhausted
    assert gate.verified_dead(JobSource.GREENHOUSE, ext, "https://x/jobs/1",
                              budget=budget) is True


def test_a_zero_budget_means_unlimited_not_disabled(monkeypatch):
    """limit<=0 disables the bound rather than disabling checking."""
    _patch_fetch(monkeypatch, status=200, body="ok")
    budget = gate.CycleBudget(seconds=0)
    budget.charge(999.0)
    assert budget.exhausted is False
    assert gate.verified_dead(JobSource.GREENHOUSE, f"{_PREFIX}bud-zero",
                              "https://x/jobs/1", budget=budget) is False
