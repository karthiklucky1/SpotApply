"""The grounding GATE, not the grounding detector.

tests/test_grounding*.py all exercise `GroundingChecker.check` — the detector.
Nothing exercised what tailor.py *does* with the verdict, and that gap hid a
real defect: `GroundingChecker()` is constructed inside a bare
`try/except Exception`, and grounding.py imports sentence_transformers at module
level. So on any deploy where the ML stack is absent or the model download
fails, every tailored résumé was delivered with

    "grounding_passed": true

in its report — claiming an anti-hallucination check that never read a single
bullet. The three states (passed / failed / never ran) are now distinct, and
these tests pin all three plus the strict posture.
"""
from __future__ import annotations

import json
import sys
import types

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job

_OWNER = "grounding-gate-user"


def _fake_grounding_module(passed: bool, flagged=None, *, raise_on_init=False):
    """A stand-in for app.tailoring.grounding that needs no torch."""
    mod = types.ModuleType("app.tailoring.grounding")

    class _Result:
        def __init__(self):
            self.passed = passed
            self.flagged_bullets = list(flagged or [])
            self.confidence_map = {"b1": 0.9, "b2": 0.4}

    class GroundingChecker:
        def __init__(self):
            if raise_on_init:
                raise ImportError("No module named 'sentence_transformers'")

        def check(self, master, tailored):
            return _Result()

    mod.GroundingChecker = GroundingChecker
    return mod


def _fake_doctor_module():
    """A always-passing ResumeDoctor so only grounding drives the outcome."""
    mod = types.ModuleType("app.tailoring.doctor")

    class _DResult:
        score = 95
        ats_coverage_pct = 0.8
        llm_verdict = "strong"
        weak_bullets: list = []
        banned_found: list = []
        integrity_issues: list = []
        human_score = 90
        fingerprint_flags: list = []
        issues: list = []
        passed = True

    class ResumeDoctor:
        def check(self, resume_md, master, jd):
            return _DResult()

    mod.ResumeDoctor = ResumeDoctor
    # grounding.py and others import this symbol from the real module.
    import re
    mod._METRIC_RE = re.compile(r"\d+")
    return mod


@pytest.fixture
def app_id(tmp_path, monkeypatch):
    """One Application owned by _OWNER, with all LLM/IO work stubbed out."""
    from app.config import settings
    from app.tailoring import tailor as tailor_mod

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)

    with get_session() as s:
        for a in s.exec(select(Application).where(Application.user_id == _OWNER)).all():
            s.delete(a)
        for j in s.exec(select(Job).where(Job.user_id == _OWNER)).all():
            s.delete(j)
        s.commit()
        job = Job(user_id=_OWNER, source="greenhouse", external_id="gate-1",
                  title="Senior Backend Engineer", company="GateCo",
                  url="https://example.com/gate/1", description="Build APIs. Python, Postgres.")
        s.add(job)
        s.commit()
        s.refresh(job)
        application = Application(user_id=_OWNER, job_id=job.id,
                                  status=ApplicationStatus.SHORTLISTED)
        s.add(application)
        s.commit()
        s.refresh(application)
        aid = application.id

    monkeypatch.setattr(tailor_mod.Tailor, "__init__", lambda self: None, raising=False)
    monkeypatch.setattr(tailor_mod.Tailor, "tailor_resume",
                        lambda self, *a, **k: "# Alex Tenant\n\n- Built APIs in Python\n",
                        raising=False)
    monkeypatch.setattr(tailor_mod.Tailor, "write_cover_letter",
                        lambda self, *a, **k: "Dear GateCo,\n\nI am interested.\n",
                        raising=False)
    monkeypatch.setattr("app.matching.pipeline._load_resume",
                        lambda user_id=None: "# Alex Tenant\n\n- Built APIs in Python\n")
    monkeypatch.setitem(sys.modules, "app.tailoring.doctor", _fake_doctor_module())
    return aid


def _report(tmp_path, aid: int) -> dict:
    p = tmp_path / "tailored" / f"app_{aid}" / "report.json"
    assert p.exists(), f"no report written at {p}"
    return json.loads(p.read_text())


def _status(aid: int) -> ApplicationStatus:
    with get_session() as s:
        return s.get(Application, aid).status


def _notes(aid: int) -> str:
    with get_session() as s:
        return s.get(Application, aid).notes or ""


def test_grounding_pass_lands_tailored_and_reports_passed(app_id, tmp_path, monkeypatch):
    from app.tailoring.tailor import tailor_for_application
    monkeypatch.setitem(sys.modules, "app.tailoring.grounding",
                        _fake_grounding_module(passed=True))
    tailor_for_application(app_id)
    assert _status(app_id) == ApplicationStatus.TAILORED
    r = _report(tmp_path, app_id)
    assert r["grounding_passed"] is True
    assert r["grounding_status"] == "passed"


def test_grounding_failure_blocks_at_error_and_names_the_bullet(app_id, tmp_path, monkeypatch):
    """The enforcement path: a flagged bullet must never reach TAILORED."""
    from app.tailoring.tailor import tailor_for_application
    monkeypatch.setitem(
        sys.modules, "app.tailoring.grounding",
        _fake_grounding_module(passed=False,
                              flagged=[{"bullet": "Scaled billing to 40M users"}]))
    tailor_for_application(app_id)
    assert _status(app_id) == ApplicationStatus.ERROR
    assert "Scaled billing to 40M users" in _notes(app_id)
    r = _report(tmp_path, app_id)
    assert r["grounding_passed"] is False
    assert r["grounding_status"] == "failed"


def test_unrunnable_grounding_is_never_reported_as_passed(app_id, tmp_path, monkeypatch):
    """THE bug. GroundingChecker() raising must not read as a clean résumé.

    This is the ML-absent path, which is the one both CI and a broken deploy
    take — so it is the path most likely to be live and least likely to be
    noticed.
    """
    from app.tailoring.tailor import tailor_for_application
    monkeypatch.setitem(sys.modules, "app.tailoring.grounding",
                        _fake_grounding_module(passed=True, raise_on_init=True))
    tailor_for_application(app_id)

    r = _report(tmp_path, app_id)
    assert r["grounding_passed"] is not True, (
        "a résumé whose grounding check never ran was reported as having passed it"
    )
    assert r["grounding_passed"] is None
    assert r["grounding_status"] == "unverified"
    # Delivered (the human review step is the backstop) but explicitly warned,
    # because that backstop only works if the human is told.
    assert _status(app_id) == ApplicationStatus.TAILORED
    assert "could not be verified" in _notes(app_id).lower()


def test_grounding_required_blocks_when_the_check_cannot_run(app_id, monkeypatch):
    """Strict posture: opt in and "could not verify" becomes a hard failure."""
    from app.config import settings
    from app.tailoring.tailor import tailor_for_application
    monkeypatch.setattr(settings, "grounding_required", True, raising=False)
    monkeypatch.setitem(sys.modules, "app.tailoring.grounding",
                        _fake_grounding_module(passed=True, raise_on_init=True))
    tailor_for_application(app_id)
    assert _status(app_id) == ApplicationStatus.ERROR
    assert "could not be verified" in _notes(app_id).lower()


def test_grounding_required_defaults_off_so_an_ml_hiccup_is_not_an_outage():
    """Documented product decision, pinned so a flip is deliberate."""
    from app.config import settings
    assert settings.grounding_required is False
