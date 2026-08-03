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
- All three of those routes are STRING routes: they can only credit a want whose
  words the candidate wrote down somewhere. A fourth, SEMANTIC route closes the
  rest, but only against résumé claim phrases and only behind a floor — see the
  note on EMBED_FLOOR for why it must never run against the skill tokens.
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

# ── Semantic route thresholds (docs/CARDRACE_DESIGN.md §9.2.3) ───────────────
# "Managed Kubernetes workloads on GCP" proves "container orchestration" with no
# token in common, so no amount of graph curation reaches it. Cosine over the
# MiniLM already resident for FAISS retrieval does.
#
# WHAT THIS ROUTE MAY BE COMPARED AGAINST IS THE WHOLE FINDING. Measured on the
# 732-row clean subset of the shadow ledger: similarity against the UserCard's
# 27 skill TOKENS scored 54.5% decision agreement — BELOW the 57.1%
# majority-class floor, i.e. worse than answering SHORTLIST every time. Against
# résumé CLAIM PHRASES it scored 62.4%. Tech nouns are simply not what a
# requirement sentence is similar to. So the route fires only when claim phrases
# are supplied; with ``phrases=None`` coverage() is byte-for-byte what it was,
# which is also why every pre-existing guard test still holds.
EMBED_FLOOR = 0.35     # below this, no credit at all. Agreement is MONOTONE in
                       # this number (0.30 measured +1.7 more), which means
                       # loosening it buys agreement with generosity rather than
                       # accuracy — so it holds at the value min_embedding_score
                       # already uses elsewhere in the matcher.
EMBED_FULL = 0.75      # cosine at/above which a claim counts in full; between
                       # the two it ramps LINEARLY, and the ramp is what defuses
                       # the near-floor false friends ("mentoring junior
                       # engineers" hit "master of engineering aug 2026" at
                       # 0.40 — admitted, but at 12% of the cap, not as proof).
                       # Real cosines for a genuine claim/requirement pair land
                       # ~0.65-0.75, so ramping to 1.0 instead would score every
                       # true match as partial.
EMBED_CAP = 0.70       # < PHRASE_CAP < INFERRED_CAP, deliberately: a similarity
                       # hit is a weaker claim than a phrase resolved through its
                       # parts, which is weaker than an edge a human wrote down.
_EMB_CACHE_MAX = 10_000  # ~1.5 KB/vector (384 x float32) => ~15 MB ceiling

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


# ── Semantic route: the ONE MiniLM, a bounded cache, one batch per pair ──────
_EMB_LOCK = threading.Lock()
_EMB_CACHE: Dict[str, "object"] = {}      # normalized text -> L2-normed float32 vec
_EMB_STATE = {"unavailable": False}


def _encoder():
    """The embedding model, or None.

    Goes through ``matcher._get_embed_model`` on purpose: that cache is the
    single owner of MiniLM in this process (tests/test_architecture_invariants),
    the weights are already resident for FAISS retrieval, and constructing a
    second SentenceTransformer here would add ~90 MB to a container that has
    been OOM-killed before (docs/MEMORY.md).

    Returns None — permanently, after one log line — when the ML stack is
    absent. That is the CI configuration (the workflow strips torch,
    sentence-transformers and faiss-cpu), so this route degrades to "not there"
    rather than to an exception on the scoring path.
    """
    if _EMB_STATE["unavailable"]:
        return None
    try:
        from app.matching.matcher import _get_embed_model
        return _get_embed_model()
    except Exception as e:
        _EMB_STATE["unavailable"] = True
        log.info("Semantic skill route off (no embedding model available: %s)", e)
        return None


