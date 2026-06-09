import pytest
from app.db.models import Job, JobSource
from app.matching.filters.embedding_filter import EmbeddingFilter

def test_embedding_filter():
    # Instantiate EmbeddingFilter
    # We pass None to matcher so it lazy loads it or we mock/stub it.
    # To keep tests fast and avoid loading heavy models during simple tests,
    # let's write a mock Matcher first.
    class MockMatcher:
        def encode(self, texts):
            import numpy as np
            # Return dummy 384-dimensional normalized vectors
            # Let's make sure the shape is correct
            num_texts = len(texts)
            arr = np.random.randn(num_texts, 384)
            # Normalize L2
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            return (arr / norms).astype("float32")
            
        def _job_text(self, job):
            return f"{job.title} {job.description}"

    mock_matcher = MockMatcher()
    emb_filter = EmbeddingFilter(matcher=mock_matcher)
    
    job = Job(
        source=JobSource.GREENHOUSE,
        external_id="111",
        company="TestCo",
        title="ML Engineer",
        description="Build machine learning systems",
        url="http://test.com"
    )
    
    passed, score, reason = emb_filter.filter(job, "Resume text goes here")
    assert isinstance(passed, bool)
    assert isinstance(score, float)
    assert -1.0 <= score <= 1.0
