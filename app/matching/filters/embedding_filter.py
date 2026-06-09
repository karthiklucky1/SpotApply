import logging
import numpy as np
from app.db.models import Job
from app.config import settings

log = logging.getLogger(__name__)

class EmbeddingFilter:
    def __init__(self, matcher=None):
        self._matcher = matcher

    @property
    def matcher(self):
        if self._matcher is None:
            from app.matching.matcher import Matcher
            self._matcher = Matcher()
        return self._matcher

    def filter(self, job: Job, resume_text: str) -> tuple[bool, float, str]:
        """Compute cosine similarity of job text against resume chunks.
        
        Returns:
            (passed, max_similarity, reason)
        """
        # Split resume into chunks
        from app.matching.matcher import _chunk_resume
        chunks = _chunk_resume(resume_text)
        if not chunks:
            return False, 0.0, "Resume has no chunks"

        # Encode resume chunks
        resume_embs = self.matcher.encode(chunks)

        # Encode job text
        job_text = self.matcher._job_text(job)
        job_emb = self.matcher.encode([job_text])[0]

        # Calculate cosine similarity (inner product for normalized embeddings)
        similarities = np.dot(resume_embs, job_emb)
        max_sim = float(np.max(similarities))

        min_emb_score = getattr(settings, "min_embedding_score", 0.35)
        passed = max_sim >= min_emb_score
        reason = f"Embedding similarity {max_sim:.3f} vs threshold {min_emb_score:.2f}"
        
        return passed, max_sim, reason
