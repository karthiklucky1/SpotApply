"""Process-wide LLM SDK clients — the one place client instances come from.

Every `Anthropic()` / `OpenAI()` construction builds an httpx connection pool
and an SSL context that only an explicit `.close()` tears down — and almost no
call site closes. Constructing one per call was the slow RSS climb from a
~850MB baseline to the container ceiling (the post-mortem note sits on
`_shared_llm_clients` in app/matching/reranker.py): pooled sockets and SSL
state are exactly the kind of allocation that fragments the heap and is never
returned to the OS. Both SDKs are designed to be long-lived and thread-safe,
so one pair for the whole process is both correct and much cheaper.

The pair itself lives in app.matching.reranker (the first module that needed
it, and its tests drive the cold-build path via `rr._CLIENTS`); this module is
the neutral import point so autofill/tailoring/intelligence/server call sites
don't each grow their own copy.
"""
from __future__ import annotations


def shared_llm_clients() -> tuple:
    """(anthropic_client, openai_client, active_backend), one pair per process."""
    from app.matching.reranker import _shared_llm_clients
    return _shared_llm_clients()


def _with_options(client, timeout: float | None, max_retries: int | None):
    if client is None:
        return None
    opts: dict = {}
    if timeout is not None:
        opts["timeout"] = timeout
    if max_retries is not None:
        opts["max_retries"] = max_retries
    # with_options() copies client config but REUSES the underlying httpx
    # connection pool — per-call overrides stay leak-free.
    return client.with_options(**opts) if opts else client


def shared_anthropic(*, timeout: float | None = None, max_retries: int | None = None):
    """The shared Anthropic client, or None when no key is configured.

    Defaults are lane-safe: `llm_request_timeout` bounds every request (the SDK
    default is 10 minutes) with no SDK-side retries. Request paths that want a
    longer window or a retry pass them here instead of building a client.
    """
    return _with_options(shared_llm_clients()[0], timeout, max_retries)


def shared_openai(*, timeout: float | None = None, max_retries: int | None = None):
    """The shared OpenAI client, or None when no key is configured."""
    return _with_options(shared_llm_clients()[1], timeout, max_retries)


# Model families that REJECT sampling parameters with a 400. Anthropic removed
# `temperature`/`top_p`/`top_k` from Opus 4.7 onward and from the Claude 5
# family; adaptive thinking plus `output_config.effort` replaced them. The
# Python SDK went further and dropped `temperature` from the typed
# `Messages.create` signature entirely (1.x), so passing it as a named argument
# is a TypeError on EVERY model regardless of what the wire API accepts.
_NO_SAMPLING = (
    "opus-4-7", "opus-4-8", "opus-5", "sonnet-5", "fable-5", "mythos-5",
)


def supports_sampling(model: str) -> bool:
    """True when this model still accepts a temperature on the wire."""
    m = (model or "").lower()
    return not any(fam in m for fam in _NO_SAMPLING)


def sampling(model: str, temperature: float | None) -> dict:
    """Kwargs pinning generation temperature, or {} where it isn't accepted.

    Returns an ``extra_body`` payload rather than a named argument because the
    SDK no longer types one. Splatted at the call site
    (``client.messages.create(..., **sampling(model, 0.0))``) so a model that
    rejects sampling simply gets nothing — which is the right failure: a 400 on
    every scoring call is a far worse outcome than an unpinned temperature, and
    on those models `output_config.effort` is the equivalent lever.

    This matters because it is what makes verification reproducible. Unpinned,
    the tailoring stack ran at the SDK default of 1.0 and two runs of the same
    résumé against the same JD reached different honesty verdicts.
    """
    if temperature is None or not supports_sampling(model):
        return {}
    return {"extra_body": {"temperature": float(temperature)}}
