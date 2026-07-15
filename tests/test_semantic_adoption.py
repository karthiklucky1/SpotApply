"""Semantic adoption: keep title matches PLUS the closest résumé-neighbours so
"same work, different title" jobs are copied into a user's pool and scored.

The embedding pass imports app.matching.matcher (the ML stack, unimportable in
this env), so we inject a light fake matcher + pipeline for the semantic tests
and exercise the branching/threshold logic without the real model.
"""
from __future__ import annotations

import sys
import types

import numpy as np

import app.strategy.adoption as ad
from app.db.models import Job, JobSource


def _job(title, ext="x"):
    return Job(title=title, company="Co", location="Remote", remote=True,
               description="desc", source=JobSource.GREENHOUSE, external_id=ext,
               url=f"http://x/{ext}")


# ── _select_adoptable: title-only vs semantic ───────────────────────────────────
def test_select_title_only_when_semantic_disabled(monkeypatch):
    monkeypatch.setattr(ad.settings, "adoption_semantic_enabled", False)
    jobs = [_job("Machine Learning Engineer", "1"),   # title match
            _job("Applied Scientist", "2"),           # off-title
            _job("Warehouse Associate", "3")]         # off-title
    out = ad._select_adoptable(jobs, ["machine learning engineer"], "u", limit=50)
    titles = {j.title for j in out}
    assert "Machine Learning Engineer" in titles
    assert "Applied Scientist" not in titles      # no semantic pass → dropped
    assert "Warehouse Associate" not in titles


def test_select_adds_semantic_extras(monkeypatch):
    monkeypatch.setattr(ad.settings, "adoption_semantic_enabled", True)
    # Stub the embedding pass: pretend "Applied Scientist" is a close neighbour.
    extra = _job("Applied Scientist", "2")
    monkeypatch.setattr(ad, "_semantic_extras", lambda others, roles, uid, need: [extra])
    jobs = [_job("Machine Learning Engineer", "1"), extra, _job("Warehouse Associate", "3")]
    out = ad._select_adoptable(jobs, ["machine learning engineer"], "u", limit=50)
    titles = {j.title for j in out}
    assert titles == {"Machine Learning Engineer", "Applied Scientist"}  # title + semantic


def test_select_respects_limit(monkeypatch):
    monkeypatch.setattr(ad.settings, "adoption_semantic_enabled", True)
    called = {}
    def _fake_extras(others, roles, uid, need):
        called["need"] = need
        return [_job("Extra", "e")]
    monkeypatch.setattr(ad, "_semantic_extras", _fake_extras)
    # 3 title matches, limit 3 → no room left for extras, and _semantic_extras isn't even reached.
    jobs = [_job("ML Engineer", "1"), _job("Machine Learning Engineer", "2"),
            _job("ML Engineer", "3"), _job("Applied Scientist", "4")]
    out = ad._select_adoptable(jobs, ["machine learning engineer"], "u", limit=3)
    assert len(out) == 3
    assert "need" not in called  # len(title_hits) >= limit → semantic pass skipped


def test_select_falls_back_on_semantic_error(monkeypatch):
    monkeypatch.setattr(ad.settings, "adoption_semantic_enabled", True)
    def _boom(*a, **k):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(ad, "_semantic_extras", _boom)
    jobs = [_job("Machine Learning Engineer", "1"), _job("Applied Scientist", "2")]
    out = ad._select_adoptable(jobs, ["machine learning engineer"], "u", limit=50)
    assert {j.title for j in out} == {"Machine Learning Engineer"}  # title-only fallback


# ── _semantic_extras: threshold + ranking with a fake embedder ──────────────────
def _install_fake_embedder(monkeypatch, resume="resume text"):
    """Fake Matcher whose encode reads a leading 'sim|' from each job title, so we
    control cosines deterministically. Query text (no prefix) → cosine 1.0."""
    fake_m = types.ModuleType("app.matching.matcher")

    class _FakeMatcher:
        def __init__(self, user_id=None):
            pass

        @staticmethod
        def _job_text(j):
            return j.title  # tests encode the sim into the title as "0.40|..."

        def encode(self, texts):
            rows = []
            for t in texts:
                try:
                    v = float(str(t).split("|", 1)[0])
                except Exception:
                    v = 1.0  # the query text
                rows.append([v, 0.0])
            return np.array(rows, dtype="float32")

    fake_m.Matcher = _FakeMatcher
    monkeypatch.setitem(sys.modules, "app.matching.matcher", fake_m)

    fake_p = types.ModuleType("app.matching.pipeline")
    fake_p._load_resume = lambda user_id=None: resume
    monkeypatch.setitem(sys.modules, "app.matching.pipeline", fake_p)


def test_semantic_extras_threshold_and_sort(monkeypatch):
    _install_fake_embedder(monkeypatch)
    monkeypatch.setattr(ad.settings, "adoption_semantic_threshold", 0.30)
    monkeypatch.setattr(ad.settings, "adoption_semantic_max_candidates", 1500)
    others = [_job("0.50|A", "1"), _job("0.40|B", "2"),
              _job("0.20|C", "3"), _job("0.10|D", "4")]
    picked = ad._semantic_extras(others, ["ml engineer"], "u", need=10)
    # 0.20 / 0.10 are below the 0.30 floor; the rest come back best-cosine first.
    assert [p.external_id for p in picked] == ["1", "2"]


def test_semantic_extras_caps_at_need(monkeypatch):
    _install_fake_embedder(monkeypatch)
    monkeypatch.setattr(ad.settings, "adoption_semantic_threshold", 0.30)
    others = [_job("0.90|A", "1"), _job("0.80|B", "2"), _job("0.70|C", "3")]
    picked = ad._semantic_extras(others, ["ml engineer"], "u", need=2)
    assert [p.external_id for p in picked] == ["1", "2"]  # top-2 by cosine


def test_semantic_extras_empty_when_nothing_clears_threshold(monkeypatch):
    _install_fake_embedder(monkeypatch)
    monkeypatch.setattr(ad.settings, "adoption_semantic_threshold", 0.60)
    others = [_job("0.50|A", "1"), _job("0.40|B", "2")]
    assert ad._semantic_extras(others, ["ml engineer"], "u", need=10) == []


def test_semantic_extras_noop_when_need_zero(monkeypatch):
    _install_fake_embedder(monkeypatch)
    assert ad._semantic_extras([_job("0.9|A", "1")], ["ml engineer"], "u", need=0) == []
