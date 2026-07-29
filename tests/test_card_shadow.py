"""Shadow-path safety: with no LLM keys and no cards, the CardRace shadow hook
must be a silent no-op — it can never break or slow the authoritative scoring
path, and it must never write a partial row."""
from __future__ import annotations

from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import CardMatchShadow, Job, JobSource
from app.matching.card_shadow import shadow_card_match


def test_shadow_is_silent_noop_without_llm_clients(monkeypatch):
    init_db()
    monkeypatch.setattr(settings, "card_match_shadow", True)
    # Force the no-clients path regardless of environment keys.
    import app.matching.reranker as rr
    monkeypatch.setattr(rr, "_CLIENTS", (None, None, None))

    with get_session() as session:
        session.exec(delete(CardMatchShadow))
        job = Job(source=JobSource.GREENHOUSE, external_id="shadow-noop-1",
                  company="Acme", title="Backend Engineer",
                  url="https://example.com/j/1", user_id="u-shadow",
                  description="Python backend role")
        session.add(job)
        session.commit()
        jid = job.id

    # Must not raise, must not write anything.
    shadow_card_match(jid, "resume text", 72.0, {"skills": {"score": 70, "note": "x"}})
    with get_session() as session:
        rows = session.exec(select(CardMatchShadow)).all()
        assert rows == []


def test_shadow_respects_the_flag(monkeypatch):
    init_db()
    monkeypatch.setattr(settings, "card_match_shadow", False)
    called = {"n": 0}

    import app.matching.card_shadow as cs
    monkeypatch.setattr(cs, "_run", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    shadow_card_match(1, "resume", 50.0, None)
    assert called["n"] == 0


def test_missing_job_is_a_noop():
    init_db()
    shadow_card_match(999999999, "resume", 50.0, None)  # must not raise
