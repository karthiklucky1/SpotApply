"""The grounding verifier never fails open, and it says who answered.

Production, 2026-09-16, while Anthropic was rejecting every call (400, credit
balance): the tailor log read "Grounding: batched Anthropic verify failed: 400"
followed by "5 verified, 1 LLM call, PASSED". A fallback verifier had served
and a small gpt-4o charge appeared, and nothing on the record said which
provider verified. The auditor filed "whether grounding can fail open when the
provider is down" as a code check. This is that check, pinned:

  * Anthropic fails and no OpenAI client exists → every verdict is False;
  * Anthropic fails and OpenAI answers → OpenAI's verdicts are used and the
    checker records verifier "openai";
  * an unreadable answer is False, never True;
  * a tripped breaker means Anthropic is not even asked, and an exhaustion
    error trips it for everyone else.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.matching.reranker as rr
from app.config import settings
from app.tailoring.grounding import GroundingChecker


def _checker() -> GroundingChecker:
    # Skip __init__ (it would load a SentenceTransformer); the verifier path
    # never touches self.model.
    return object.__new__(GroundingChecker)


class _Anthropic:
    def __init__(self, behaviour):
        self.calls = 0
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls += 1
                if isinstance(behaviour, Exception):
                    raise behaviour
                return SimpleNamespace(content=[SimpleNamespace(text=behaviour)])

        self.messages = _Messages()


class _OpenAI:
    def __init__(self, answer):
        self.calls = 0
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls += 1
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=answer))])

        self.chat = SimpleNamespace(completions=_Completions())


@pytest.fixture(autouse=True)
def _breaker_reset(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider_cooldown_minutes", 30, raising=False)
    state = (rr._provider_down_until, rr._provider_down_since, rr._provider_last_error)
    saved = [dict(d) for d in state]
    for d in state:
        d.clear()
    yield
    for d, before in zip(state, saved):
        d.clear()
        d.update(before)


def _wire_tailor(monkeypatch, anthropic, openai):
    class _FakeTailor:
        def __init__(self):
            self._anthropic_client = anthropic
            self._openai_client = openai
            self._active_backend = "anthropic" if anthropic is not None else (
                "openai" if openai is not None else None)

    monkeypatch.setattr("app.tailoring.tailor.Tailor", _FakeTailor)


PATCHES = [("Built CUDA kernels at DeepMind", "Wrote GPU code"),
           ("Deployed RAG pipelines", "Deployed RAG pipelines with FastAPI")]


def test_anthropic_down_and_no_openai_means_every_verdict_is_false(monkeypatch):
    anth = _Anthropic(RuntimeError("Error code: 400 - Your credit balance is too low"))
    _wire_tailor(monkeypatch, anth, None)
    c = _checker()
    assert c.verify_batch(PATCHES, "master") == [False, False]
    assert c.last_verifier_provider is None
    assert c.verify_with_llm("a claim", "master") is False


def test_the_fallback_verifier_serves_and_is_named(monkeypatch):
    anth = _Anthropic(RuntimeError("Error code: 400 - Your credit balance is too low"))
    oai = _OpenAI("1: FABRICATED\n2: SUPPORTED")
    _wire_tailor(monkeypatch, anth, oai)
    c = _checker()
    assert c.verify_batch(PATCHES, "master") == [False, True]
    assert c.last_verifier_provider == "openai"
    assert oai.calls == 1


def test_an_exhaustion_error_trips_the_shared_breaker(monkeypatch):
    anth = _Anthropic(RuntimeError("Error code: 400 - Your credit balance is too low"))
    _wire_tailor(monkeypatch, anth, _OpenAI("1: SUPPORTED\n2: SUPPORTED"))
    assert rr.provider_available("anthropic")
    _checker().verify_batch(PATCHES, "master")
    assert not rr.provider_available("anthropic"), \
        "the first tailor to hit the suspended account must spare every later one"


def test_a_tripped_breaker_means_anthropic_is_not_even_asked(monkeypatch):
    rr._mark_provider_down("anthropic", "credit")
    anth = _Anthropic("1: SUPPORTED\n2: SUPPORTED")
    oai = _OpenAI("1: SUPPORTED\n2: FABRICATED")
    _wire_tailor(monkeypatch, anth, oai)
    c = _checker()
    assert c.verify_batch(PATCHES, "master") == [True, False]
    assert anth.calls == 0 and oai.calls == 1
    assert c.last_verifier_provider == "openai"


def test_anthropic_answers_are_used_when_it_is_up(monkeypatch):
    anth = _Anthropic("1: SUPPORTED\n2: FABRICATED")
    oai = _OpenAI("1: FABRICATED\n2: FABRICATED")
    _wire_tailor(monkeypatch, anth, oai)
    c = _checker()
    assert c.verify_batch(PATCHES, "master") == [True, False]
    assert c.last_verifier_provider == "anthropic" and oai.calls == 0


def test_an_unreadable_answer_is_never_a_pass(monkeypatch):
    _wire_tailor(monkeypatch, _Anthropic("sure, looks fine to me"), None)
    c = _checker()
    assert c.verify_batch(PATCHES, "master") == [False, False]


def test_a_plain_provider_error_does_not_trip_the_breaker(monkeypatch):
    anth = _Anthropic(RuntimeError("overloaded_error: try again"))
    _wire_tailor(monkeypatch, anth, _OpenAI("1: SUPPORTED\n2: SUPPORTED"))
    _checker().verify_batch(PATCHES, "master")
    assert rr.provider_available("anthropic"), "a transient error is not exhaustion"
