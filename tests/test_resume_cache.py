"""Résumé Storage cache — the egress fix. Every lane used to re-download the
résumé from Supabase Storage on every pass (~1,000 downloads/user/day, the
5-8 GB/day egress baseline). Now: one download per TTL window, explicit
invalidation on upload, and misses are never cached."""
from __future__ import annotations

import pytest

import app.matching.pipeline as pl


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(pl, "_RESUME_CACHE", {})
    # Force the supabase branch without real Supabase.
    monkeypatch.setattr(pl.settings.__class__, "use_supabase",
                        property(lambda self: True), raising=False)
    yield


def test_storage_hit_only_once_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(uid):
        calls["n"] += 1
        return f"resume-of-{uid}"

    monkeypatch.setattr(pl, "_fetch_resume_from_storage", fake_fetch)
    assert pl._load_resume_file("u1") == "resume-of-u1"
    assert pl._load_resume_file("u1") == "resume-of-u1"
    assert pl._load_resume_file("u1") == "resume-of-u1"
    assert calls["n"] == 1                      # one download, many reads


def test_users_are_isolated(monkeypatch):
    monkeypatch.setattr(pl, "_fetch_resume_from_storage", lambda uid: f"r-{uid}")
    assert pl._load_resume_file("u1") == "r-u1"
    assert pl._load_resume_file("u2") == "r-u2"  # never another tenant's résumé


def test_invalidate_forces_refetch(monkeypatch):
    versions = iter(["v1", "v2"])
    monkeypatch.setattr(pl, "_fetch_resume_from_storage", lambda uid: next(versions))
    assert pl._load_resume_file("u1") == "v1"
    pl.invalidate_resume_cache("u1")
    assert pl._load_resume_file("u1") == "v2"   # fresh upload visible immediately


def test_ttl_expiry_refetches(monkeypatch):
    versions = iter(["v1", "v2"])
    monkeypatch.setattr(pl, "_fetch_resume_from_storage", lambda uid: next(versions))
    t = {"now": 1000.0}
    monkeypatch.setattr(pl.time, "time", lambda: t["now"])
    assert pl._load_resume_file("u1") == "v1"
    t["now"] += pl._RESUME_CACHE_TTL_S + 1
    assert pl._load_resume_file("u1") == "v2"


def test_missing_resume_is_not_cached(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(uid):
        calls["n"] += 1
        raise ValueError("No resume found")

    monkeypatch.setattr(pl, "_fetch_resume_from_storage", fake_fetch)
    for _ in range(2):
        with pytest.raises(ValueError):
            pl._load_resume_file("u1")
    assert calls["n"] == 2                      # miss never cached: retry works
