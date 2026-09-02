"""Distillation groundwork: export label-quality filter, shadow scorer safety,
train/inference pair-builder consistency."""
from __future__ import annotations

import json
from datetime import datetime

from sqlmodel import delete, select

import app.matching.local_scorer as ls
from app.config import settings
from app.db.init_db import get_session
from app.db.models import FunnelEvent, Job, JobSource
from scripts.export_training_data import is_llm_final


def _job(**kw):
    base = dict(title="ML Engineer", company="Acme", location="Remote", remote=True,
                description="Build LLM systems.", source=JobSource.GREENHOUSE,
                external_id="x", url="https://x/1")
    base.update(kw)
    return Job(**base)


# ── Export label-quality filter ───────────────────────────────────────────────
_LLM_BREAKDOWN = json.dumps({
    "skills": {"score": 80, "note": "strong Python/ML overlap"},
    "experience": {"score": 70, "note": "4 yrs vs 3-5 required"},
    "location": {"score": 90, "note": ""},
    "work_auth": {"score": 85, "note": ""},
})
_RULE_BREAKDOWN = json.dumps({
    "skills": {"score": 10, "note": ""}, "experience": {"score": 10, "note": ""},
    "location": {"score": 10, "note": ""}, "work_auth": {"score": 10, "note": ""},
})


def test_genuine_llm_final_is_exported():
    assert is_llm_final("Strong fit for ML platform work.", _LLM_BREAKDOWN) is True


def test_cheap_gate_rows_are_excluded():
    assert is_llm_final("Ghost filtered (score=0.85): stale", _LLM_BREAKDOWN) is False
    assert is_llm_final("Pre-screened (Tier-1 fit 22): off-role", _LLM_BREAKDOWN) is False
    assert is_llm_final("Wrong Door: frontend role", _LLM_BREAKDOWN) is False
    assert is_llm_final("Embedding filtered: low similarity", _LLM_BREAKDOWN) is False


def test_rule_filter_rows_are_excluded():
    # Rule filter synthesizes a breakdown with empty notes — not a real LLM label.
    assert is_llm_final("Different country (India)", _RULE_BREAKDOWN) is False


def test_missing_breakdown_or_reasoning_excluded():
    assert is_llm_final("Strong fit", None) is False
    assert is_llm_final(None, _LLM_BREAKDOWN) is False
    assert is_llm_final("Strong fit", "not-json") is False


# ── Pair-builder consistency (train == inference) ─────────────────────────────
def test_build_pair_identical_between_train_and_inference():
    from scripts.train_local_scorer import build_pair as train_build
    job = _job(description="d" * 6000)
    resume = "r" * 20000
    row = {"resume": resume, "title": job.title, "company": job.company,
           "location": job.location, "remote": job.remote,
           "description": job.description}
    assert ls.build_pair(resume, job) == train_build(row)


# ── Shadow scorer safety ──────────────────────────────────────────────────────
def test_local_scorer_noop_without_model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "local_scorer_path", str(tmp_path / "missing"))
    scorer = ls.LocalScorer()          # fresh instance, not the singleton
    assert scorer.available() is False
    assert scorer.score("resume", _job()) is None


def test_shadow_score_never_raises_without_model(monkeypatch):
    monkeypatch.setattr(settings, "local_scorer_path", "/nonexistent/model")
    ls.LocalScorer._instance = None    # reset singleton
    ls.shadow_score(1, "resume", _job(), 72.0)  # must not raise


def test_shadow_score_records_agreement_event(monkeypatch):
    class _FakeScorer:
        def available(self):
            return True
        def score(self, resume, job):
            return 68.0

    monkeypatch.setattr(ls.LocalScorer, "get", classmethod(lambda cls: _FakeScorer()))
    monkeypatch.setattr(settings, "local_scorer_shadow", True)
    with get_session() as session:
        session.exec(delete(FunnelEvent))
        session.commit()

    ls.shadow_score(123, "resume", _job(), 72.0)

    with get_session() as session:
        ev = session.exec(select(FunnelEvent).where(
            FunnelEvent.stage == "shadow_score")).first()
        assert ev is not None
        m = json.loads(ev.metadata_json)
        assert m["llm"] == 72.0 and m["local"] == 68.0
        assert ev.passed is True                      # |72-68| <= 10


def test_shadow_disabled_records_nothing(monkeypatch):
    monkeypatch.setattr(settings, "local_scorer_shadow", False)
    called = {"n": 0}

    class _FakeScorer:
        def available(self):
            called["n"] += 1
            return True

    monkeypatch.setattr(ls.LocalScorer, "get", classmethod(lambda cls: _FakeScorer()))
    ls.shadow_score(1, "resume", _job(), 50.0)
    assert called["n"] == 0                           # short-circuits before the model


# ── Shadow report math ────────────────────────────────────────────────────────
def test_shadow_report_aggregates(monkeypatch):
    from scripts.shadow_report import report

    # Pairs are placed RELATIVE to the shortlist bar rather than written as
    # literals. The two aggregates measure different things — closeness of the
    # scores, and agreement on the shortlist DECISION — and only bar-relative
    # fixtures keep them separable when the bar moves. (At the old literals a
    # (90, 60) pair agreed only because 60 sat exactly ON a 60 bar; raising the
    # bar to 70 silently turned this into an agreement test that measured the
    # threshold instead of the arithmetic.)
    bar = settings.shortlist_score_threshold
    pairs = [
        (bar + 10, bar + 8),    # close, both shortlist
        (bar - 20, bar - 15),   # close, neither shortlists
        (bar + 30, bar + 5),    # FAR apart, but still both shortlist
        (bar - 30, bar - 27),   # close, neither shortlists
    ]
    with get_session() as session:
        session.exec(delete(FunnelEvent))
        for llm, local in pairs:
            session.add(FunnelEvent(
                job_id=1, stage="shadow_score", passed=abs(llm - local) <= 10,
                reason="", metadata_json=json.dumps({"llm": llm, "local": local}),
                created_at=datetime.utcnow(),
            ))
        session.commit()

    r = report(days=1)
    assert r["n"] == 4
    assert r["within10_pct"] == 75                    # 3 of 4 within 10 pts
    # …yet all four land on the same side of the bar: a big score gap is not the
    # same failure as a different shortlist decision, which is the whole reason
    # both numbers are reported.
    assert r["shortlist_decision_agreement_pct"] == 100.0


def test_hub_id_is_accepted_as_model_source(monkeypatch):
    monkeypatch.setattr(settings, "local_scorer_path", "karthik/hirepath-scorer")
    assert ls._model_source() == "karthik/hirepath-scorer"
    scorer = ls.LocalScorer()
    assert scorer.available() is True                 # will attempt hub download


def test_plain_missing_path_is_not_a_source(monkeypatch):
    monkeypatch.setattr(settings, "local_scorer_path", "data/models/not-there")
    assert ls._model_source() is None
