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

        def check(self, master, tailored, *, use_cache=True):
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


def test_the_lenient_posture_still_delivers_with_a_warning(app_id, tmp_path, monkeypatch):
    """With GROUNDING_REQUIRED off, an unrunnable check delivers the résumé but
    says so — the human review step is the backstop, and it only works if the
    human is told."""
    from app.config import settings
    from app.tailoring.tailor import tailor_for_application
    monkeypatch.setattr(settings, "grounding_required", False, raising=False)
    monkeypatch.setitem(sys.modules, "app.tailoring.grounding",
                        _fake_grounding_module(passed=True, raise_on_init=True))
    tailor_for_application(app_id)
    assert _status(app_id) == ApplicationStatus.TAILORED
    assert "could not be verified" in _notes(app_id).lower()


def test_grounding_required_defaults_ON_before_real_users():
    """Flipped to True for the launch cohort (2026-08-21 pre-launch review).

    Off, a broken ML stack meant the check threw, the résumé shipped as TAILORED
    anyway, and only a log line said otherwise — on a product whose core promise
    is that tailoring stays grounded in the real résumé. Failing closed costs a
    retry; failing open puts a fabricated bullet in someone's real application.
    Pinned so a flip back is deliberate."""
    from app.config import settings
    assert settings.grounding_required is True


def test_a_fabricated_fact_blocks_even_when_grounding_passes(app_id, tmp_path, monkeypatch):
    """The deterministic guard outranks the model's opinion.

    Grounding only inspects lines it recognises as achievement bullets, so an
    invented employer sitting in an experience HEADER never reaches it — the
    check passes and the résumé ships claiming a job the candidate never had.
    Set difference over the master's own facts catches that without asking
    anyone, and it must be able to block on its own.
    """
    from app.tailoring.tailor import tailor_for_application
    monkeypatch.setitem(sys.modules, "app.tailoring.grounding",
                        _fake_grounding_module(passed=True))
    monkeypatch.setattr(
        "app.tailoring.tailor.Tailor.tailor_resume",
        lambda self, *a, **k: (
            "# Alex Tenant\n\n## PROFESSIONAL EXPERIENCE\n"
            "**Staff Engineer** | Stripe | May 2019 - Aug 2024 | Remote\n"
            "- Built APIs in Python\n"
        ),
        raising=False)
    tailor_for_application(app_id)

    assert _status(app_id) == ApplicationStatus.ERROR
    notes = _notes(app_id).lower()
    assert "stripe" in notes, "the user must be told WHICH fact was invented"
    r = _report(tmp_path, app_id)
    assert r["fabrications"], "the report claimed a clean résumé"
    assert {f["kind"] for f in r["fabrications"]} & {"employer", "job title", "employment date"}


def test_a_style_failure_delivers_the_resume_instead_of_blocking(app_id, tmp_path, monkeypatch):
    """Reading machine-written is not a false claim.

    It earns a rebuild and an honest report, but it must never park a truthful
    résumé at ERROR — that leaves the user with nothing to submit over a
    bolding count, which is a worse outcome than the defect.
    """
    import types as _types

    from app.tailoring.tailor import tailor_for_application
    monkeypatch.setitem(sys.modules, "app.tailoring.grounding",
                        _fake_grounding_module(passed=True))

    doctor_mod = _fake_doctor_module()
    doctor_mod.ResumeDoctor.check = lambda self, resume_md, master, jd: _types.SimpleNamespace(
        score=80, ats_coverage_pct=0.8, llm_verdict="fine", weak_bullets=[],
        banned_found=[], integrity_issues=[], human_score=24, fingerprint_flags=["uniform"],
        issues=["reads machine-written"], passed=True, human_passed=False,
    )
    monkeypatch.setitem(sys.modules, "app.tailoring.doctor", doctor_mod)

    tailor_for_application(app_id)

    assert _status(app_id) == ApplicationStatus.TAILORED
    r = _report(tmp_path, app_id)
    assert r["human_passed"] is False
    assert r["doctor_passed"] is False, (
        "a résumé we score 24/100 on reading human was reported as passing"
    )
    assert "machine-written" in _notes(app_id).lower()


def test_a_user_can_ask_for_a_fresh_check_of_a_delivered_resume(app_id, tmp_path, monkeypatch):
    """The manual escape hatch.

    Every delivered résumé was already verified, and re-asking normally returns
    the verdict we have — which is why the automatic path reuses it. But "the
    system is confident" is not "the user is", and someone about to put their
    name on a document is entitled to make us look again.
    """
    from app.tailoring.tailor import reverify_application, tailor_for_application
    monkeypatch.setitem(sys.modules, "app.tailoring.grounding",
                        _fake_grounding_module(passed=True))
    tailor_for_application(app_id)
    assert (tmp_path / "tailored" / f"app_{app_id}" / "resume.md").exists(), (
        "the markdown must be kept — re-verifying a lossy .docx reconstruction "
        "would check a different document than the one we generated"
    )

    section = reverify_application(app_id)
    assert section["grounding_status"] == "passed"
    assert section["fabrications"] == []
    assert "reverified_at" in section
    assert _report(tmp_path, app_id)["reverified_at"] == section["reverified_at"]


def test_a_recheck_without_a_delivered_resume_says_so(app_id):
    """A clear refusal beats re-verifying nothing and reporting it clean."""
    from app.tailoring.tailor import reverify_application
    with pytest.raises(FileNotFoundError):
        reverify_application(app_id)


def test_zero_extractable_bullets_is_unverified_not_passed():
    """'We found nothing to check' is a fact about the extractor, not about the
    document. It used to return passed=True — the one thing that result is not
    allowed to mean."""
    import importlib
    g = importlib.import_module("app.tailoring.grounding")
    res = g.GroundingResult(passed=False, flagged_bullets=[], confidence_map={},
                            unverified=True)
    assert res.unverified is True and res.passed is False
