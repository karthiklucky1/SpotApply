"""Skill ecosystem graph — CardRace v2 Layer 2 (docs/CARDRACE_DESIGN.md §9.2).

A typed, human-readable knowledge base over the skill vocabulary: directional
edges with strengths ("pytorch → ml deployment, 0.7") let the matcher credit
RELATED evidence instead of demanding exact keywords — the PyTorch problem.

This is a knowledge base, not a model: every edge is a named number in a
versioned JSON file (data/skill_graph.json), auditable and editable by hand.

Hard rules (each one exists because the naive version backfires):
- Strengths MULTIPLY along a path; the best path wins (max), paths never SUM —
  summing double-counts correlated evidence.
- Propagation depth is capped at MAX_DEPTH (2 hops).
- Inferred coverage is capped at INFERRED_CAP (0.85) of direct evidence —
  a must-have is never fully satisfied by inference alone.
- Aliases ("k8s" = "kubernetes") are spelling, not inference: full credit.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Dict, Optional, Tuple

from app.config import settings

log = logging.getLogger(__name__)

MAX_DEPTH = 2          # hops of inference allowed
INFERRED_CAP = 0.85    # inferred evidence can never exceed 85% of direct


def _norm(term: str) -> str:
    return " ".join((term or "").lower().replace("-", " ").replace("_", " ").split())


class SkillGraph:
    """Immutable view over the graph file. Build once, query per pair."""

    def __init__(self, aliases: Dict[str, str], edges: Dict[str, Dict[str, float]],
                 version: int = 0):
        self.aliases = aliases            # normalized alias -> canonical term
        self.edges = edges                # from -> {to: strength}
        self.version = version

    def canon(self, term: str) -> str:
        t = _norm(term)
        return self.aliases.get(t, t)

    def infer_strength(self, have: str, want: str) -> float:
        """Best inference strength from a held skill toward a wanted capability.

        1.0 means "same term (or alias)". Anything indirect multiplies edge
        strengths along the path, best path wins, depth-capped. 0.0 = no path.
        """
        src, dst = self.canon(have), self.canon(want)
        if src == dst:
            return 1.0
        best = 0.0
        # Depth-limited best-path search (graphs are small: hand-curated edges).
        frontier: Dict[str, float] = {src: 1.0}
        for _ in range(MAX_DEPTH):
            nxt: Dict[str, float] = {}
            for node, strength in frontier.items():
                for to, w in self.edges.get(node, {}).items():
                    s = strength * w
                    if s <= best:      # can't improve the answer — prune
                        continue
                    if to == dst:
                        best = max(best, s)
                    elif s > nxt.get(to, 0.0):
                        nxt[to] = s
            frontier = nxt
            if not frontier:
                break
        return best

    def coverage(self, evidence: Dict[str, float], want: str,
                 use_inference: bool = True) -> Tuple[float, Optional[str]]:
        """How well a candidate's evidence covers one wanted capability.

        ``evidence`` maps skill -> evidence strength 0..1 (Layer 3). Returns
        (coverage 0..1, via) where ``via`` names the skill that carried it —
        the explanation surface ("covered by: vLLM").

        Direct hits pay full evidence; inferred hits pay
        evidence x path_strength, hard-capped at INFERRED_CAP.
        """
        dst = self.canon(want)
        best, via = 0.0, None
        for skill, ev in evidence.items():
            ev = max(0.0, min(1.0, float(ev)))
            if self.canon(skill) == dst:
                if ev > best:
                    best, via = ev, skill
                continue
            if not use_inference:
                continue
            s = self.infer_strength(skill, dst)
            if s <= 0.0:
                continue
            contrib = min(ev * s, INFERRED_CAP)
            if contrib > best:
                best, via = contrib, skill
        return best, via


# ── Module-level cached loader ────────────────────────────────────────────────
_GRAPH: Optional[SkillGraph] = None
_GRAPH_MTIME: float = -1.0
_LOCK = threading.Lock()
_EMPTY = SkillGraph({}, {}, version=0)


def load_graph(path: Optional[str] = None) -> SkillGraph:
    """Load (and cache) the graph; reloads automatically when the file changes.
    A missing/broken file degrades to the empty graph — exact/alias matching
    only, never an exception on the scoring path."""
    global _GRAPH, _GRAPH_MTIME
    p = path or settings.card_graph_path
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return _EMPTY
    with _LOCK:
        if _GRAPH is not None and mtime == _GRAPH_MTIME:
            return _GRAPH
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            aliases = {_norm(a): _norm(c) for a, c in (raw.get("aliases") or {}).items()}
            edges: Dict[str, Dict[str, float]] = {}
            for e in raw.get("edges") or []:
                src, dst = _norm(e.get("from", "")), _norm(e.get("to", ""))
                w = float(e.get("strength", 0.0))
                if not src or not dst or not (0.0 < w <= 1.0):
                    continue
                edges.setdefault(src, {})
                # duplicate edge: keep the stronger claim
                if w > edges[src].get(dst, 0.0):
                    edges[src][dst] = w
            _GRAPH = SkillGraph(aliases, edges, version=int(raw.get("version", 0)))
            _GRAPH_MTIME = mtime
            log.info("Skill graph loaded: v%d, %d aliases, %d edge sources",
                     _GRAPH.version, len(aliases), len(edges))
        except Exception as e:
            log.warning("Skill graph load failed (%s) — inference disabled: %s", p, e)
            _GRAPH, _GRAPH_MTIME = _EMPTY, mtime
        return _GRAPH
