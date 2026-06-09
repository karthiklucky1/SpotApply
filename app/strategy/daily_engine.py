import logging
from typing import List
from app.db.models import Job

log = logging.getLogger(__name__)

class DailyStrategyEngine:
    def __init__(self, max_age_days: int = 14):
        self.max_age_days = max_age_days
        log.info("DailyStrategyEngine initialized (stub). Max age days: %d", max_age_days)

    def prioritize_jobs(self, jobs: List[Job]) -> List[Job]:
        """Rank and filter jobs based on composite scoring: match_quality x freshness x sponsorship_likelihood."""
        prioritized = []
        for job in jobs:
            match_quality = job.rerank_score or 50.0
            freshness = 1.0  # multiplier based on hours since discovery
            sponsorship_mult = 1.0  # multiplier based on sponsorship check
            
            composite_score = match_quality * freshness * sponsorship_mult
            job.rerank_score = composite_score
            prioritized.append(job)
            
        prioritized.sort(key=lambda j: j.rerank_score or 0.0, reverse=True)
        log.info("DailyStrategyEngine: prioritized %d jobs.", len(prioritized))
        return prioritized
