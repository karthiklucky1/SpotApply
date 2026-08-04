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


# ── the missing denominator (audit B3) ───────────────────────────────────────
#
# `n` counted successes only, and three early returns produced no row and no log
# above DEBUG. A systematic mint failure therefore just made the ledger fill
# slowly, and every agreement number computed from it was biased by whatever the
# failures correlated with — with no way to notice.


def _reset_shadow_stats():
    from app.matching import card_shadow as cs
    with cs._stats_lock:
        for k in cs._stats:
            cs._stats[k] = 0.0 if isinstance(cs._stats[k], float) else 0
        cs._failed.clear()


def test_every_attempt_is_counted_even_when_no_row_is_written(monkeypatch):
    """The denominator: attempts must be counted before anything can fail."""
    from app.matching import card_shadow as cs
    _reset_shadow_stats()
    monkeypatch.setattr(cs.settings, "card_match_shadow", True)
    cs.shadow_card_match(-1, "resume", 70.0, None)     # job id that cannot exist
    s = cs.shadow_stats()
    assert s["attempted"] == 1, "a final that produced no row was not counted"
    assert s["n"] == 0
    assert s["failed"].get("job_missing") == 1, f"reason not recorded: {s['failed']}"


def test_each_miss_reason_is_named_separately(monkeypatch):
    """'no row' has four causes needing four different fixes — a single counter
    would have said 'something is wrong' and nothing else."""
    from app.matching import card_shadow as cs
    _reset_shadow_stats()
    monkeypatch.setattr(cs.settings, "card_match_shadow", True)
    monkeypatch.setattr(cs, "_run", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    cs.shadow_card_match(1, "r", 70.0, None)
    assert cs.shadow_stats()["failed"].get("RuntimeError") == 1


def test_disabled_shadow_counts_nothing(monkeypatch):
    from app.matching import card_shadow as cs
    _reset_shadow_stats()
    monkeypatch.setattr(cs.settings, "card_match_shadow", False)
    cs.shadow_card_match(1, "r", 70.0, None)
    assert cs.shadow_stats()["attempted"] == 0
