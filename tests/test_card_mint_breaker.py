"""Card minting consults the provider circuit breaker the lanes already trip.

A 106-line production log sample from 2026-09-14..16 — the days Anthropic was
rejecting every call for an unpaid balance — carried 23 "card mint call failed"
(400) lines. cards._haiku_json called Anthropic directly, so every mint attempt
paid a doomed request and its timeout while every scoring lane had already
stopped asking. Pinned: a tripped breaker means no call; an exhaustion error
trips it; a transient error does not.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.matching.reranker as rr
from app.config import settings
from app.matching import cards


class _Anthropic:
    def __init__(self, behaviour):
        self.calls = 0
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls += 1
                if isinstance(behaviour, Exception):
                    raise behaviour
                return SimpleNamespace(stop_reason="end_turn",
                                       content=[SimpleNamespace(text=behaviour)])

        self.messages = _Messages()


@pytest.fixture(autouse=True)
def _breaker_reset(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider_cooldown_minutes", 30, raising=False)
    state = (rr._provider_down_until, rr._provider_down_since, rr._provider_last_error)
    saved = [dict(d) for d in state]
    for d in state:
        d.clear()
    cards._breaker_skip["logged_at"] = None
    cards._breaker_skip["skipped"] = 0
    yield
    for d, before in zip(state, saved):
        d.clear()
        d.update(before)


def _wire(monkeypatch, anth):
    monkeypatch.setattr(rr, "_shared_llm_clients", lambda: (anth, None, "anthropic"))


def test_a_tripped_breaker_means_no_call_at_all(monkeypatch, caplog):
    anth = _Anthropic('{"ok": true}')
    _wire(monkeypatch, anth)
    rr._mark_provider_down("anthropic", "credit")
    with caplog.at_level("WARNING"):
        assert cards._haiku_json("sys", "user") is None
        assert cards._haiku_json("sys", "user") is None
    assert anth.calls == 0
    skips = [r for r in caplog.records if "card mint skipped" in r.getMessage()]
    assert len(skips) == 1, "the skip is logged once per interval, not per mint"


def test_an_exhaustion_error_trips_the_breaker_for_everyone(monkeypatch):
    anth = _Anthropic(RuntimeError("Error code: 400 - Your credit balance is too low"))
    _wire(monkeypatch, anth)
    assert cards._haiku_json("sys", "user") is None
    assert anth.calls == 1
    assert not rr.provider_available("anthropic")
    # …and the next mint does not pay for the same answer.
    assert cards._haiku_json("sys", "user") is None
    assert anth.calls == 1


def test_a_transient_error_does_not_trip_the_breaker(monkeypatch):
    anth = _Anthropic(RuntimeError("overloaded_error"))
    _wire(monkeypatch, anth)
    assert cards._haiku_json("sys", "user") is None
    assert rr.provider_available("anthropic")


def test_a_healthy_mint_still_parses(monkeypatch):
    anth = _Anthropic('{"role_family": "backend engineer"}')
    _wire(monkeypatch, anth)
    assert cards._haiku_json("sys", "user") == {"role_family": "backend engineer"}
    assert anth.calls == 1
