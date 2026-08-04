"""Shared test setup.

Some modules under test import heavy ML dependencies (torch via
sentence-transformers, faiss) transitively. Those can't always be installed
in CI / lightweight environments. When they're genuinely absent, register
lightweight *package* stubs so imports of pure-Python siblings still work —
but NEVER override a real, installed package.
"""
from __future__ import annotations

# ── Test database isolation ───────────────────────────────────────────────────
# CRITICAL: point the test suite at a throwaway SQLite file BEFORE any app module
# is imported. The engine in app.db.init_db is built from settings.sqlite_url at
# import time, and Settings reads SQLITE_PATH from the environment. Without this,
# tests seed/delete rows in the real ./data/jobagent.db — which is how FunnelCo
# rows and orphan applications leaked into production. Setting it here guarantees
# tests never touch real data.
import os
import pathlib
import tempfile

_TEST_DB = pathlib.Path(tempfile.gettempdir()) / f"jobagent_test_{os.getpid()}.db"
os.environ["SQLITE_PATH"] = str(_TEST_DB)
os.environ["FAISS_INDEX_PATH"] = str(_TEST_DB.with_suffix(".faiss"))
os.environ["DATABASE_URL"] = ""
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_ANON_KEY"] = ""
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = ""

import importlib.util
import sys
import types


def _ensure_stub(name: str, attrs: dict | None = None, submodules: dict | None = None) -> None:
    # Only stub if the real package is not installed.
    if importlib.util.find_spec(name) is not None:
        return
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    mod.__path__ = []  # mark as a package so `import name.sub` can resolve
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    for sub_name, sub_attrs in (submodules or {}).items():
        full = f"{name}.{sub_name}"
        sub = types.ModuleType(full)
        for k, v in (sub_attrs or {}).items():
            setattr(sub, k, v)
        sys.modules[full] = sub
        setattr(mod, sub_name, sub)


_ensure_stub(
    "sentence_transformers",
    attrs={"SentenceTransformer": object, "CrossEncoder": object},
    submodules={"util": {"cos_sim": lambda *a, **k: None}},
)
_ensure_stub("faiss")
_ensure_stub("rank_bm25", attrs={"BM25Okapi": object})


import pytest


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    """Ensure the SQLite schema exists before any DB-dependent test runs.

    Several tests use a live session (dedup, funnel) and assume the tables
    have already been created. On a fresh checkout there is no DB file yet,
    so create the schema (+ migrations) once per test session.
    """
    from app.db.init_db import init_db
    from app.config import settings
    # Safety net: never run the suite against the real production database.
    assert "jobagent_test_" in str(settings.sqlite_path), (
        f"Test DB isolation failed — tests are pointing at {settings.sqlite_path}. "
        "SQLITE_PATH must resolve to a temp file (see top of conftest.py)."
    )
    init_db()
    yield
    # Tear down the throwaway DB + index after the session.
    for p in (_TEST_DB, _TEST_DB.with_suffix(".faiss")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _reset_process_globals():
    """Clear every module-level mutable counter/cache between tests.

    These modules deliberately hold process-global state (budget counters, the
    provider circuit breaker, attempt-ceiling deferrals, caches) because in
    production one process serves all lanes for its whole lifetime. Under pytest
    that lifetime spans the entire suite, so one test's leftovers silently change
    another file's result.

    This was not hypothetical. test_scoring_lane_starvation.py set
    `_deferred_until[1] = now + 3600` and never cleaned up; SQLite then reused
    row id 1 in test_scoring_lane.py, whose jobs the lane skipped as "deferred".
    Two tests failed only when the files ran in that order — invisible in the
    default alphabetical order, reproducible under any reordering.

    Resetting after each test (not just before) makes the whole class of bug
    impossible rather than fixing the one instance.
    """
    yield
    from app.matching import reranker as _rr
    from app.strategy import scoring_lane as _sl
    for d in (_sl._fail_counts, _sl._deferred_until, _sl._prescore_memo,
              _rr._provider_down_until, _rr._user_finals):
        d.clear()
    for counter, key in ((_rr._daily_finals, "day"), (_rr._hourly_finals, "hour")):
        counter[key] = ""
        counter["count"] = 0
    _rr._usage_totals.update(calls=0, input=0, cache_read=0, cache_write=0, output=0)
    try:
        from app.matching import cards as _cards
        _cards._mints_today["day"] = ""
        _cards._mints_today["count"] = 0
    except ImportError:      # module is optional in trimmed environments
        pass
    try:
        from app.matching.pipeline import _RESUME_CACHE
        _RESUME_CACHE.clear()
    except ImportError:
        pass


# True only when the real sentence-transformers/torch stack is installed.
_HAS_REAL_ST = importlib.util.find_spec("torch") is not None

# Tests that genuinely construct a real GroundingChecker (which imports
# sentence_transformers at module scope) and so cannot run on the stub.
#
# This list used to be the substring "test_grounding", which swept up every file
# whose name began that way — including pure-Python ones like
# test_grounding_metric_gate.py and test_grounding_enforcement.py, the latter
# stubbing the grounding module precisely so it could test the ML-ABSENT path.
# The anti-hallucination gate is exactly what must not go untested in CI, so this
# matches whole files by name and nothing more.
_NEEDS_TORCH = (
    "tests/test_grounding.py::",
    "tests/test_grounding_fail_open.py::",
)


def pytest_collection_modifyitems(config, items):
    """Skip tests that need the real ML stack (torch) when it isn't installed.

    These run normally in a full environment; in lightweight ones where torch
    can't be installed we skip rather than fail on the stub.
    """
    if _HAS_REAL_ST:
        return
    skip_ml = pytest.mark.skip(reason="requires torch/sentence-transformers (not installed)")
    for item in items:
        nodeid = item.nodeid.replace("\\", "/")
        if any(nodeid.startswith(m) or f"/{m}" in f"/{nodeid}" for m in _NEEDS_TORCH):
            item.add_marker(skip_ml)
