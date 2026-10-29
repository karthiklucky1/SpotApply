"""The matching lane does not hard-filter on the door-match verdict (2026-10-28).

`classify_door` is advisory by its own contract ("it does NOT hide or filter
jobs"), and with JD text alone its axis reader called a posting "enterprise"
for "dental insurance" in its benefits and "elite-outlier" for a salary over
$200k. As a gate it stamped "Applied AI Engineer" and "Full Stack Developer
(Python, React.js)" Wrong Door for an AI developer — in this lane only, so a
job's fate depended on which lane found it. Location, years and seniority are
decided by the eligibility verdict, RuleFilter and the scorers.
"""
import inspect

import pytest
from unittest.mock import patch
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, Job, JobSource, UserProfile
from app.matching import pipeline


def test_the_matching_lane_never_asks_the_door_verdict():
    src = inspect.getsource(pipeline.run_matching)
    assert "classify_door(" not in src and 'f"Wrong Door:' not in src


def _seed():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("pipe-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
        if not prof:
            prof = UserProfile(user_id="local", first_name="Karthik", remote_ok=True,
                               open_to_relocation=False, location="Cincinnati, OH")
        s.add(prof)
        j = Job(source=JobSource.MANUAL, external_id="pipe-test-1",
                company="Acme Insurance", title="Applied AI Engineer", url="http://x",
                description="Python, FastAPI, LangChain. Benefits include dental insurance.")
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _cleanup():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("pipe-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_an_insurance_benefit_no_longer_stamps_wrong_door():
    pytest.importorskip("torch", reason="ML stack not installed in this environment")
    job_id = _seed()
    try:
        with patch("app.matching.matcher.Matcher.search_for_resume", return_value=[(job_id, 0.8)]), \
             patch("app.matching.matcher.Matcher.rebuild"), \
             patch("app.matching.pipeline._load_resume", return_value="Resume text"):
            pipeline.run_matching(user_id="local")
        with get_session() as s:
            job = s.get(Job, job_id)
            assert not (job.rerank_reasoning or "").startswith("Wrong Door")
    finally:
        _cleanup()
