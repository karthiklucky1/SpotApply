import pytest
from app.db.models import Job, JobSource
from app.db.init_db import get_session
from app.analytics.funnel import FunnelTracker

def test_funnel_tracker_record():
    # Insert a dummy job
    with get_session() as session:
        job = Job(
            source=JobSource.GREENHOUSE,
            external_id="funnel-test-1",
            company="FunnelCo",
            title="Funnel Engineer",
            url="http://funnel.com",
            description="Testing funnel events"
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    # Record some events
    FunnelTracker.record(job_id, "discovered", True)
    FunnelTracker.record(job_id, "rule_filtered", False, reason="Location blocker")
    FunnelTracker.record(job_id, "rule_filtered", True)
    FunnelTracker.record(job_id, "embedding_filtered", True)
    FunnelTracker.record(job_id, "scored", True, metadata={"score": 85})

    # Retrieve summary
    summary = FunnelTracker.get_summary(days=1)
    
    assert summary["discovered"] >= 1
    assert summary["rule_filtered"]["passed"] >= 1
    assert summary["rule_filtered"]["failed"] >= 1
    assert summary["embedding_filtered"]["passed"] >= 1
    assert summary["scored"] >= 1
    assert "greenhouse" in summary["sources"]
