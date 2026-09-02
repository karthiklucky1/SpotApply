import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from sentence_transformers.util import cos_sim
from app.config import settings
from app.tailoring.doctor import _METRIC_RE
from app.tailoring.evidence import (
    build_evidence,
    normalize_text,
    patch_hash,
)

log = logging.getLogger(__name__)

# Bump whenever the verifier's prompt, parsing or model changes. It is half of
# the cache key, so bumping it invalidates every stored verdict at once without
# touching a row — and NOT bumping it after a prompt change would serve answers
# the current verifier never gave.
VERIFIER_VERSION = "v2-batched-2026-09"


def _verifier_version() -> str:
    """Version string including the model, so a model swap invalidates too."""
    return f"{VERIFIER_VERSION}:{getattr(settings, 'scoring_model', '')}"


def _metric_tokens(text: str) -> set[str]:
    """Metrics in `text`, normalized so formatting differences are not new facts.

    Whitespace and thousands separators are dropped ('2,500 requests' and
    '2500 requests' are one metric); everything else is preserved, because the
    digits themselves are the claim.
    """
    out: set[str] = set()
    for m in _METRIC_RE.finditer(text or ""):
        token = re.sub(r"[\s,]+", "", (m.group(0) or "").lower())
        if token:
            out.add(token)
    return out


def _adds_unbacked_metric(tailored: str, source_bullet: str) -> bool:
    """True when the tailored bullet contains a metric (e.g. '43%', '2,500 req/min',
    '3x') that does NOT appear in its matched source bullet.

    This is the exact shape of a fabricated number grafted onto a near-copy of a
    real bullet — high cosine similarity to the source, but with an invented
    metric. Similarity alone waves it through; this forces an LLM fact-check.

    Comparison is by SET MEMBERSHIP over normalized metric tokens, never by
    substring. The old `token not in source_text` test read '5%' as present
    because the source said '45%' — so a fabricated conversion lift rode into
    production on the back of a real latency number, unchecked. Every containing
    number hid a smaller invented one this way ('2%' inside '12%', '1.5x' inside
    '11.5x').
    """
    return bool(_metric_tokens(tailored) - _metric_tokens(source_bullet))

