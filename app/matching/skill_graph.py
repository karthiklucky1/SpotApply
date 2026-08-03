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
- Capabilities arrive as success-profile PHRASES ("LLM application development"),
  skills arrive as TOKENS ("langchain"). Whole-phrase equality can never bridge
  those two vocabularies, so a phrase decomposes to its content tokens and each
  is resolved separately, capped at PHRASE_CAP. Without this the graph is
  unreachable for most real capabilities (see the note on PHRASE_CAP).
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
# A phrase resolved through its parts is a weaker claim than a graph edge
# someone wrote down on purpose, so it sits below INFERRED_CAP. Measured need:
# the JobCard mint is prompted for success-profile phrases, so ~9 of every 12
# capabilities were multi-word and scored a flat 0 against a token vocabulary —
# g() dropped 37.2% of the jobs Claude accepted (docs/CARDRACE_DESIGN.md §9.2).
PHRASE_CAP = 0.75
# A wanted token found inside a held multi-word skill ("monitoring" in
# "prometheus monitoring"): real evidence, but the skill is about more than the
# token, so it is not full credit.
TOKEN_IN_SKILL = 0.8

# Role-prose filler. These words carry no skill signal on their own; leaving
# them in means "python backend engineering" can only match a candidate who
# literally wrote that phrase down. Stripping them is safe because phrase
# coverage is a max() on top of exact+inference — it can only ever raise.
_GENERIC = frozenset("""
a an and or the of in for to with using use via across over
development developing developer engineering engineer engineers
system systems application applications app apps platform platforms
experience experienced skills skill knowledge expertise proficiency proficient
strong solid deep broad excellent good great modern advanced senior
production grade hands on working practical real world end
tools tooling technologies technology technologies stack stacks
framework frameworks best practices ability able understanding
""".split())


def _norm(term: str) -> str:
    return " ".join((term or "").lower().replace("-", " ").replace("_", " ").split())


def _content_tokens(term: str) -> list[str]:
    """Skill-bearing tokens of a phrase, filler removed.

    Falls back to the raw tokens when everything was filler ("best practices"),
    so a phrase never resolves to nothing at all.
    """
    toks = [t for t in _norm(term).split() if len(t) > 1 and not t.isdigit()]
    kept = [t for t in toks if t not in _GENERIC]
    return kept or toks


class SkillGraph:
    """Immutable view over the graph file. Build once, query per pair."""

    def __init__(self, aliases: Dict[str, str], edges: Dict[str, Dict[str, float]],
                 version: int = 0):
        self.aliases = aliases            # normalized alias -> canonical term
        self.edges = edges                # from -> {to: strength}
        self.version = version
        # token -> multi-word nodes it heads, so a decomposed phrase can still
        # reach a node nobody spells in one word ("backend" -> "backend
        # development", the only route from fastapi to a backend capability).
        nodes = set(edges) | {d for m in edges.values() for d in m} | set(aliases.values())
        self._tok_nodes: Dict[str, set] = {}
        for n in nodes:
            raw = n.split()
            if len(raw) < 2:
                continue
            # Keyed on RAW tokens, minus filler: "backend development" has to be
            # reachable from "backend", and keying on "development" would drag
            # in every node that ends in it.
            for t in raw:
                if t not in _GENERIC:
                    self._tok_nodes.setdefault(t, set()).add(n)
        self._infer_cache: Dict[Tuple[str, str], float] = {}

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
        ck = (src, dst)
        cached = self._infer_cache.get(ck)
        if cached is not None:
            return cached
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
        self._infer_cache[ck] = best
        return best

    def _token_coverage(self, evidence: Dict[str, float], raw: str,
                        use_inference: bool) -> Tuple[float, Optional[str]]:
        """Coverage of ONE token of a phrase by the held skills. Same routes as
        :meth:`coverage`, minus the phrase tier — this is what the phrase tier
        calls, so recursing would be circular."""
        token = self.canon(raw)          # "ml" -> "machine learning"
        t_words = set(token.split())
        best, via = 0.0, None
        for skill, ev in evidence.items():
            ev = max(0.0, min(1.0, float(ev)))
            src = self.canon(skill)
            if src == token:
                cand = ev
            else:
                # The token sitting inside a held multi-word skill
                # ("monitoring" within "prometheus monitoring").
                cand = ev * TOKEN_IN_SKILL if t_words <= set(src.split()) else 0.0
                if use_inference:
                    s = self.infer_strength(src, token)
                    if s > 0.0:
                        cand = max(cand, min(ev * s, INFERRED_CAP))
                    # ...inference out of the held skill's own head terms, so
                    # "LangChain pipelines" still reaches llm -> generative ai...
                    for stok in _content_tokens(src):
                        if stok == src:
                            continue
                        s2 = self.infer_strength(stok, token)
                        if s2 > 0.0:
                            cand = max(cand, min(ev * s2 * TOKEN_IN_SKILL,
                                                 INFERRED_CAP))
                    # ...and toward the multi-word nodes this token heads, which
                    # is the only way a stripped phrase reaches them.
                    heads = self._tok_nodes.get(raw, set()) | self._tok_nodes.get(token, set())
                    for node in heads:
                        s3 = self.infer_strength(src, node)
                        if s3 > 0.0:
                            cand = max(cand, min(ev * s3 * TOKEN_IN_SKILL,
                                                 INFERRED_CAP))
            if cand > best:
                best, via = cand, skill
        return best, via

    def coverage(self, evidence: Dict[str, float], want: str,
                 use_inference: bool = True) -> Tuple[float, Optional[str]]:
        """How well a candidate's evidence covers one wanted capability.

        ``evidence`` maps skill -> evidence strength 0..1 (Layer 3). Returns
        (coverage 0..1, via) where ``via`` names the skill that carried it —
        the explanation surface ("covered by: vLLM").

        Three routes, strongest wins:
        - direct/alias hit           -> full evidence
        - graph inference            -> evidence x path_strength, <= INFERRED_CAP
        - phrase decomposition       -> mean over the want's content tokens,
                                        each resolved by the two routes above,
                                        x PHRASE_CAP
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

        # Phrase tier. Skipped when the want is a bare token — there is nothing
        # to decompose, so the two routes above were the whole story — or when
        # it cannot improve on what they found: a partial phrase match must
        # never beat a skill the candidate actually stated.
        toks = _content_tokens(dst)
        if best < PHRASE_CAP and not (len(toks) == 1 and toks[0] == dst):
            acc, p_via, p_best = 0.0, None, 0.0
            for t in toks:
                cov, cvia = self._token_coverage(evidence, t, use_inference)
                acc += cov
                if cov > p_best:
                    p_best, p_via = cov, cvia
            phrase = min((acc / len(toks)) * PHRASE_CAP, PHRASE_CAP) if toks else 0.0
            if phrase > best:
                best, via = phrase, p_via
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
