"""Discovery serialization (OOM guard) + orphaned-run clearing."""
from __future__ import annotations

import threading
import time
from datetime import datetime

from sqlmodel import delete, select

from app.common.discovery_lock import discovery_guard
from app.db.init_db import get_session
from app.db.models import DiscoveryRun


def test_guard_blocking_serializes():
    results = []

    def worker(name, hold):
        with discovery_guard(label=name) as ran:
            results.append((name, ran))
            if ran:
                time.sleep(hold)

    t = threading.Thread(target=worker, args=("first", 0.3))
    t.start()
    time.sleep(0.05)
    # Non-blocking attempt while held → skips.
    with discovery_guard(blocking=False, label="second") as ran:
        assert ran is False
    t.join()
    assert ("first", True) in results


def test_guard_released_after_use():
    with discovery_guard(blocking=False) as ran:
        assert ran is True
    # Lock is free again → next non-blocking acquire succeeds.
    with discovery_guard(blocking=False) as ran2:
        assert ran2 is True


def test_clear_orphaned_discovery_runs():
    from app.api.server import _clear_orphaned_discovery_runs
    with get_session() as session:
        session.exec(delete(DiscoveryRun))
        for st in ("discovering", "ranking", "first_results", "done", "error"):
            session.add(DiscoveryRun(user_id="local", started_at=datetime.utcnow(),
                                     status=st, source_counts="{}"))
        session.commit()

    _clear_orphaned_discovery_runs()

    with get_session() as session:
        rows = session.exec(select(DiscoveryRun)).all()
    by_status = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    # The 3 in-progress ones became 'error'; 'done' stays; original 'error' stays.
    assert "discovering" not in by_status
    assert "ranking" not in by_status
    assert "first_results" not in by_status
    assert by_status.get("done") == 1
    assert by_status.get("error") == 4  # 3 cleared + 1 original
