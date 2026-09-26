"""One export verdict, read by every door a tailored draft leaves through.

AUDIT 2026-09-25 (finding 5). The pre-download review reproduced
`unconfirmed_claims: ["Kafka"]` beside `download_blocked: false`: the flag
looked only at the application's status, so the review WARNED about an
invented Kafka project while the download, the preview and the extension's
fill-pack served the same document. `app/tailoring/export_gate.py` is now the
one decision; these tests pin that the review and the routes agree on it.

Synthetic data only — no real candidate's résumé appears in this repo.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import delete

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.tailoring import export_gate

_PREFIX = "xgate-"

MASTER = """# Alex Rivera
## Professional Experience
**Software Engineer** | Northwind Labs | Jan 2024 - Present | Remote
- Built a FastAPI service handling 40k requests per day with Postgres and Redis.
- Automated the CI/CD pipeline with GitHub Actions, cutting release time 30%.

## Skills
Languages: Python, SQL
Tools: Docker, FastAPI, AWS
"""

JD = """Backend Engineer

## Minimum Qualifications
- 2 years of experience with Python.
- 2 years of experience with Kafka.
"""

CLEAN = MASTER
FABRICATED = MASTER + "\n- Built a Kafka streaming platform processing 1M events per second.\n"


@pytest.fixture(autouse=True)
def _fresh_cache():
    export_gate.reset_state()
    yield
    export_gate.reset_state()


# ── the verdict itself ───────────────────────────────────────────────────────

def test_a_draft_claiming_an_unevidenced_skill_is_blocked():
    v = export_gate.evaluate(grounding_rejected=False, grounding_reason="",
                             master=MASTER, tailored=FABRICATED, jd=JD)
    assert v.blocked
    assert v.code == "unconfirmed_claims"
    assert "Kafka" in v.unconfirmed_claims
    assert "Kafka" in v.reason


def test_a_clean_draft_is_allowed_even_with_a_genuine_gap():
    """Missing Kafka is a gap to explain, not a reason to block the document."""
    v = export_gate.evaluate(grounding_rejected=False, grounding_reason="",
                             master=MASTER, tailored=CLEAN, jd=JD)
    assert v.allowed and v.code == "ok"


def test_grounding_rejection_blocks_with_its_own_reason():
    v = export_gate.evaluate(grounding_rejected=True, grounding_reason="Invented employer",
                             master=MASTER, tailored=CLEAN, jd=JD)
    assert v.blocked and v.code == "grounding_rejected"
    assert v.reason == "Invented employer"


def test_an_acronym_expansion_is_a_spelling_not_a_claim():
    """The tailor is told to expand acronyms; "Amazon Web Services" for a résumé
    that says AWS must not read as a fabrication and lock the document."""
    jd = "## Minimum Qualifications\n- 2 years of experience with Amazon Web Services.\n"
    draft = MASTER + "\n- Deployed on AWS (Amazon Web Services).\n"
    v = export_gate.evaluate(grounding_rejected=False, grounding_reason="",
                             master=MASTER, tailored=draft, jd=jd)
    assert v.allowed, v.reason


def test_the_verdict_is_bound_to_its_inputs():
    """A changed draft is a different key — nothing has to invalidate it."""
    a = export_gate.evaluate(grounding_rejected=False, grounding_reason="",
                             master=MASTER, tailored=FABRICATED, jd=JD)
    b = export_gate.evaluate(grounding_rejected=False, grounding_reason="",
                             master=MASTER, tailored=CLEAN, jd=JD)
    assert a.blocked and b.allowed
    assert a.basis != b.basis
    # …and the master résumé is part of the binding: once the candidate adds
    # where they really used Kafka, the same draft is judged afresh.
    master2 = MASTER + "- Ran the Kafka consumers behind the billing pipeline.\n"
    c = export_gate.evaluate(grounding_rejected=False, grounding_reason="",
                             master=master2, tailored=FABRICATED, jd=JD)
    assert c.allowed and c.basis != a.basis


# ── the routes agree ─────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


@pytest.fixture()
def fabricated_app(tmp_path, monkeypatch):
    draft = tmp_path / f"{_PREFIX}resume.md"
    draft.write_text(FABRICATED, encoding="utf-8")
    import app.matching.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_load_resume", lambda user_id=None: MASTER)
    with get_session() as s:
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id=_PREFIX + "1",
                  company="XGate Co", title="Backend Engineer", url="https://x/xgate",
                  description=JD, rerank_score=80, blended_score=80,
                  first_seen=datetime.utcnow() - timedelta(hours=1))
        s.add(job)
        s.commit()
        s.refresh(job)
        row = Application(user_id=None, job_id=job.id, status=ApplicationStatus.TAILORED,
                          tailored_resume_path=str(draft), tailored_at=datetime.utcnow())
        s.add(row)
        s.commit()
        s.refresh(row)
        ids = (job.id, row.id)
    yield ids[1]
    with get_session() as s:
        s.exec(delete(Application).where(Application.job_id == ids[0]))
        s.exec(delete(Job).where(Job.id == ids[0]))
        s.commit()


def test_review_and_download_agree_on_a_fabricated_draft(client, fabricated_app):
    """THE DEFECT: the review said Kafka was unconfirmed and download_blocked
    was false."""
    r = client.get(f"/application/{fabricated_app}/review").json()
    assert "Kafka" in r["unconfirmed_claims"]
    assert r["download_blocked"] is True
    assert "Kafka" in r["download_blocked_reason"]

    d = client.get(f"/application/{fabricated_app}/download-resume")
    assert d.status_code == 409
    assert "Kafka" in d.json()["detail"]


def test_details_withholds_the_fabricated_draft(client, fabricated_app):
    d = client.get(f"/application/{fabricated_app}/details").json()
    assert d["blocked"] is True
    assert "Kafka streaming platform" not in d["resume"]
    assert "Kafka" in d["blocked_reason"]


def test_every_export_route_reads_the_one_verdict():
    """Structural: a new door that hands the draft out must ask the gate."""
    import inspect
    from app.api import server
    for fn in (server.download_tailored_resume, server.application_pre_download_review,
               server.application_details, server.get_fill_pack,
               server.get_tailored_resume):
        assert "_export_verdict(" in inspect.getsource(fn), fn.__name__


def test_the_gate_needs_no_llm_or_network():
    src = Path(export_gate.__file__).read_text(encoding="utf-8")
    for banned in ("anthropic", "openai", "requests.", "httpx"):
        assert banned not in src
