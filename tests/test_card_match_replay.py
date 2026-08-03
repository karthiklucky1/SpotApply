"""The offline replay must be trustworthy before anyone reads a number off it.

Re-scoring the shadow ledger is the only way to measure a g() change without
waiting for new Claude finals, so the failure that matters is a SILENT one:
skipping rows and still printing a confident percentage. These pin that the
denominator is honest, that nothing is written, and that no model is called.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import CardMatchShadow, JobCardRow, UserCardRow
from scripts import card_match_replay

USER = "replay-user"

JOB_CARD = {
    "role": "AI Backend Engineer",
    "capabilities": [
        {"name": "LLM application development", "importance": 0.95,
         "evidence_needed": ["langchain", "prompt engineering"]},
        {"name": "Python backend engineering", "importance": 0.9,
         "evidence_needed": ["python", "fastapi"]},
    ],
    "years_min": 3,
    "_version": 1,
}
USER_CARD = {
    "skills": [
        {"name": "python", "evidence": 0.95},
        {"name": "fastapi", "evidence": 0.85},
        {"name": "langchain", "evidence": 0.85},
        {"name": "prompt engineering", "evidence": 0.9},
    ],
    "years_experience": 4,
    "_version": 1,
}


def _seed(session, *, n=3, card_key="hash:abc", with_job_card=True,
          with_user_card=True, stored_g=10.0):
    if with_job_card:
        session.add(JobCardRow(card_key=card_key, version=1,
                               payload=json.dumps(JOB_CARD)))
    if with_user_card:
        session.add(UserCardRow(user_id=USER, version=1, resume_hash="h",
                                payload=json.dumps(USER_CARD),
                                updated_at=datetime(2026, 1, 1)))
    for i in range(n):
        session.add(CardMatchShadow(
            job_id=1000 + i, user_id=USER, llm_score=78.0,
            direct_score=stored_g, expanded_score=stored_g, spread=0.0,
            band="band", card_key=card_key,
            breakdown=json.dumps({"skills": {"score": stored_g}}),
            created_at=datetime(2026, 7, 30) + timedelta(hours=i)))
    session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with get_session() as s:
        for model in (CardMatchShadow, JobCardRow, UserCardRow):
            for row in s.exec(select(model)).all():
                s.delete(row)
        s.commit()
    yield


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["card_match_replay"] + argv)
    return card_match_replay.main()


def test_replay_recovers_the_pair_and_rescores_it(monkeypatch, capsys):
    """The whole point: stored g() was wrong, today's g() is not, same cards."""
    with get_session() as s:
        _seed(s, n=3, stored_g=10.0)
    assert _run(monkeypatch, []) == 0
    out = capsys.readouterr().out
    assert "3 of 3 rows re-scored" in out
    assert "decision agreement" in out
    # stored g()=10 vs Claude 78 disagreed at the bar; the rehydrated pair
    # covers every capability, so the replay must move it up.
    assert "toward Claude 3" in out


def test_missing_cards_are_counted_not_hidden(monkeypatch, capsys):
    """A row whose card is gone must shrink the denominator VISIBLY."""
    with get_session() as s:
        _seed(s, n=2, with_job_card=False)
    assert _run(monkeypatch, []) == 1
    out = capsys.readouterr().out
    assert "replayed none" in out
    assert "no job card" in out


def test_partial_coverage_reports_the_real_denominator(monkeypatch, capsys):
    with get_session() as s:
        _seed(s, n=2, card_key="hash:abc")
        _seed(s, n=1, card_key="hash:missing", with_job_card=False,
              with_user_card=False)
    assert _run(monkeypatch, []) == 0
    out = capsys.readouterr().out
    assert "2 of 3 rows re-scored" in out
    assert "not replayed" in out


def test_replay_writes_nothing(monkeypatch):
    """Read-only is a promise, not a comment."""
    with get_session() as s:
        _seed(s, n=3)
    with get_session() as s:
        before = [(r.id, r.expanded_score, r.spread, r.band)
                  for r in s.exec(select(CardMatchShadow)).all()]
    _run(monkeypatch, [])
    with get_session() as s:
        after = [(r.id, r.expanded_score, r.spread, r.band)
                 for r in s.exec(select(CardMatchShadow)).all()]
    assert before == after


def test_replay_calls_no_model(monkeypatch):
    """Mint/compile would spend money and re-derive the card we are replaying."""
    import app.matching.cards as cards

    def boom(*a, **k):
        raise AssertionError("replay must never mint or compile a card")

    monkeypatch.setattr(cards, "mint_job_card", boom)
    monkeypatch.setattr(cards, "compile_user_card", boom)
    monkeypatch.setattr(cards, "_haiku_json", boom)
    with get_session() as s:
        _seed(s, n=2)
    assert _run(monkeypatch, []) == 0


