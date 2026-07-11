"""Chunked embedding_id backfill — avoids Supabase statement_timeout on big
indexes (the 'Job Discovery Failed: QueryCanceled … 6376 bound parameter sets'
notification)."""
from __future__ import annotations

from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Job, JobSource


def _mk_jobs(n):
    with get_session() as session:
        session.exec(delete(Job))
        session.commit()
        ids = []
        for i in range(n):
            j = Job(user_id=None, source=JobSource.GREENHOUSE, external_id=f"eb-{i}",
                    company="Co", title="Eng", url=f"https://x/{i}", description="d")
            session.add(j)
            session.commit()
            session.refresh(j)
            ids.append(j.id)
    return ids


def test_backfill_chunks_and_writes_all(monkeypatch):
    import app.matching.matcher as m
    monkeypatch.setattr(m, "_EMBED_UPDATE_BATCH", 100)
    ids = _mk_jobs(250)  # 3 batches at size 100

    m._bulk_set_embedding_ids(
        [{"id": jid, "embedding_id": idx} for idx, jid in enumerate(ids)]
    )
    with get_session() as session:
        rows = session.exec(select(Job).where(Job.id.in_(ids))).all()
    by_id = {r.id: r.embedding_id for r in rows}
    for idx, jid in enumerate(ids):
        assert by_id[jid] == idx, f"job {jid} embedding_id not backfilled"


def test_backfill_one_bad_batch_does_not_abort(monkeypatch):
    import app.matching.matcher as m
    monkeypatch.setattr(m, "_EMBED_UPDATE_BATCH", 100)
    ids = _mk_jobs(250)

    real_session = m.get_session
    calls = {"n": 0}

    class Boom(Exception):
        pass

    def flaky_session():
        calls["n"] += 1
        if calls["n"] == 2:   # blow up the second batch only
            class Ctx:
                def __enter__(self):
                    raise Boom("simulated statement timeout")
                def __exit__(self, *a):
                    return False
            return Ctx()
        return real_session()

    monkeypatch.setattr(m, "get_session", flaky_session)
    # Must not raise despite one failing batch.
    m._bulk_set_embedding_ids([{"id": jid, "embedding_id": idx} for idx, jid in enumerate(ids)])

    monkeypatch.setattr(m, "get_session", real_session)
    with get_session() as session:
        rows = session.exec(select(Job).where(Job.id.in_(ids))).all()
    done = sum(1 for r in rows if r.embedding_id is not None)
    # 250 rows in batches of 100 → [100, 100, 50]; batch 2 (100 rows) fails and
    # is skipped, batches 1 and 3 commit → 100 + 50 = 150 rows written.
    assert done == 150


def test_backfill_empty_is_noop():
    from app.matching.matcher import _bulk_set_embedding_ids
    _bulk_set_embedding_ids([])  # must not raise