_MONTHS = (r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec")
# A line that is essentially only a date or date range: "May 2022 – Aug 2024",
# "2020 - Present", "Jan 2020 — Dec 2021", "06/2019 - 08/2021".
_DATE_ONLY_RE = re.compile(
    rf"^\W*(?:(?:{_MONTHS})[a-z]*\.?\s*)?[\d/]{{0,7}}\s*\d{{4}}"
    rf"\s*(?:[-–—]|to|until|through)?\s*"
    rf"(?:(?:{_MONTHS})[a-z]*\.?\s*)?(?:[\d/]{{0,7}}\s*\d{{4}}|present|current|now|ongoing)?\W*$",
    re.I,
)
# An employer / location header: "Home Depot Cincinnati, OH",
# "Acme Corp, San Francisco, CA", "Globex — Remote".
_ORG_LOCATION_RE = re.compile(
    r"(?:,\s*[A-Z]{2}|,\s*(?:remote|hybrid|onsite|on-site))\s*$", re.I)


def _is_not_a_bullet(line: str) -> bool:
    """True for résumé lines that are structure, not achievement claims.

    Employer/location headers, standalone job titles and date ranges sit inside
    the EXPERIENCE section and survived the old ``len >= 12 and " " in line``
    filter — whose comment claimed it "skips dates/company headers" while doing
    nothing of the sort. They then went through grounding as if they were
    bullets, could not be semantically supported by any source bullet, got
    escalated to the LLM, and failed the whole résumé to ERROR.

    That is a false positive that BLOCKS a user's application, so removing these
    makes grounding more accurate rather than more permissive: a date range is
    not a claim that can be hallucinated, and real credential facts (degree,
    school, employment dates) are protected structurally by tailoring/lock.py,
    which restores them verbatim from the master before this check even runs.
    """
    s = (line or "").strip()
    if not s:
        return True
    # A markdown heading that carried a bullet glyph ("- ## PROFESSIONAL
    # EXPERIENCE"). The section detector checks for "#" BEFORE glyph stripping,
    # so this reached the bullet list. Seen flagging application 1358.
    if s.lstrip("-*•·–—‣▪◦●» ").startswith("#"):
        return True
    # A label introducing a list, not a claim: "Familiar / Actively Adopting:",
    # "Systems & Infrastructure:", "Generative AI & LLM Engineering:". These are
    # skills-section headings that the section detector missed (not ALL-CAPS, no
    # known section keyword), so they were graded as experience bullets and
    # failed to ground — blocking applications 1358, 1359 and 1284. A real
    # achievement bullet is a sentence; it does not end in a colon.
    if s.endswith(":"):
        return True
    if _DATE_ONLY_RE.match(s):
        return True
    if _ORG_LOCATION_RE.search(s) and not s.endswith((".", "!", "?")):
        return True
    # Standalone titles ("Software Engineer", "Data Analyst"). A real bullet is
    # an action statement; three words is well below anything that reads as one.
    if len(s.split()) < 4:
        return True
    return False


@dataclass
class GroundingResult:
    passed: bool
    flagged_bullets: List[Dict[str, Any]]
    confidence_map: Dict[str, float]
    # ── work actually done, so the cost of a check is observable ────────────
    # tier names how much of the résumé was newly generated, and therefore how
    # much verification it could possibly need:
    #   L0  the tailored résumé says nothing the master does not — no calls
    #   L1  reordered / re-selected existing claims — no calls, no words changed
    #   L2  1-3 rewritten spans — only those spans are verified
    #   L3  substantial generation — every changed span that needs it
    tier: str = "L3"
    spans_total: int = 0
    spans_changed: int = 0
    spans_verified: int = 0     # changed spans that required a verdict
    llm_calls: int = 0          # BATCHED requests, not spans
    cache_hits: int = 0
    # THREE states, not two. `unverified` means the check could not form an
    # opinion (nothing extractable to compare) — which is neither "clean" nor
    # "fabricated". Collapsing it into `passed=True` is how a résumé nobody
    # checked gets delivered as verified; collapsing it into a failure would
    # block a résumé that may be perfectly fine.
    unverified: bool = False

class GroundingChecker:
    def __init__(self):
        # Reuse the ONE process-wide MiniLM held by the matcher rather than
        # constructing a second copy. tailor.py builds a GroundingChecker per
        # tailor request, so this constructor used to load a fresh
        # SentenceTransformer (model weights + a torch graph, ~150-200MB of
        # transient allocation) on every user-triggered tailor — concurrent
        # tailors stacked those loads on top of the matcher's already-resident
        # copy and pushed the container into an OOM kill. It is the exact same
        # checkpoint (all-MiniLM-L6-v2) and the model is stateless once loaded,
        # so sharing is free.
        # A missing ML stack is no longer a dead end. When MiniLM cannot load,
        # `check` falls back to a deterministic containment match (see
        # `_containment`) — the check still RUNS, which matters because
        # grounding_required=True turns "could not verify" into a total outage
        # of tailoring on any container without torch.
        try:
            from app.matching.matcher import _get_embed_model
            self.model = _get_embed_model()
        except Exception as e:
            log.warning("Grounding: MiniLM unavailable (%s) — matching changed spans "
                        "deterministically instead", e)
            self.model = None

    _SECTION_KEYS = ("EXPERIENCE", "PROJECT", "WORK", "EMPLOYMENT")
    _BULLET_PREFIXES = ("- ", "* ", "• ", "·", "– ", "— ", "‣ ", "▪ ", "◦ ", "● ", "» ")

    def _extract_bullets(self, resume_md: str) -> List[str]:
        """Extract experience/project bullets.

        Handles both markdown résumés (## EXPERIENCE + "- " bullets) and the
        plain text produced by PDF/DOCX extraction (ALL-CAPS or title-case
        section headers, varied bullet glyphs, or no bullet glyph at all).
        Falls back to substantive content lines so the grounding check never
        silently no-ops on a real résumé.
        """
        bullets: List[str] = []
        current_section = ""

        _other_sections = ("EDUCATION", "SKILLS", "SUMMARY", "PROJECTS", "CERTIFICATION",
                           "AWARDS", "PUBLICATION", "CONTACT", "OBJECTIVE", "INTERESTS")

        def _is_header(s: str) -> bool:
            if s.startswith("#"):
                return True
            if len(s) > 40 or s.endswith((".", ",", ";")):
                return False
            up = s.upper()
            # All-caps short line, or a short line naming a known résumé section.
            # (Avoids misreading a Title-Case content line like "Managed Backend
            # Systems" as a header.)
            return up == s or any(k in up for k in self._SECTION_KEYS) or any(k in up for k in _other_sections)

        for line in resume_md.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _is_header(stripped):
                current_section = stripped.upper()
                continue
            in_target = any(k in current_section for k in self._SECTION_KEYS)
            if not in_target:
                continue
            cleaned = stripped
            for pre in self._BULLET_PREFIXES:
                if cleaned.startswith(pre):
                    cleaned = cleaned[len(pre):]
                    break
            cleaned = cleaned.replace("**", "").replace("*", "").strip()
            # Keep substantive lines — and ONLY lines that are actually bullets.
            if len(cleaned) >= 12 and " " in cleaned and not _is_not_a_bullet(cleaned):
                bullets.append(cleaned)

        # Safety fallback: if section detection found nothing (e.g. messy PDF
        # extraction with no recognizable headers), compare against ALL
        # substantive lines so grounding still runs instead of passing blindly.
        if not bullets:
            for line in resume_md.splitlines():
                s = line.strip().lstrip("-*•·–—‣▪◦●» ").replace("**", "").strip()
                if len(s) >= 25 and " " in s and not _is_not_a_bullet(s):
                    bullets.append(s)
        return bullets

    def verify_with_llm(self, bullet: str, source_resume_md: str) -> bool:
        """Use the LLM to verify if a flagged bullet is supported by the master resume."""
        prompt = f"""You are a Fact-Checking Assistant for job applications.
Your task is to determine whether the claim in the Tailored Bullet is supported by the Master Resume.

Master Resume:
---
{source_resume_md}
---

Tailored Bullet:
"{bullet}"

Analyze whether the Tailored Bullet represents a factual claim that is supported by or reasonably derived from the Master Resume.
Guidelines:
1. CORE CLAIMS & METRICS: The core metrics (e.g., "22% accuracy", "65% cycle reduction", "2,500+ requests per minute") and core professional experience responsibilities must match or be directly derived from the Master Resume.
2. HONEST BRIDGING: If the Tailored Bullet introduces new technologies or tools (e.g. Triton, vLLM, CUDA) but frames them honestly as adjacent, under study, planned transition, or similar learning/bridging frameworks (e.g., "designed with plans to transition to...", "with adjacent study of...", "familiar with..."), this is SUPPORTED and should pass.
3. FABRICATED CLAIMS: If the bullet claims direct, hands-on production experience, design, implementation, or deployment of a technology that the candidate does not have in their Master Resume (e.g., claiming they actively developed Triton services or built CUDA kernels if not in the Master Resume), it is FABRICATED.

Return exactly "SUPPORTED" if it is supported, or "FABRICATED" if it is not supported. No other text.
"""
        try:
            from app.tailoring.tailor import Tailor
            tailor = Tailor()
            answer = ""
            
            # Try Anthropic first if it is the active backend
            if tailor._active_backend == "anthropic" and tailor._anthropic_client:
                try:
                    from app.common.llm import sampling
                    resp = tailor._anthropic_client.messages.create(
                        model=settings.scoring_model,
                        max_tokens=10,
                        messages=[{"role": "user", "content": prompt}],
                        **sampling(settings.scoring_model, settings.verifier_temperature),
                    )
                    answer = resp.content[0].text.strip()
                except Exception as ae:
                    log.warning("Grounding: Anthropic failed during verify_with_llm, falling back to OpenAI: %s", ae)
            
            # Fall back to OpenAI if Anthropic failed, was not run, or answer is empty
            if not answer and tailor._openai_client:
                resp = tailor._openai_client.chat.completions.create(
                    model="gpt-4o",
                    max_tokens=10,
                    temperature=settings.verifier_temperature,
                    messages=[{"role": "user", "content": prompt}]
                )
                answer = resp.choices[0].message.content.strip()
                
            if not answer:
                return False
                
            return "SUPPORTED" in answer.upper()
        except Exception as e:
            log.warning("LLM verification of flagged bullet failed: %s", e)
            return False

    # ── batched patch verification ────────────────────────────────────────────

    _BATCH_SYSTEM = (
        "You are a fact-checking assistant for job applications. For each "
        "numbered CLAIM you decide whether it is supported by the candidate's "
        "master resume, given the SOURCE line the claim was derived from.\n\n"
        "Guidelines:\n"
        "1. CORE CLAIMS & METRICS: metrics and responsibilities must match, or "
        "be directly derived from, the master resume.\n"
        "2. HONEST BRIDGING: a claim that frames a new technology as adjacent, "
        "under study, or planned ('with adjacent study of...', 'familiar "
        "with...') is SUPPORTED.\n"
        "3. FABRICATED: a claim of direct hands-on production experience with "
        "something absent from the master resume is FABRICATED.\n\n"
        "Answer with one line per claim, in order, formatted exactly as:\n"
        "<number>: SUPPORTED\n<number>: FABRICATED\n"
        "No other text."
    )

    def verify_batch(self, patches: List[Tuple[str, str]],
                     source_resume_md: str) -> List[bool]:
        """Verify several generated claims in ONE request.

        `patches` is a list of (claim, matched_source_line) pairs. The master
        résumé is included ONCE for the whole batch rather than once per claim:
        the old path spent 1,423 input tokens to answer a five-token question,
        repeated per bullet, which a measured audit put at 64% of a tailor's
        total cost. Each claim still carries the specific source line it was
        derived from, so the model judges the pair and not the document.

        Returns one verdict per patch, in order. On any failure every verdict is
        False — an unanswered fact-check is not a pass.
        """
        if not patches:
            return []
        try:
            from app.tailoring.tailor import Tailor
            tailor = Tailor()
            client = tailor._anthropic_client if tailor._active_backend == "anthropic" else None

            items = "\n\n".join(
                f"{i}. CLAIM: {claim}\n   SOURCE: {src or '(no matching line in the master resume)'}"
                for i, (claim, src) in enumerate(patches, start=1)
            )
            prompt = (
                f"Master resume:\n---\n{source_resume_md}\n---\n\n"
                f"Claims to check ({len(patches)}):\n\n{items}\n\n"
                f"Return exactly {len(patches)} lines, one verdict per claim."
            )

            answer = ""
            if client is not None:
                try:
                    from app.common.llm import sampling
                    resp = client.messages.create(
                        model=settings.scoring_model,
                        max_tokens=16 * len(patches) + 32,
                        system=self._BATCH_SYSTEM,
                        messages=[{"role": "user", "content": prompt}],
                        **sampling(settings.scoring_model, settings.verifier_temperature),
                    )
                    answer = resp.content[0].text.strip()
                except Exception as ae:
                    log.warning("Grounding: batched Anthropic verify failed: %s", ae)
            if not answer and tailor._openai_client:
                resp = tailor._openai_client.chat.completions.create(
                    model="gpt-4o",
                    max_tokens=16 * len(patches) + 32,
                    temperature=settings.verifier_temperature,
                    messages=[{"role": "system", "content": self._BATCH_SYSTEM},
                              {"role": "user", "content": prompt}],
                )
                answer = (resp.choices[0].message.content or "").strip()
            if not answer:
                return [False] * len(patches)
            return self._parse_batch_answer(answer, len(patches))
        except Exception as e:
            log.warning("Batched grounding verification failed: %s", e)
            return [False] * len(patches)

    @staticmethod
    def _parse_batch_answer(answer: str, expected: int) -> List[bool]:
        """Map '<n>: SUPPORTED' lines back onto the patch order.

        A verdict we could not read is False, not True: the whole point of the
        gate is that silence never means clean.
        """
        verdicts: List[Optional[bool]] = [None] * expected
        for line in answer.splitlines():
            m = re.match(r"\s*(\d+)\s*[:.)-]\s*(SUPPORTED|FABRICATED)", line.strip(), re.I)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            if 0 <= idx < expected:
                verdicts[idx] = m.group(2).upper() == "SUPPORTED"
        if all(v is None for v in verdicts):
            # No numbering at all — fall back to bare verdict words in order.
            words = re.findall(r"\b(SUPPORTED|FABRICATED)\b", answer, re.I)
            for i, w in enumerate(words[:expected]):
                verdicts[i] = w.upper() == "SUPPORTED"
        return [bool(v) for v in verdicts]

    # ── similarity ────────────────────────────────────────────────────────────

    @staticmethod
    def _containment(claim: str, source: str) -> float:
        """Share of the claim's content words that appear in the source line.

        The deterministic stand-in for cosine similarity, used when MiniLM is
        not resident. It answers the same question a cosine score is used for
        here — "is this a rewrite of that line, or something new?" — without a
        model, so grounding still runs on a container with no ML stack instead
        of reporting every résumé unverified.
        """
        a = set(re.findall(r"[a-z0-9+#.]{3,}", (claim or "").lower()))
        b = set(re.findall(r"[a-z0-9+#.]{3,}", (source or "").lower()))
        if not a:
            return 0.0
        return len(a & b) / len(a)

    def _match_sources(self, claims: List[str], sources: List[str]) -> List[Tuple[str, float]]:
        """Pair each claim with its single best source line and that score."""
        if not sources:
            return [("", 0.0) for _ in claims]
        if self.model is not None:
            try:
                src_emb = self.model.encode(sources, convert_to_tensor=True)
                cl_emb = self.model.encode(claims, convert_to_tensor=True)
                sim = cos_sim(cl_emb, src_emb)
                out = []
                for i in range(len(claims)):
                    j = sim[i].argmax().item()
                    out.append((sources[j], sim[i][j].item()))
                return out
            except Exception as e:
                log.warning("Grounding: embedding match failed, using deterministic "
                            "containment instead: %s", e)
        out = []
        for claim in claims:
            best, best_score = "", 0.0
            for src in sources:
                score = self._containment(claim, src)
                if score > best_score:
                    best, best_score = src, score
            out.append((best, best_score))
        return out

    def check(self, source_resume_md: str, tailored_resume_md: str, *,
              use_cache: bool = True) -> GroundingResult:
        """Verify a tailored résumé against its master.

        ``use_cache=False`` forces every needed verdict to be recomputed. That
        is the manual re-check path: a user who is unsure about a bullet can ask
        for a fresh opinion instead of being served a stored one. It is
        deliberately opt-in — re-buying a verdict that cannot have changed is
        exactly the waste the cache exists to remove.
        """
        source_bullets = self._extract_bullets(source_resume_md)
        tailored_bullets = self._extract_bullets(tailored_resume_md)

        if not tailored_bullets:
            # NOT a pass. Extracting zero bullets means the parse found nothing
            # to check, which is a fact about our extractor, not about the
            # document — and "we checked and it was clean" is the one thing this
            # result is allowed to mean. Report it as unverified and let the
            # caller decide (grounding_required now defaults to True).
            log.warning("Grounding: no tailored bullets extracted — cannot verify, "
                        "reporting UNVERIFIED rather than passing")
            return GroundingResult(passed=False, flagged_bullets=[], confidence_map={},
                                   unverified=True)

        if not source_bullets:
            # Don't fail open: with no comparable source bullets, every tailored
            # bullet is LLM-verified against the FULL master resume text instead
            # of being waved through unchecked.
            log.warning("No source bullets extracted — LLM-verifying each tailored "
                        "bullet against the full master resume.")
            flagged_bullets = []
            confidence_map = {}
            for t_bullet in tailored_bullets:
                confidence_map[t_bullet] = 0.0
                if not self.verify_with_llm(t_bullet, source_resume_md):
                    flagged_bullets.append({
                        "bullet": t_bullet,
                        "best_match_bullet": "",
                        "best_match_score": 0.0,
                    })
            return GroundingResult(passed=not flagged_bullets,
                                   flagged_bullets=flagged_bullets,
                                   confidence_map=confidence_map)

        # ── Tier the work before spending anything ────────────────────────────
        # A claim the master already makes has not been generated, so there is
        # nothing to fact-check: no similarity search, no LLM call, no cache
        # entry. Verification is scoped to what actually CHANGED — which is the
        # whole point, because the old path re-verified an entire résumé every
        # time one bullet was rewritten.
        evidence = build_evidence(source_resume_md)
        known = {s.normalized for s in evidence.spans}
        known.update(normalize_text(b) for b in source_bullets)

        confidence_map: Dict[str, float] = {}
        changed: List[str] = []
        for bullet in tailored_bullets:
            if normalize_text(bullet) in known:
                confidence_map[bullet] = 1.0      # verbatim from the master
            else:
                changed.append(bullet)

        if not changed:
            # L0 (identical) / L1 (reordered or re-selected): the words and the
            # facts are the master's own. Nothing to verify, nothing to charge.
            tier = "L0" if len(tailored_bullets) == len(source_bullets) else "L1"
            log.info("Grounding: %s — %d/%d spans unchanged, no verification needed",
                     tier, len(tailored_bullets), len(tailored_bullets))
            return GroundingResult(
                passed=True, flagged_bullets=[], confidence_map=confidence_map,
                tier=tier, spans_total=len(tailored_bullets), spans_changed=0,
                spans_verified=0, llm_calls=0, cache_hits=0,
            )

        matches = self._match_sources(changed, source_bullets)
        threshold = settings.grounding_similarity_threshold

        # Only a changed span that is EITHER unlike anything in the master OR a
        # near-copy carrying a metric its source does not have needs a verdict.
        needs_verdict: List[Tuple[str, str, float]] = []
        for bullet, (best_bullet, score) in zip(changed, matches, strict=True):
            confidence_map[bullet] = score
            adds_metric = _adds_unbacked_metric(bullet, best_bullet)
            if score < threshold or adds_metric:
                log.info("Grounding: span needs a verdict (%s, sim=%.3f): %s",
                         "below threshold" if score < threshold else "adds unbacked metric",
                         score, bullet[:120])
                needs_verdict.append((bullet, best_bullet, score))

        tier = "L2" if len(needs_verdict) <= 3 else "L3"
        if not needs_verdict:
            log.info("Grounding: %s — %d changed spans, all close paraphrases of "
                     "their source, none required a verdict", tier, len(changed))
            return GroundingResult(
                passed=True, flagged_bullets=[], confidence_map=confidence_map,
                tier=tier, spans_total=len(tailored_bullets),
                spans_changed=len(changed), spans_verified=0,
                llm_calls=0, cache_hits=0,
            )

        # ── Reuse before spending ─────────────────────────────────────────────
        from app.tailoring import verify_cache
        version = _verifier_version()
        span_ids = {s.normalized: s.span_id for s in evidence.spans}
        keys = [
            (evidence.evidence_id,
             patch_hash(bullet, span_ids.get(normalize_text(src))),
             version)
            for bullet, src, _ in needs_verdict
        ]
        cached = verify_cache.lookup(keys) if use_cache else {}

        verdicts: Dict[int, bool] = {}
        pending: List[int] = []
        for i, key in enumerate(keys):
            if key in cached:
                verdicts[i] = cached[key]
            else:
                pending.append(i)

        llm_calls = 0
        fresh: List[Tuple[Tuple[str, str, str], bool]] = []
        batch_max = max(1, int(getattr(settings, "grounding_verify_batch_max", 12)))
        for start in range(0, len(pending), batch_max):
            chunk = pending[start:start + batch_max]
            pairs = [(needs_verdict[i][0], needs_verdict[i][1]) for i in chunk]
            answers = self.verify_batch(pairs, source_resume_md)
            llm_calls += 1
            for i, supported in zip(chunk, answers, strict=False):
                verdicts[i] = supported
                fresh.append((keys[i], supported))
        if fresh:
            verify_cache.store(fresh)

        flagged_bullets = [
            {"bullet": bullet, "best_match_bullet": best_bullet, "best_match_score": score}
            for i, (bullet, best_bullet, score) in enumerate(needs_verdict)
            if not verdicts.get(i, False)
        ]

        log.info("Grounding: %s — %d/%d spans changed, %d verified "
                 "(%d cache hits, %d LLM calls), %d flagged",
                 tier, len(changed), len(tailored_bullets), len(needs_verdict),
                 len(cached), llm_calls, len(flagged_bullets))

        return GroundingResult(
            passed=not flagged_bullets,
            flagged_bullets=flagged_bullets,
            confidence_map=confidence_map,
            tier=tier,
            spans_total=len(tailored_bullets),
            spans_changed=len(changed),
            spans_verified=len(needs_verdict),
            llm_calls=llm_calls,
            cache_hits=len(cached),
        )