def test_since_filters_and_rejects_bad_dates(monkeypatch, capsys):
    with get_session() as s:
        _seed(s, n=3)
    assert _run(monkeypatch, ["--since", "2026-13-99"]) == 2
    assert "not YYYY-MM-DD" in capsys.readouterr().out
    assert _run(monkeypatch, ["--since", "2026-09-01"]) == 1
    assert "nothing to replay" in capsys.readouterr().out


def test_recompiled_user_card_is_flagged_as_drift(monkeypatch, capsys):
    """A résumé edited after the window makes movement ambiguous — say so."""
    with get_session() as s:
        _seed(s, n=2)
        row = s.exec(select(UserCardRow)).first()
        row.updated_at = datetime(2026, 8, 2)     # after the shadow window
        s.add(row)
        s.commit()
    assert _run(monkeypatch, []) == 0
    out = capsys.readouterr().out
    assert "CAVEAT" in out and "recompiled after" in out


# ── flip autopsy ─────────────────────────────────────────────────────────────
#
# The summary cannot tell two mechanisms apart, and they need opposite fixes:
# a row can cross the bar because the resolver over-credits skills, or because
# skills<=BLOCKER_FLOOR was tripping the hard-blocker cap and nothing about the
# row was ever really evaluated. Acting on the wrong one suppresses the good
# flips along with the bad, so --flips has to name the mechanism, not guess it.

def _flip_seed(session, *, stored_g, stored_skills, llm, job_id=2001):
    session.add(JobCardRow(card_key="hash:flip", version=1,
                           payload=json.dumps(JOB_CARD)))
    session.add(UserCardRow(user_id=USER, version=1, resume_hash="h",
                            payload=json.dumps(USER_CARD),
                            updated_at=datetime(2026, 1, 1)))
    session.add(CardMatchShadow(
        job_id=job_id, user_id=USER, llm_score=llm,
        direct_score=stored_g, expanded_score=stored_g, spread=0.0,
        band="band", card_key="hash:flip",
        breakdown=json.dumps({"skills": {"score": stored_skills, "note": ""},
                              "experience": {"score": 80.0, "note": ""},
                              "location": {"score": 90.0, "note": ""},
                              "work_auth": {"score": 90.0, "note": ""}}),
        created_at=datetime(2026, 7, 30)))
    session.commit()


def test_autopsy_names_blocker_release(monkeypatch, capsys):
    """Stored g() sitting exactly on the cap with skills under the floor is the
    signature of a row the cap rejected, not a row g() judged."""
    with get_session() as s:
        _flip_seed(s, stored_g=25.0, stored_skills=0.0, llm=20.0)
    assert _run(monkeypatch, ["--flips"]) == 0
    out = capsys.readouterr().out
    assert "flip autopsy" in out
    assert "BROKE (away from Claude)" in out
    assert "blocker-release" in out
    assert "skills-over-credit" not in out.split("BROKE")[1].split("FIXED")[0]


def test_autopsy_names_skills_over_credit(monkeypatch, capsys):
    """A row that was scored on its merits and still moved over the bar is the
    other mechanism, and must not be filed under blocker-release."""
    with get_session() as s:
        _flip_seed(s, stored_g=48.0, stored_skills=30.0, llm=20.0)
    assert _run(monkeypatch, ["--flips"]) == 0
    out = capsys.readouterr().out
    assert "skills-over-credit" in out
    assert "blocker-release" not in out


def test_autopsy_is_opt_in(monkeypatch, capsys):
    with get_session() as s:
        _flip_seed(s, stored_g=25.0, stored_skills=0.0, llm=20.0)
    assert _run(monkeypatch, []) == 0
    assert "flip autopsy" not in capsys.readouterr().out


def test_autopsy_survives_a_missing_breakdown(monkeypatch, capsys):
    """Older rows may have no breakdown JSON — that must not crash the autopsy."""
    with get_session() as s:
        _flip_seed(s, stored_g=25.0, stored_skills=0.0, llm=20.0)
        row = s.exec(select(CardMatchShadow)).first()
        row.breakdown = None
        s.add(row)
        s.commit()
    assert _run(monkeypatch, ["--flips"]) == 0
    out = capsys.readouterr().out
    assert "flip autopsy" in out
    assert "—" in out                      # missing factors render, not explode
