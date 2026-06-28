"""Pluggable reranker backend for the retrieval rerank stage.

The local cross-encoder is accurate but very slow on Railway's fractional
CPU (~1s/pair). A hosted rerank API (Jina) does the same job in ~300-800ms
for the whole batch. This module abstracts the provider so it's swappable
via `RERANK_PROVIDER` with graceful fallback:

    jina succeeds          → use Jina scores
    jina fails / no key     → return None  → caller uses local cross-encoder
    local also unavailable  → caller falls back to FAISS/RRF order

Provider-agnostic by design — add Voyage/Cohere/Mixedbread the same way.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_JINA_URL = "https://api.jina.ai/v1/rerank"


def rerank_scores(query: str, documents: List[str]) -> Optional[List[float]]:
    """Return a 0-1 relevance score per document (aligned to input order),
    or None to signal the caller should fall back to the local cross-encoder.
    """
    provider = (settings.rerank_provider or "local").lower()
    if provider == "jina":
        return _jina_rerank(query, documents)
    return None  # "local" or unknown → caller uses the on-CPU cross-encoder


def _jina_rerank(query: str, documents: List[str]) -> Optional[List[float]]:
    if not settings.jina_api_key:
        log.debug("Jina rerank: no api key — falling back to local cross-encoder")
        return None
    if not documents:
        return []
    try:
        resp = httpx.post(
            _JINA_URL,
            headers={
                "Authorization": f"Bearer {settings.jina_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.jina_rerank_model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),   # score every doc; we re-map by index
                "return_documents": False,
            },
            timeout=30.0,
        )
        if resp.status_code != 200:
            log.warning("Jina rerank: HTTP %d — falling back to local: %s",
                        resp.status_code, resp.text[:200])
            return None
        results = resp.json().get("results", [])
        # Re-map scores back to the original document order (Jina returns them
        # sorted by relevance with the original index attached).
        scores = [0.0] * len(documents)
        for r in results:
            idx = r.get("index")
            if idx is not None and 0 <= idx < len(documents):
                scores[idx] = float(r.get("relevance_score", 0.0))
        log.info("Jina rerank: scored %d documents", len(documents))
        return scores
    except Exception as e:
        log.warning("Jina rerank failed (%s) — falling back to local cross-encoder", e)
        return None
