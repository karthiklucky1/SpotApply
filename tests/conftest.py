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


# True only when the real sentence-transformers/torch stack is installed.
_HAS_REAL_ST = importlib.util.find_spec("torch") is not None


def pytest_collection_modifyitems(config, items):
    """Skip tests that need the real ML stack (torch) when it isn't installed.

    These run normally in a full environment; in lightweight ones where torch
    can't be installed we skip rather than fail on the stub.
    """
    if _HAS_REAL_ST:
        return
    skip_ml = pytest.mark.skip(reason="requires torch/sentence-transformers (not installed)")
    for item in items:
        if "test_grounding" in item.nodeid:
            item.add_marker(skip_ml)
