import logging
import re
from typing import List, Dict, Any
from dataclasses import dataclass
from sentence_transformers.util import cos_sim
from app.config import settings
from app.tailoring.doctor import _METRIC_RE

log = logging.getLogger(__name__)


def _adds_unbacked_metric(tailored: str, source_bullet: str) -> bool:
    """True when the tailored bullet contains a metric (e.g. '43%', '2,500 req/min',
    '3x') that does NOT appear verbatim in its matched source bullet.

    This is the exact shape of a fabricated number grafted onto a near-copy of a
    real bullet — high cosine similarity to the source, but with an invented
    metric. Similarity alone waves it through; this forces an LLM fact-check.
    """
    src = (source_bullet or "").lower()
    for m in _METRIC_RE.finditer(tailored or ""):
        token = (m.group(0) or "").strip().lower()
        if token and token not in src:
            return True
    return False

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
        from app.matching.matcher import _get_embed_model
        self.model = _get_embed_model()

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
                    resp = tailor._anthropic_client.messages.create(
                        model=settings.scoring_model,
                        max_tokens=10,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    answer = resp.content[0].text.strip()
                except Exception as ae:
                    log.warning("Grounding: Anthropic failed during verify_with_llm, falling back to OpenAI: %s", ae)
            
            # Fall back to OpenAI if Anthropic failed, was not run, or answer is empty
            if not answer and tailor._openai_client:
                resp = tailor._openai_client.chat.completions.create(
                    model="gpt-4o",
                    max_tokens=10,
                    messages=[{"role": "user", "content": prompt}]
                )
                answer = resp.choices[0].message.content.strip()
                
            if not answer:
                return False
                
            return "SUPPORTED" in answer.upper()
        except Exception as e:
            log.warning("LLM verification of flagged bullet failed: %s", e)
            return False

    def check(self, source_resume_md: str, tailored_resume_md: str) -> GroundingResult:
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

        log.info("Computing embeddings for %d source bullets and %d tailored bullets...", len(source_bullets), len(tailored_bullets))
        
        source_embeddings = self.model.encode(source_bullets, convert_to_tensor=True)
        tailored_embeddings = self.model.encode(tailored_bullets, convert_to_tensor=True)
        
        similarity_matrix = cos_sim(tailored_embeddings, source_embeddings)
        
        flagged_bullets = []
        confidence_map = {}
        threshold = settings.grounding_similarity_threshold
        
        for i, t_bullet in enumerate(tailored_bullets):
            best_match_idx = similarity_matrix[i].argmax().item()
            best_match_score = similarity_matrix[i][best_match_idx].item()
            best_match_bullet = source_bullets[best_match_idx]
            
            confidence_map[t_bullet] = best_match_score

            # Verify when the bullet is dissimilar to any source bullet OR when it
            # is a near-copy that ADDS a metric not present in its matched source
            # bullet — otherwise a fabricated number on a real bullet (cosine ~0.9)
            # would sail past the similarity gate unchecked.
            adds_metric = _adds_unbacked_metric(t_bullet, best_match_bullet)
            if best_match_score < threshold or adds_metric:
                reason = "below threshold" if best_match_score < threshold else "adds unbacked metric"
                log.info("Grounding: verifying bullet (%s, sim=%.3f): %s", reason, best_match_score, t_bullet)
                is_supported = self.verify_with_llm(t_bullet, source_resume_md)
                if not is_supported:
                    flagged_bullets.append({
                        "bullet": t_bullet,
                        "best_match_bullet": best_match_bullet,
                        "best_match_score": best_match_score
                    })
                else:
                    log.info("Grounding: LLM verified bullet as SUPPORTED: %s", t_bullet)
                    
        passed = len(flagged_bullets) == 0
        return GroundingResult(passed=passed, flagged_bullets=flagged_bullets, confidence_map=confidence_map)
