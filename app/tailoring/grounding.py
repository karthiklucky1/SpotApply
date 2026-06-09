import logging
import os
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
from sentence_transformers import SentenceTransformer
from sentence_transformers.util import cos_sim
from app.config import settings
from app.db.init_db import get_session

log = logging.getLogger(__name__)

@dataclass
class GroundingResult:
    passed: bool
    flagged_bullets: List[Dict[str, Any]]
    confidence_map: Dict[str, float]

class GroundingChecker:
    def __init__(self):
        self.model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")

    def _extract_bullets(self, resume_md: str) -> List[str]:
        """Extract markdown bullets from EXPERIENCE and PROJECTS sections only."""
        bullets = []
        current_section = ""
        for line in resume_md.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Track current section header
            if stripped.startswith("# ") or stripped.startswith("## ") or stripped.startswith("### "):
                current_section = stripped.upper()
                continue
            # Only extract bullets from Experience and Projects sections
            is_target_section = any(k in current_section for k in ["EXPERIENCE", "PROJECT", "WORK", "EMPLOYMENT"])
            if is_target_section and (stripped.startswith("- ") or stripped.startswith("* ")):
                cleaned = stripped[2:].replace("**", "").replace("*", "").strip()
                if cleaned:
                    bullets.append(cleaned)
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
        
        if not source_bullets:
            log.warning("No source bullets found for grounding check!")
            return GroundingResult(passed=True, flagged_bullets=[], confidence_map={})
            
        if not tailored_bullets:
            log.info("No tailored bullets found. Passing.")
            return GroundingResult(passed=True, flagged_bullets=[], confidence_map={})

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
            
            if best_match_score < threshold:
                log.info("Grounding: bullet below threshold (%.3f < %.3f), running LLM verification: %s", best_match_score, threshold, t_bullet)
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
