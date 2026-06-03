"""End-to-end matching pipeline.

1. Load resume from disk.
2. Rebuild FAISS index over all jobs (cheap, runs in seconds for <10k jobs).
3. Search top-K by cosine similarity.
4. Rerank with Claude, store score + reasoning back on Job rows.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import List

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job
from app.matching.matcher import Matcher
from app.matching.reranker import Reranker

log = logging.getLogger(__name__)


def _load_resume() -> str:
    p: Path = settings.resume_path
    if not p.exists():
        raise FileNotFoundError(
            f"Resume not found at {p}. Put a markdown version of your resume there."
        )
    return p.read_text(encoding="utf-8")


def run_matching() -> List[int]:
    resume = _load_resume()
    matcher = Matcher()
    matcher.rebuild()

    candidates = matcher.search_for_resume(resume, k=settings.top_k_rerank)
    candidates = [(jid, score) for jid, score in candidates if score >= settings.min_match_score]
    log.info("%d candidates above cross-encoder threshold %.2f", len(candidates), settings.min_match_score)

    reranker = Reranker()
    shortlisted: List[int] = []
    with get_session() as session:
        # Count applications already created today to honour the daily cap
        today_start = datetime.combine(date.today(), datetime.min.time())
        today_count = len(session.exec(
            select(Application).where(Application.created_at >= today_start)
        ).all())

        for jid, sim in candidates:
            job = session.get(Job, jid)
            if not job:
                continue
            score, reason, concerns = reranker.score(resume, job)
            job.similarity_score = sim
            job.rerank_score = score
            job.rerank_reasoning = reason + (
                ("\nConcerns: " + "; ".join(concerns)) if concerns else ""
            )
            session.add(job)

            # If rerank ≥60, create an Application row in SHORTLISTED state
            if score >= 60:
                existing = session.exec(
                    select(Application).where(Application.job_id == job.id)
                ).first()
                if not existing:
                    if today_count < settings.daily_apply_limit:
                        session.add(
                            Application(
                                job_id=job.id,
                                status=ApplicationStatus.SHORTLISTED,
                                apply_url=job.url,
                            )
                        )
                        shortlisted.append(job.id)
                        today_count += 1
                    else:
                        log.info("Daily apply limit (%d) reached — skipping application creation for job %s.", settings.daily_apply_limit, job.title)
            log.info("Job %s @ %s: sim=%.3f rerank=%.0f — %s",
                     job.title, job.company, sim, score, reason)
        session.commit()
    return shortlisted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    new_ids = run_matching()
    print(f"Shortlisted {len(new_ids)} new applications.")
