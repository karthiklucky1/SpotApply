"""The promise-ordered queue only works if a queued job's prescore reaches the
database — and the ledger only bounds spend if no increment is lost.

`_user_queue` orders the whole queue by `Job.prescore`. But every other writer
of that column — `_stamp_job`, the matching lane's final stamp, the pulse fast
path — writes it TOGETHER with `rerank_score`, i.e. as the job LEAVES the
queue. A job prescored and KEPT (waiting in the queue, or whose final failed)
had its prescore only in `_prescore_memo`, so in the database it still read
NULL: to the sort it was "unknown", it re-entered the front of the queue every
cycle, and the ordering the budget depends on never saw a real number in
production. The original ordering test passed only because it seeded `prescore`
directly, which nothing in production does.

These run the real `_score_job_owned` against the real session, so the
persistence is exercised end to end rather than asserted about.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, UserUsage
from app.matching import finals_budget as fb
from app.strategy import scoring_lane as sl

USER = "u-persist"


def _make_job(external_id: str, minutes_old: int = 0) -> int:
    with get_session() as session:
        j = Job(user_id=USER, source=JobSource.GREENHOUSE, external_id=external_id,
                company="Acme", title="Software Engineer", location="Remote",
                url=f"https://e.com/{external_id}",
                description="Python backend engineering " * 30,
                first_seen=datetime.utcnow() - timedelta(minutes=minutes_old))
        session.add(j)
        session.commit()
        session.refresh(j)
        return j.id


def _cleanup() -> None:
    with get_session() as session:
        for j in session.exec(select(Job).where(Job.user_id == USER)).all():
            session.delete(j)
        session.commit()
    sl._prescore_memo.clear()


class _RK:
    """Tier-1 answers a fixed prescore; Tier-2 always fails."""

    def __init__(self, pre: float):
        self._pre = pre

    def prescore(self, resume, job):
        return (self._pre, "adjacent role")

    def score(self, resume, job, provider=None):
        raise RuntimeError("final unavailable")

    def has_dual(self):
        return False


def test_a_job_whose_final_never_returned_keeps_its_prescore_in_the_database():
    """45 clears the advance gate, so the job is not drained; Tier-2 then fails,
    so no verdict is written. It stays Queued — and its Tier-1 number must
    survive on the row, or next cycle it is "unknown" all over again."""
    jid = _make_job("wait-1")
    try:
        ctx = sl._Ctx("resume", _RK(45.0), True, fb.normal_gate(), fb.normal_gate())
        assert sl._score_job_owned(jid, ctx) is None
        with get_session() as session:
            row = session.get(Job, jid)
            assert row.rerank_score is None, "must stay Queued, not be drained"
            assert row.prescore == 45.0, "prescore must be persisted, not only memoized"
    finally:
        _cleanup()


def test_a_job_whose_final_failed_keeps_its_prescore_in_the_database(monkeypatch):
    jid = _make_job("fail-1")
    try:
        gate = fb.normal_gate()
        ctx = sl._Ctx("resume", _RK(70.0), True, gate, gate)
        monkeypatch.setattr(sl, "_transient_llm_stall", lambda: True)
        assert sl._score_job_owned(jid, ctx) is None
        with get_session() as session:
            row = session.get(Job, jid)
            assert row.rerank_score is None
            assert row.prescore == 70.0
    finally:
        _cleanup()


def test_the_persisted_prescore_is_what_the_queue_sorts_on():
    """End to end: next cycle the stronger waiting job outranks the weaker one
    even though it is older, and a never-prescored newcomer ranks AT THE GATE —
    between them, not ahead of them.

    The old fallback ranked an unknown at 100, so a job Tier-1 had never looked
    at pre-empted one it had scored 90. Unknown means "worth investigating",
    which is exactly the advance gate: above everything Tier-1 called a misfit,
    below everything it called a candidate. Freshness only breaks ties.
    """
    weak = _make_job("q-weak", minutes_old=5)
    strong = _make_job("q-strong", minutes_old=30)      # older: freshness alone would lose
    try:
        gate = fb.normal_gate()                          # 40
        sl._score_job_owned(weak, sl._Ctx("resume", _RK(45.0), True, gate, gate))
        sl._score_job_owned(strong, sl._Ctx("resume", _RK(54.0), True, gate, gate))
        unknown = _make_job("q-unknown", minutes_old=0)  # freshest, never prescored
        assert sl._user_queue(USER, 3) == [strong, weak, unknown]
    finally:
        _cleanup()


def test_an_unknown_outranks_a_job_tier1_judged_a_misfit():
    """The other half of ranking unknowns at the gate: a prescore BELOW it is
    evidence against, and must not sit ahead of a job nobody has judged."""
    misfit = _make_job("q-misfit", minutes_old=30)
    try:
        with get_session() as session:
            row = session.get(Job, misfit)
            row.prescore = fb.normal_gate() - 10.0        # judged, and found wanting
            session.add(row)
            session.commit()
        unknown = _make_job("q-unjudged", minutes_old=5)
        assert sl._user_queue(USER, 2) == [unknown, misfit]
    finally:
        _cleanup()


# ── the matching lane spends the same budget as everyone else ────────────────

def test_the_matching_lane_consults_the_finals_allowance():
    """run_matching recorded its finals to the ledger but never CONSULTED it, so
    the 5-minute lane could spend straight past the daily burst and the weekly
    budget on its own — which made both ceilings advisory rather than hard.

    Source-level, because the pass itself needs FAISS + the ML stack: what must
    never regress is that the lane asks for an allowance and caps Tier-2 by it.
    """
    import ast
    import inspect
    from app.matching import pipeline

    tree = ast.parse(inspect.getsource(pipeline))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_matching")
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_finals_allowance" in calls, (
        "run_matching must consult the per-user adaptive budget before spending "
        "Tier-2 — otherwise burst and weekly are not ceilings at all"
    )
    src = inspect.getsource(pipeline)
    assert "_tier2_cap = min(settings.llm_rerank_cap, _allow.n)" in src, (
        "the Tier-2 cap must be the smaller of the per-pass ceiling and what the "
        "user's budget still allows"
    )


def test_prescores_cut_by_the_tier2_cap_are_kept_not_thrown_away():
    """WHERE THE QUEUE'S RANKING ACTUALLY COMES FROM, and it used to be dropped.

    The scoring lane scores everything it prescores, so it ranks nothing. The
    matching lane is the spreader: Tier-1 prescores up to `prescore_cap` (600)
    candidates and Tier-2 buys at most `llm_rerank_cap`/allowance (~100) of
    them. The ~500 cut by that ceiling stay Queued and each already HAS a real
    Tier-1 number — but the cut simply truncated the list, so those numbers were
    discarded and the rows kept `prescore = NULL`.

    Every other writer of that column sets it as a job LEAVES the queue, so the
    whole waiting corpus read NULL, tied at the gate in `_user_queue`'s
    ORDER BY, and the promise ordering silently degraded to arrival order —
    the exact thing it was written to replace, and the thing the finals budget
    now depends on being real.

    Source-level: a real pass needs FAISS and the ML stack.
    """
    import inspect
    from app.matching import pipeline

    src = inspect.getsource(pipeline.run_matching)
    cut = src.split("_tier2_cap = min(", 1)[1].split("if to_rerank:", 1)[0]
    assert "to_rerank[_tier2_cap:]" in cut and "prescore_kept.append" in cut, (
        "the candidates cut by the Tier-2 cap must have their prescores kept — "
        "otherwise the promise-ordered queue has nothing to sort on"
    )


def test_kept_prescores_are_written_without_leaving_the_queue():
    """_persist_kept_prescores writes `prescore` and NOTHING else: the job stays
    Queued (rerank_score NULL) but is now sortable by promise. The reject path,
    by contrast, stamps rerank_score and removes the job from the queue."""
    from app.matching.pipeline import _persist_kept_prescores

    jid = _make_job("kept-1")
    try:
        _persist_kept_prescores([(jid, 47.0)])
        with get_session() as session:
            row = session.get(Job, jid)
            assert row.prescore == 47.0
            assert row.rerank_score is None, "must stay in the queue"
            assert row.rerank_reasoning is None, "not a drain: no verdict written"
    finally:
        _cleanup()


# ── the ledger's first-final-of-the-day insert race ──────────────────────────

def test_a_lost_insert_race_does_not_lose_the_increment(monkeypatch):
    """Two workers finish a user's first finals of the day together: both see
    rowcount 0 on the UPDATE and both INSERT. uq_user_usage_date lets one win;
    the loser must retry the UPDATE, not drop its increment on the floor.

    Replayed deterministically — SQLite's single writer would otherwise
    serialize the two and never produce the conflict.
    """
    from sqlalchemy.exc import IntegrityError
    import app.db.init_db as _init_db

    uid, day = "u-race", date(2026, 8, 21)

    def _clean():
        with get_session() as session:
            for r in session.exec(select(UserUsage).where(UserUsage.user_id == uid)).all():
                session.delete(r)
            session.commit()

    _clean()
    real_get_session = _init_db.get_session
    state = {"raced": False, "calls": 0}

    class _LoserSession:
        """Attempt 1: the UPDATE runs for real (0 rows) and the INSERT loses."""

        def __init__(self, inner):
            self._cm = inner

        def __enter__(self):
            self._s = self._cm.__enter__()
            return self

        def __exit__(self, *a):
            return self._cm.__exit__(*a)

        def execute(self, *a, **k):
            return self._s.execute(*a, **k)

        def add(self, obj):
            self._s.rollback()                      # release SQLite's write lock
            with real_get_session() as winner:      # the other worker takes the row
                winner.add(UserUsage(user_id=uid, usage_date=day,
                                     week_start=fb._week_start(day),
                                     finals_count=1, finals_hits=0))
                winner.commit()
            state["raced"] = True
            raise IntegrityError("INSERT INTO user_usage", {},
                                 Exception("UNIQUE constraint failed"))

        def commit(self):
            self._s.commit()

    def _fake_get_session():
        state["calls"] += 1
        inner = real_get_session()
        return _LoserSession(inner) if state["calls"] == 1 else inner

    monkeypatch.setattr(_init_db, "get_session", _fake_get_session)
    try:
        fb._write(uid, day, finals=1)
        assert state["raced"], "the race was never exercised"
        assert state["calls"] == 2, "the loser must retry exactly once"
        monkeypatch.undo()
        assert fb._read_day(uid, day) == (2, 0), "winner's 1 + the loser's retried 1"
    finally:
        monkeypatch.undo()
        _clean()
