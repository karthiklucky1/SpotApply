"""Retention purge must delete ONLY closed, old, unreferenced jobs — never an
applied job, a recent job, or an open one (app/strategy/job_retention.py)."""
from datetime import datetime, timedelta

import pytest
from sqlmodel import delete

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.strategy.job_retention import purge_old_closed_jobs

OLD = datetime.utcnow() - timedelta(days=90)
RECENT = datetime.utcnow() - timedelta(days=5)


def _mk_job(session, ext, closed, first_seen):
    j = Job(external_id=ext, source=JobSource.GREENHOUSE, title="Engineer",
            company="Acme", url=f"https://x/{ext}", is_closed=closed, first_seen=first_seen)
    session.add(j)
    session.commit()
    session.refresh(j)
    return j.id


@pytest.fixture
def scenario():
    with get_session() as s:
        ids = {
            "closed_old": _mk_job(s, "ret-co", True, OLD),        # should DELETE
            "closed_old_applied": _mk_job(s, "ret-coa", True, OLD),  # keep (has app)
            "closed_recent": _mk_job(s, "ret-cr", True, RECENT),  # keep (too new)
            "open_old": _mk_job(s, "ret-oo", False, OLD),         # keep (not closed)
        }
        # Hermetic: clear any stray applications other tests left on these ids,
        # then attach exactly one to closed_old_applied.
        s.exec(delete(Application).where(Application.job_id.in_(list(ids.values()))))
        s.add(Application(job_id=ids["closed_old_applied"], status=ApplicationStatus.SUBMITTED))
        s.commit()
    yield ids
    with get_session() as s:
        s.exec(delete(Application).where(Application.job_id.in_(list(ids.values()))))
        s.exec(delete(Job).where(Job.id.in_(list(ids.values()))))
        s.commit()


def _exists(jid):
    with get_session() as s:
        return s.get(Job, jid) is not None


def test_purge_deletes_only_dead_jobs(scenario):
    ids = scenario
    n = purge_old_closed_jobs(days=60)
    assert n >= 1
    assert not _exists(ids["closed_old"])            # deleted
    assert _exists(ids["closed_old_applied"])        # kept — has an application
    assert _exists(ids["closed_recent"])             # kept — too new
    assert _exists(ids["open_old"])                  # kept — still open


def test_purge_disabled_when_days_zero(scenario):
    assert purge_old_closed_jobs(days=0) == 0
    assert _exists(scenario["closed_old"])           # nothing deleted


# ── The funnel's FK blocked every purge ──────────────────────────────────────

def test_purge_releases_funnel_events_before_deleting(monkeypatch):
    """funnel_events.job_id references job.id.

    Deleting a job the funnel still points at raises
    `funnel_events_job_id_fkey` on Postgres and rolls back the WHOLE batch, so
    retention silently reclaimed nothing while the table kept growing. SQLite
    does not enforce FKs by default, which is why the suite never saw it — this
    test turns enforcement on.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import event
    from sqlmodel import select
    from app.db.init_db import engine, get_session
    from app.db.models import FunnelEvent, Job, JobSource
    from app.strategy.job_retention import purge_old_closed_jobs

    if engine.dialect.name != "sqlite":
        pytest.skip("FK-enforcement rehearsal is SQLite-specific")

    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(engine, "connect", _fk_on)
    engine.dispose()
    try:
        old = datetime.utcnow() - timedelta(days=120)
        with get_session() as s:
            j = Job(source=JobSource.GREENHOUSE, external_id="fk-purge-1",
                    company="C", title="T", url="http://x/fk1", description="d",
                    is_closed=True, first_seen=old)
            s.add(j); s.commit(); s.refresh(j)
            s.add(FunnelEvent(job_id=j.id, stage="fk-purge-stage", passed=True))
            s.commit()
            jid = j.id

        assert purge_old_closed_jobs(days=60) >= 1

        with get_session() as s:
            assert s.exec(select(Job).where(Job.id == jid)).first() is None, (
                "the job must actually be deleted — the FK violation used to "
                "roll the whole batch back")
            ev = s.exec(select(FunnelEvent).where(
                FunnelEvent.stage == "fk-purge-stage")).first()
            assert ev is not None, "the funnel event is kept (counted by stage)"
            assert ev.job_id is None, "it is merely detached from the dead job"
            s.delete(ev); s.commit()
    finally:
        event.remove(engine, "connect", _fk_on)
        engine.dispose()
