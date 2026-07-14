"""Two-tier scoring cascade: cheap Tier-1 prescore → Claude Tier-2 final.

Covers the pure prescore helpers, provider/backend selection, and the
reject-stamping that drains the unscored backlog (the egress + throughput fix).
The full run_matching cascade needs the embedding model (torch) so it is not
exercised here; these tests lock down the parts that don't.
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import delete

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Job, JobSource
from app.matching.reranker import (
    Reranker, _build_prescore_prompt, _parse_prescore, _prescore_system_prompt,
)


def _clean(session):
    session.exec(delete(Job))
    session.commit()


class _Profile:
    years_experience = 5
    key_skills = "Python, PyTorch, LLMs"
    target_roles = "ML Engineer"
    current_title = "ML Engineer"
    professional_summary = ""
    preferred_country = "United States"
    remote_ok = True
    requires_sponsorship = True
    work_authorization = "OPT"
    work_auth_status = ""
    visa_status = ""


def test_parse_prescore_tolerant_and_clamped():
    assert _parse_prescore('{"score": 82, "reason": "strong ML fit"}') == (82.0, "strong ML fit")
    # markdown fences tolerated
    assert _parse_prescore('```json\n{"score": 5, "reason": "x"}\n```')[0] == 5.0
    # clamped into 0..100
    assert _parse_prescore('{"score": 250}')[0] == 100.0
    assert _parse_prescore('{"score": -9, "reason": ""}')[0] == 0.0


def test_prescore_prompt_is_role_aware():
    sp = _prescore_system_prompt(_Profile())
    assert "ML Engineer" in sp and "United States" in sp
    assert "sponsorship" in sp.lower()          # candidate needs sponsorship → mentioned
    # legacy (no profile) still returns a usable rubric
    assert "first-pass" in _prescore_system_prompt(None)


def test_build_prescore_prompt_is_compact():
    job = Job(title="ML Eng", company="X", location="Remote", remote=True,
              description="Build LLMs " * 400, source=JobSource.GREENHOUSE,
              external_id="1", url="u")
    prompt = _build_prescore_prompt("resume " * 400, job)
    assert "ML Eng" in prompt
    # résumé + JD are truncated so the cheap pass stays fast/cheap
    assert len(prompt) < 6000


def test_prescore_backend_selection_prefers_configured_provider():
    r = Reranker.__new__(Reranker)
    r._profile = None
    r._feedback = ""
    r._anthropic_client = object()
    r._openai_client = object()

    settings.prescore_provider = "openai"
    assert [n for n, _ in r._prescore_backends()] == ["openai", "anthropic"]
    settings.prescore_provider = "anthropic"
    assert [n for n, _ in r._prescore_backends()][0] == "anthropic"

    assert r.has_prescore_backend() is True
    r._openai_client = None
    r._anthropic_client = None
    assert r.has_prescore_backend() is False


def test_persist_prescore_rejects_stamps_below_threshold():
    """Rejected jobs get their prescore stamped so they exit the unscored corpus,
    and the stamped score stays below the shortlist threshold so the re-shortlist
    query never resurrects them."""
    from app.matching.pipeline import _persist_prescore_rejects

    with get_session() as session:
        _clean(session)
        j = Job(title="Bakery Manager", company="Acme", location="Denver, CO",
                remote=False, description="unrelated", source=JobSource.GREENHOUSE,
                external_id="ez1", url="http://x/1", first_seen=datetime.utcnow())
        session.add(j)
        session.commit()
        jid = j.id

    _persist_prescore_rejects([(jid, 12.0, "unrelated field")])

    with get_session() as session:
        row = session.get(Job, jid)
        assert row.rerank_score == 12.0
        assert row.rerank_score < settings.shortlist_score_threshold
        assert "Pre-screened" in (row.rerank_reasoning or "")

    # empty input is a no-op (no crash)
    _persist_prescore_rejects([])
