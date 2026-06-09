import pytest
from sqlmodel import select
from app.db.models import Job, JobSource
from app.db.init_db import get_session
from app.discovery.pipeline import _cross_source_slug, _upsert, RawJob

def test_cross_source_slug_normalization():
    slug1 = _cross_source_slug("Google Inc.", "Sr. ML Engineer", "New York, NY")
    slug2 = _cross_source_slug("google llc", "Senior ML Engineer", "new york ny")
    assert slug1 == slug2

def test_cross_source_dedup_upsert():
    # Clean previous test entries
    with get_session() as session:
        jobs = session.exec(
            select(Job).where(Job.external_id.like("test-dedup-%"))
        ).all()
        for j in jobs:
            session.delete(j)
        session.commit()
        
    job1 = RawJob(
        source="greenhouse",
        external_id="test-dedup-1",
        company="Anthropic Inc.",
        title="Sr. AI Safety Researcher",
        location="San Francisco, CA",
        remote=True,
        url="http://greenhouse.com/anthropic/job1",
        description="Help us make AI safe.",
        posted_at=None
    )
    
    job2 = RawJob(
        source="lever",
        external_id="test-dedup-2",
        company="anthropic",
        title="Senior AI Safety Researcher",
        location="san francisco ca",
        remote=True,
        url="http://lever.co/anthropic/job2",
        description="Help us make AI safe.",
        posted_at=None
    )
    
    # Run upsert for job1 (should insert)
    inserted1 = _upsert([job1])
    assert inserted1 == 1
    
    # Run upsert for job2 (should be deduped since it has same normalized details)
    inserted2 = _upsert([job2])
    assert inserted2 == 0
