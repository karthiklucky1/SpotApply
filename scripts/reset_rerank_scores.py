"""Reset rerank_score on existing jobs so the next matching run re-scores them
through the LLM reranker — used to benchmark the rerank pipeline (Stage 1+2
gate + concurrency) when discovery yields no new unscored jobs.

Safe: scores are recomputed on the next `run_matching`. Only clears the LLM
rerank fields; cheap-filter data (ghost/embedding) is left intact.

Usage:
    python -m scripts.reset_rerank_scores --limit 200            # reset 200 jobs
    python -m scripts.reset_rerank_scores --limit 200 --apply    # actually write
    python -m scripts.reset_rerank_scores --user <uid> --limit 200 --apply
"""
from __future__ import annotations

import argparse
import logging

from sqlmodel import Session, select

from app.db.init_db import engine
from app.db.models import Job

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main(limit: int, user: str | None, apply: bool) -> None:
    with Session(engine) as session:
        q = select(Job).where(Job.is_closed == False, Job.rerank_score.is_not(None))
        if user:
            q = q.where(Job.user_id == user)
        # Highest-scored first so the benchmark exercises real shortlist candidates.
        q = q.order_by(Job.rerank_score.desc()).limit(limit)
        jobs = session.exec(q).all()

        log.info("Found %d scored jobs to reset (limit=%d, user=%s)", len(jobs), limit, user or "all")
        for job in jobs:
            if apply:
                job.rerank_score = None
                job.rerank_reasoning = None
                job.rerank_breakdown = None
                job.blended_score = None
                session.add(job)

        if apply:
            session.commit()
            log.info("Reset %d jobs. Run matching now to benchmark the rerank path.", len(jobs))
        else:
            log.info("Dry-run: %d jobs would be reset. Pass --apply to commit.", len(jobs))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200, help="How many jobs to reset")
    parser.add_argument("--user", default=None, help="Restrict to a specific user_id")
    parser.add_argument("--apply", action="store_true", help="Write changes to DB")
    args = parser.parse_args()
    main(limit=args.limit, user=args.user, apply=args.apply)
