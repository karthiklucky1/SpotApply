"""Ghost-detector repost check: tenant scoping + bounded read (the query that
was 93% of all DB time). Other users' copies of the same posting must never
count as duplicates; real same-pool reposts still flag."""
from __future__ import annotations

from sqlmodel import delete

from app.db.init_db import get_session, init_db
from app.db.models import Job, JobSource
from app.matching.filters.ghost_detector import score_ghost


def _job(session, user_id, ext, title="Backend Engineer", company="Acme"):
    j = Job(source=JobSource.GREENHOUSE, external_id=ext, company=company,
            title=title, url=f"https://x.test/{ext}", user_id=user_id,
            description="A real posting with a reasonable amount of text " * 20)
    session.add(j)
    session.commit()
    session.refresh(j)
    return j


def _cleanup():
    with get_session() as session:
        session.exec(delete(Job))
        session.commit()


def test_other_tenants_copies_are_not_duplicates():
    init_db()
    _cleanup()
    with get_session() as session:
        mine = _job(session, "u1", "j-1")
        for i in range(3):                       # same posting adopted by others
            _job(session, f"other-{i}", f"j-other-{i}")
        res = score_ghost(mine, session)
        assert not any(f.startswith("duplicate_postings") for f in res.flags)
    _cleanup()


def test_same_pool_reposts_still_flag():
    init_db()
    _cleanup()
    with get_session() as session:
        mine = _job(session, "u1", "j-1")
        _job(session, "u1", "j-2")               # same company+title, own pool
        _job(session, "u1", "j-3")
        res = score_ghost(mine, session)
        assert any(f.startswith("duplicate_postings") for f in res.flags)
    _cleanup()


def test_duplicate_read_is_bounded():
    init_db()
    _cleanup()
    with get_session() as session:
        mine = _job(session, "u1", "j-0")
        for i in range(25):                      # pathological repost storm
            _job(session, "u1", f"j-{i + 1}")
        res = score_ghost(mine, session)
        dup = [f for f in res.flags if f.startswith("duplicate_postings")]
        assert dup and int(dup[0].rsplit("_", 1)[1]) <= 10   # LIMIT 10 held
    _cleanup()
