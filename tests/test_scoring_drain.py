"""The Tier-1 drain must survive the finals budget running out.

Production, Aug 2026: with one PRO user the lane bought ~70 finals/day and
~1,200 prescores/day against ~19,000 new jobs/day, and the scored feed's median
age reached 34.8 days. Root cause: a user whose finals allowance hit 0 was
dropped from the cycle ENTIRELY (`if allow.n <= 0: continue`), so exhausting
the finals budget also halted the ~$0.0002 Tier-1 drain — the queue slice was
keyed to remaining finals, and the backlog froze for the rest of the day.

The drain-only slice (settings.scoring_drain_cap) keeps Tier-1 running with a
closed finals budget: misfits are stamped out for good, real candidates keep
their prescore and stay Queued, and — the invariant with money attached — a
drain slice NEVER buys a Tier-2 final.
"""
from __future__ import annotations

import pytest
from sqlmodel import delete

from app.db.init_db import get_session
from app.db.models import Job, JobSource
from app.strategy import scoring_lane
from app.strategy.scoring_lane import _Ctx, _score_job_owned, _user_queue


@pytest.fixture(autouse=True)
def _clean():
    with get_session() as session:
        session.exec(delete(Job))
        session.commit()
    scoring_lane._prescore_memo.clear()
    yield
    with get_session() as session:
        session.exec(delete(Job))
        session.commit()
    scoring_lane._prescore_memo.clear()


def _seed(uid="u-drain", n=3, prescore=None, ext_prefix="d"):
    ids = []
    with get_session() as session:
        for i in range(n):
            j = Job(user_id=uid, source=JobSource.GREENHOUSE,
                    external_id=f"{ext_prefix}{i}",
                    company=f"Co{i}", title="Backend Engineer",
                    url=f"https://x.co/{i}", description="d", prescore=prescore)
            session.add(j)
            session.commit()
            session.refresh(j)
            ids.append(j.id)
    return ids


class _FakeReranker:
    """Prescore returns a fixed value; any Tier-2 call is the test failing."""
    def __init__(self, pre_value):
        self.pre_value = pre_value
        self.final_calls = 0

    def prescore(self, resume, job):
        return (self.pre_value, "fake tier-1")

    def score(self, resume, job, provider=None):
        self.final_calls += 1
        raise AssertionError("drain-only slice bought a Tier-2 final")

    def has_prescore_backend(self):
        return True


def test_drain_slice_stamps_misfits_without_a_final(monkeypatch):
    ids = _seed()
    rr = _FakeReranker(pre_value=20.0)   # below the drain gate
    ctx = _Ctx("resume text", rr, use_prescore=True, gate=40, spend_gate=40,
               drain_only=True)
    monkeypatch.setattr(scoring_lane, "score_ghost", None, raising=False)
    out = _score_job_owned(ids[0], ctx)
    assert out is not None and out[0] == "drained"
    with get_session() as session:
        job = session.get(Job, ids[0])
    assert job.rerank_score is not None       # stamped out of the queue for good
    assert job.prescore == 20.0
    assert rr.final_calls == 0


def test_drain_slice_keeps_a_real_candidate_queued_with_its_prescore():
    ids = _seed()
    rr = _FakeReranker(pre_value=72.0)   # a genuinely promising job
    ctx = _Ctx("resume text", rr, use_prescore=True, gate=40, spend_gate=40,
               drain_only=True)
    out = _score_job_owned(ids[0], ctx)
    assert out is not None and out[0] == "prescored"
    with get_session() as session:
        job = session.get(Job, ids[0])
    assert job.rerank_score is None           # still Queued for tomorrow's budget
    assert job.prescore == 72.0               # promise-ordered queue sees it
    assert rr.final_calls == 0


def test_drain_slice_never_reaches_tier2_when_prescore_fails():
    ids = _seed()

    class _NoPrescore(_FakeReranker):
        def prescore(self, resume, job):
            return None                        # backend hiccup

    rr = _NoPrescore(pre_value=0)
    ctx = _Ctx("resume text", rr, use_prescore=True, gate=40, spend_gate=40,
               drain_only=True)
    out = _score_job_owned(ids[0], ctx)
    assert out is None                         # stays Queued, no money spent
    assert rr.final_calls == 0


def test_drain_queue_only_picks_unprescored_jobs():
    """Re-picking already-prescored jobs every 90s would re-pay and re-write
    the same number forever — the drain works through the unknowns."""
    uid = "u-queue"
    _seed(uid=uid, n=2, prescore=66.0, ext_prefix="known")   # already known
    unknown = _seed(uid=uid, n=2, prescore=None, ext_prefix="unk")
    jids = _user_queue(uid, cap=10, only_unprescored=True)
    assert set(jids) == set(unknown)


def test_normal_path_is_unchanged_without_the_flag():
    ids = _seed()
    rr = _FakeReranker(pre_value=72.0)
    calls = []
    rr.score = lambda resume, job, provider=None: (calls.append(1) or (80.0, "fit", [], {}))
    ctx = _Ctx("resume text", rr, use_prescore=True, gate=40, spend_gate=40)
    out = _score_job_owned(ids[0], ctx)
    assert out is not None and out[0] == "scored"
    assert calls, "with budget open, a promising job still buys a final"
