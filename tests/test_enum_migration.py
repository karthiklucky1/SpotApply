"""Postgres enum label migration + stats resilience.

SQLAlchemy stores/compares Enum columns by member NAME ('RECRUITEE'), so
that's what the migration must add — a lowercase value label left by an older
buggy migration must not suppress it. And /api/stats must not 500 even when
enum labels lag the code."""
from sqlmodel import select

from app.db.init_db import _missing_enum_labels, get_session
from app.db.models import Job, JobSource


def test_missing_labels_are_member_names():
    existing = {"GREENHOUSE", "LEVER"}
    missing = _missing_enum_labels(existing, JobSource)
    assert "RECRUITEE" in missing
    assert "PERSONIO" in missing
    assert "GREENHOUSE" not in missing
    # Names, never lowercase values.
    assert all(label == label.upper() for label in missing)


def test_value_label_does_not_suppress_name_label():
    # The broken migration added 'recruitee' (the VALUE); the NAME must still
    # be reported missing, otherwise queries keep failing.
    existing = {name for name in JobSource.__members__} - {"RECRUITEE"}
    existing.add("recruitee")
    missing = _missing_enum_labels(existing, JobSource)
    assert missing == ["RECRUITEE"]


def test_all_present_adds_nothing():
    existing = set(JobSource.__members__)
    assert _missing_enum_labels(existing, JobSource) == []


def test_api_stats_source_counts_via_group_by():
    from fastapi.testclient import TestClient
    import app.api.server as server

    with get_session() as s:
        if not s.exec(select(Job).where(Job.external_id == "stats-src-1")).first():
            s.add(Job(source=JobSource.GREENHOUSE, external_id="stats-src-1",
                      company="StatsCo", title="Engineer", url="http://x/s1",
                      description="d"))
            s.commit()

    client = TestClient(server.app)
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["sources"].get("greenhouse", 0) >= 1

    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id == "stats-src-1")).all():
            s.delete(j)
        s.commit()
