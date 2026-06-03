"""FAISS-backed semantic matcher.

- SentenceTransformer all-MiniLM-L6-v2 produces 384-dim embeddings
- FAISS IndexFlatIP for cosine sim (vectors are L2-normalized)
- Index persists to disk; rebuilt only when stale

Resume goes through the same encoder so queries and corpus are in the same space.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Tuple

import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer, CrossEncoder
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Job

log = logging.getLogger(__name__)

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DIM = 384


def _chunk_resume(resume_text: str) -> List[str]:
    """Split markdown resume by headers and append a target skills profile chunk."""
    raw_chunks = []
    current_chunk = []
    for line in resume_text.split("\n"):
        if line.startswith("## ") or line.startswith("# "):
            if current_chunk:
                raw_chunks.append("\n".join(current_chunk).strip())
            current_chunk = [line]
        else:
            current_chunk.append(line)
    if current_chunk:
        raw_chunks.append("\n".join(current_chunk).strip())
        
    # Keep only non-empty, reasonably-sized chunks
    chunks = [c for c in raw_chunks if len(c.strip()) > 50]
    
    # High-impact search profile summarizing target titles and tech stack
    summary_chunk = (
        "Role Target: Python Developer, AI/ML Systems Engineer, LLM & Deep Learning Infrastructure Engineer.\n"
        "Key Technologies: Python, PyTorch, Transformers, LLMs, RAG, FAISS Vector Search, Semantic Caching, "
        "Agent Orchestration, FastAPI, MLOps, Docker, AWS ECS/Lambda."
    )
    chunks.append(summary_chunk)
    return chunks


class Matcher:
    def __init__(self):
        log.info("Loading embedding model %s …", MODEL_NAME)
        self.model = SentenceTransformer(MODEL_NAME, device="cpu")
        log.info("Loading cross-encoder model mixedbread-ai/mxbai-rerank-xsmall-v1 …")
        self.cross_encoder = CrossEncoder("mixedbread-ai/mxbai-rerank-xsmall-v1", device="cpu")
        self.index_path: Path = settings.faiss_index_path
        self.id_map_path: Path = self.index_path.with_suffix(".ids.npy")
        self.index: "faiss.Index" | None = None
        self.job_ids: np.ndarray | None = None  # index position -> Job.id

    # ---------- embeddings ----------

    def encode(self, texts: List[str]) -> np.ndarray:
        """Returns L2-normalized vectors (so inner product == cosine)."""
        import faiss
        embs = self.model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        faiss.normalize_L2(embs)
        return embs.astype("float32")

    @staticmethod
    def _job_text(job: Job) -> str:
        """Document text we embed for each job. Title weighted heavily."""
        return f"{job.title}\n{job.title}\n{job.company} | {job.location}\n\n{job.description[:4000]}"

    # ---------- index lifecycle ----------

    def rebuild(self) -> int:
        """Embed every job in DB and rebuild the index from scratch."""
        import faiss
        with get_session() as session:
            jobs = session.exec(select(Job)).all()

        if not jobs:
            log.warning("No jobs in DB to index.")
            return 0

        texts = [self._job_text(j) for j in jobs]
        embs = self.encode(texts)

        index = faiss.IndexFlatIP(DIM)
        index.add(embs)

        ids = np.array([j.id for j in jobs], dtype="int64")
        np.save(self.id_map_path, ids)
        faiss.write_index(index, str(self.index_path))

        self.index = index
        self.job_ids = ids
        log.info("FAISS index built: %d vectors", len(jobs))
        return len(jobs)

    def load(self) -> None:
        import faiss
        if not self.index_path.exists():
            raise FileNotFoundError(f"FAISS index missing at {self.index_path}; run rebuild() first.")
        self.index = faiss.read_index(str(self.index_path))
        self.job_ids = np.load(self.id_map_path)
        log.info("FAISS index loaded: %d vectors", self.index.ntotal)

    # ---------- search ----------

    def search_for_resume(self, resume_text: str, k: int = 30) -> List[Tuple[int, float]]:
        """Hybrid search with RRF (Max-Similarity chunked query) + local Cross-Encoder reranking.
        
        Returns [(job_id, cross_encoder_score)] sorted desc.
        """
        # Load embedding index if needed
        if self.index is None or self.job_ids is None:
            self.load()

        with get_session() as session:
            jobs = session.exec(select(Job)).all()

        if not jobs:
            return []

        # Split resume into chunks to prevent vector dilution / sequence truncation
        chunks = _chunk_resume(resume_text)
        log.info("Resume split into %d query chunks for matching.", len(chunks))

        # Build a focused profile string for cross-encoder (last chunk = summary profile)
        profile_chunk = chunks[-1] if chunks else resume_text[:2000]

        # 1. Lexical Search (BM25) with Max-Similarity
        def _tokenize(text: str) -> List[str]:
            return text.lower().split()

        tokenized_corpus = [_tokenize(self._job_text(j)) for j in jobs]
        bm25 = BM25Okapi(tokenized_corpus)

        job_max_bm25 = {j.id: -999999.0 for j in jobs}
        for chunk in chunks:
            tokenized_query = _tokenize(chunk)
            scores = bm25.get_scores(tokenized_query)
            for idx, score in enumerate(scores):
                jid = jobs[idx].id
                if score > job_max_bm25[jid]:
                    job_max_bm25[jid] = score

        bm25_ranking = sorted(jobs, key=lambda j: job_max_bm25[j.id], reverse=True)
        bm25_ranks = {j.id: rank for rank, j in enumerate(bm25_ranking)}

        # 2. Semantic Search (FAISS) with Max-Similarity
        chunk_embs = self.encode(chunks)
        faiss_scores, faiss_idxs = self.index.search(chunk_embs, len(jobs))

        job_max_faiss = {j.id: -1.0 for j in jobs}
        for chunk_idx in range(len(chunks)):
            scores = faiss_scores[chunk_idx]
            idxs = faiss_idxs[chunk_idx]
            for score, idx in zip(scores, idxs):
                if idx >= 0:
                    jid = int(self.job_ids[idx])
                    if score > job_max_faiss[jid]:
                        job_max_faiss[jid] = score

        faiss_ranking = sorted(jobs, key=lambda j: job_max_faiss[j.id], reverse=True)
        faiss_ranks = {j.id: rank for rank, j in enumerate(faiss_ranking)}

        # 3. Reciprocal Rank Fusion (RRF)
        rrf_scores: List[Tuple[Job, float]] = []
        for j in jobs:
            b_rank = bm25_ranks.get(j.id, len(jobs))
            f_rank = faiss_ranks.get(j.id, len(jobs))
            rrf_score = 1.0 / (60.0 + b_rank) + 1.0 / (60.0 + f_rank)
            rrf_scores.append((j, rrf_score))

        # Send top max(k, 200) candidates through Cross-Encoder (was hardcoded to 50)
        ce_batch_size = max(k, 200)
        rrf_ranking = sorted(rrf_scores, key=lambda x: x[1], reverse=True)
        top_candidates = rrf_ranking[:ce_batch_size]
        log.info("Sending %d candidates to cross-encoder (from %d total)", len(top_candidates), len(jobs))

        # 4. Cross-Encoder Reranking — use profile chunk for sharper signal
        pairs = [(profile_chunk, self._job_text(j)) for j, _ in top_candidates]
        logits = self.cross_encoder.predict(pairs, show_progress_bar=True)

        # Sigmoid function to normalize logits to a 0-1 probability score
        scores_norm = 1.0 / (1.0 + np.exp(-logits))

        final_scores = []
        for (job, _), score in zip(top_candidates, scores_norm):
            final_scores.append((job.id, float(score)))

        # Return top k sorted by Cross-Encoder score descending
        final_ranking = sorted(final_scores, key=lambda x: x[1], reverse=True)
        log.info("Cross-encoder top scores: %s", [(s[1]) for s in final_ranking[:5]])
        return final_ranking[:k]
