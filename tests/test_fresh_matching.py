"""Tests for the freshness fix in run_matching:

1. Already-scored jobs above the shortlist threshold are (re)shortlisted via a
   direct DB query — they no longer need to win a retrieval slot.
2. Unscored (fresh) jobs coming out of retrieval get LLM-scored and
   shortlisted; jobs scored mid-run are skipped safely.
"""
from unittest.mock import patch

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile
from app.matching.pipeline import run_matching, _reshortlist_scored_jobs


def _seed_profile():
    with get_session() as s:
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
        if not prof:
            prof = UserProfile(user_id="local", first_name="T", remote_ok=True,
                               location="Cincinnati, OH")
            s.add(prof)
            s.commit()


def _mk_job(ext_id: str, title: str, rerank_score=None, company="FreshCo") -> int:
    with get_session() as s:
        j = Job(source=JobSource.MANUAL, external_id=ext_id, company=company,
                title=title, url="http://x", remote=True,
                description="Remote. Python, ML, LLM role.",
                rerank_score=rerank_score,
                rerank_reasoning="pre-scored" if rerank_score is not None else None)
        s.add(j)
        s.commit()
        s.refresh(j)
        # Earlier tests can leave orphan Application rows whose job_id gets
        # recycled by SQLite onto this fresh job — drop them so this test
        # only sees its own state.
        for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
            s.delete(a)
        s.commit()
        return j.id


def _cleanup(prefix: str):
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like(f"{prefix}%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_reshortlist_scored_jobs_creates_application():
    _seed_profile()
    _cleanup("fresh-test-")
    scored_id = _mk_job("fresh-test-scored", "ML Engineer", rerank_score=80.0)
    low_id = _mk_job("fresh-test-low", "ML Engineer II", rerank_score=20.0,
                     company="LowCo")
    try:
        ids, count = _reshortlist_scored_jobs(None, today_count=0)
        assert scored_id in ids
        assert low_id not in ids
        assert count == len(ids)
        with get_session() as s:
            app = s.exec(select(Application).where(Application.job_id == scored_id)).first()
            assert app is not None and app.status == ApplicationStatus.SHORTLISTED

        # Second pass must be a no-op (application exists now).
        ids2, _ = _reshortlist_scored_jobs(None, today_count=0)
        assert scored_id not in ids2
    finally:
        _cleanup("fresh-test-")


def test_run_matching_scores_fresh_job_and_reshortlists_scored():
    _seed_profile()
    _cleanup("fresh-run-")
    fresh_id = _mk_job("fresh-run-new", "AI Engineer")
    scored_id = _mk_job("fresh-run-scored", "LLM Engineer", rerank_score=75.0,
                        company="ScoredCo")
    try:
        with patch("app.matching.matcher.Matcher.__init__", return_value=None), \
             patch("app.matching.matcher.Matcher.rebuild", return_value=1), \
             patch("app.matching.matcher.Matcher.search_for_resume",
                   return_value=[(fresh_id, 0.8)]) as mock_search, \
             patch("app.matching.pipeline._load_resume", return_value="Python ML resume"), \
             patch("app.matching.filters.EmbeddingFilter.filter",
                   return_value=(True, 0.7, "ok")), \
             patch("app.matching.pipeline.SeniorReviewer", autospec=True), \
             patch("app.matching.reranker.Reranker.score",
                   return_value=(70.0, "good fit", [], {})), \
             patch("app.discovery.verify.check_job_alive",
                   return_value=(True, None)), \
             patch("app.matching.pipeline.score_ghost") as mock_ghost:
            mock_ghost.return_value.is_ghost = False
            mock_ghost.return_value.ghost_score = 0.0
            mock_ghost.return_value.flags_json = "[]"
            mock_ghost.return_value.flags = []
            shortlisted = run_matching(user_id=None)

        # Retrieval must ask for the unscored-only corpus.
        assert mock_search.call_args.kwargs.get("only_unscored") is True
        # The fresh job was LLM-scored + shortlisted; the pre-scored one was
        # re-shortlisted without retrieval.
        assert fresh_id in shortlisted
        assert scored_id in shortlisted
        with get_session() as s:
            fresh = s.get(Job, fresh_id)
            assert fresh.rerank_score == 70.0
            for jid in (fresh_id, scored_id):
                app = s.exec(select(Application).where(Application.job_id == jid)).first()
                assert app is not None and app.status == ApplicationStatus.SHORTLISTED
    finally:
        _cleanup("fresh-run-")