def embed_prewarm(texts) -> None:
    """Encode every not-yet-cached string in ONE forward pass.

    ``coverage()`` runs once per want per capability per pair, so encoding
    lazily inside it would put a model call in that loop. Callers hand the whole
    set over first (``card_match._skills_factor`` does), after which the loop is
    dict lookups. Best-effort throughout: a failure leaves the cache untouched
    and the semantic route simply does not fire.
    """
    if not getattr(settings, "card_embed_enabled", True):
        return
    pending, seen = [], set()
    for t in texts:
        k = _norm(t)
        if not k or k in seen:
            continue
        seen.add(k)
        pending.append(k)
    if not pending:
        return
    with _EMB_LOCK:
        pending = [k for k in pending if k not in _EMB_CACHE]
    if not pending:
        return
    model = _encoder()
    if model is None:
        return
    try:
        import numpy as np
        vecs = np.asarray(
            model.encode(pending, convert_to_numpy=True, show_progress_bar=False),
            dtype="float32")
        if vecs.ndim != 2 or len(vecs) != len(pending):
            return
        # Normalize here rather than via faiss.normalize_L2: this module must
        # keep working when faiss is absent, and cosine is then a plain dot.
        vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)
    except Exception as e:
        log.debug("skill embedding failed (%d strings): %s", len(pending), e)
        return
    with _EMB_LOCK:
        if len(_EMB_CACHE) + len(pending) > _EMB_CACHE_MAX:
            # Drop the whole map instead of tracking an LRU: refilling costs one
            # batch encode, and an unbounded dict in this process is how the
            # memory budget gets spent silently.
            _EMB_CACHE.clear()
        for k, v in zip(pending, vecs):
            _EMB_CACHE[k] = v


def _embed_vec(text: str):
    """Cached vector for one string, encoding it now if nobody prewarmed."""
    k = _norm(text)
    if not k:
        return None
    with _EMB_LOCK:
        v = _EMB_CACHE.get(k)
    if v is not None:
        return v
    embed_prewarm([k])
    with _EMB_LOCK:
        return _EMB_CACHE.get(k)


def _semantic_coverage(phrases: Dict[str, float], want: str
                       ) -> Tuple[float, Optional[str]]:
    """Best claim-phrase match for one want, or (0.0, None).

    Whole want against whole claim — never token-by-token. Single tokens carry
    too little context for a 384-dim sentence embedding to separate "python"
    the requirement from "python" the passing mention, and the token vocabulary
    is exactly the comparison that measured below the majority-class floor.
    """
    wv = _embed_vec(want)
    if wv is None:
        return 0.0, None
    best, via = 0.0, None
    span = max(EMBED_FULL - EMBED_FLOOR, 1e-6)
    for text, ev in phrases.items():
        ev = max(0.0, min(1.0, float(ev)))
        if ev <= 0.0:
            continue
        pv = _embed_vec(text)
        if pv is None:
            continue
        sim = float(pv @ wv)
        if sim < EMBED_FLOOR:
            continue
        ramp = min((sim - EMBED_FLOOR) / span, 1.0)
        cand = min(ev * ramp * EMBED_CAP, EMBED_CAP)
        if cand > best:
            best, via = cand, text
    return best, via


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
                 use_inference: bool = True,
                 phrases: Optional[Dict[str, float]] = None
                 ) -> Tuple[float, Optional[str]]:
        """How well a candidate's evidence covers one wanted capability.

        ``evidence`` maps skill -> evidence strength 0..1 (Layer 3).
        ``phrases`` maps résumé claim -> strength (UserCard v2); omit it and the
        semantic route never runs. Returns (coverage 0..1, via) where ``via``
        names whatever carried it — the explanation surface ("covered by: vLLM").

        Four routes, strongest wins — every one of them a ``max()``, so adding a
        route can only ever RAISE coverage, never lower it:
        - direct/alias hit           -> full evidence
        - graph inference            -> evidence x path_strength, <= INFERRED_CAP
        - phrase decomposition       -> mean over the want's content tokens,
                                        each resolved by the two routes above,
                                        x PHRASE_CAP
        - semantic (claims only)     -> evidence x ramp(cosine), <= EMBED_CAP
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

        # Semantic tier. Gated on use_inference because a cosine hit is
        # assumption by construction: it has to land in `spread` (expanded minus
        # direct = "how much of this score is inferred"), never in the direct
        # score. Skipped once the string routes already beat what it could pay.
        if (phrases and use_inference and best < EMBED_CAP
                and getattr(settings, "card_embed_enabled", True)):
            sem, s_via = _semantic_coverage(phrases, dst)
            if sem > best:
                best, via = sem, s_via
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
