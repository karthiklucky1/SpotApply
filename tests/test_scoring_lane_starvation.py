"""Regressions for two scoring-lane bugs:

1. Transient budget/cooldown failures were counted as per-job scoring failures,
   deferring perfectly-scorable fresh jobs for hours (scoring_lane.py).
2. Attempt-ceiling deferrals were filtered AFTER the SQL LIMIT, so a window of
   deferred fresh jobs could starve valid older jobs (now excluded in-query via
   _deferred_ids()).
"""
import time

import app.strategy.scoring_lane as sl


def _reset():
    sl._fail_counts.clear()
    sl._deferred_until.clear()


# Sentinel ids far outside any range SQLite hands out, so that even without the
# conftest teardown a leaked deferral cannot land on a real job row. (It did: id
# 1 deferred here for an hour made test_scoring_lane.py skip its own job 1.)
_LIVE_ID = 10 ** 9
_EXPIRED_ID = 10 ** 9 + 1


def test_deferred_ids_purges_expired_and_returns_set():
    _reset()
    sl._deferred_until[_LIVE_ID] = time.time() + 3600
    sl._deferred_until[_EXPIRED_ID] = time.time() - 1
    ids = sl._deferred_ids()
    assert ids == {_LIVE_ID}
    assert _EXPIRED_ID not in sl._deferred_until   # expired entry purged


def test_transient_stall_true_when_budget_exhausted(monkeypatch):
    import app.matching.reranker as rr
    monkeypatch.setattr(rr, "llm_budget_exhausted", lambda: True)
    monkeypatch.setattr(rr, "any_provider_available", lambda: True)
    assert sl._transient_llm_stall() is True


def test_transient_stall_true_when_all_providers_cooling(monkeypatch):
    import app.matching.reranker as rr
    monkeypatch.setattr(rr, "llm_budget_exhausted", lambda: False)
    monkeypatch.setattr(rr, "any_provider_available", lambda: False)
    assert sl._transient_llm_stall() is True


def test_transient_stall_false_when_healthy(monkeypatch):
    import app.matching.reranker as rr
    monkeypatch.setattr(rr, "llm_budget_exhausted", lambda: False)
    monkeypatch.setattr(rr, "any_provider_available", lambda: True)
    assert sl._transient_llm_stall() is False
