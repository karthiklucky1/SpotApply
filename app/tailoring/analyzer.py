import json
import logging
import re
from typing import Dict, Any
from app.config import settings

log = logging.getLogger(__name__)

class RejectionAnalyzer:
    def __init__(self):
        self._active_backend = None
        self._openai_client = None
        self._anthropic_client = None

        if settings.anthropic_api_key:
            try:
                from anthropic import Anthropic
                self._anthropic_client = Anthropic(api_key=settings.anthropic_api_key)
                self._active_backend = "anthropic"
            except Exception:
                pass
        if settings.openai_api_key:
            try:
                from openai import OpenAI
                self._openai_client = OpenAI(api_key=settings.openai_api_key)
                if not self._active_backend:
                    self._active_backend = "openai"
            except Exception:
                pass

    def analyze(self, job_description: str, resume_markdown: str, email_body: str) -> Dict[str, Any]:
        """Use AI to dissect a rejection email and compare with JD and resume."""
        prompt = f"""You are an elite Tech Recruiter and ATS Forensic Expert.
Analyze the following rejection email sent to a candidate, comparing it against the Job Description and the Candidate's Resume to deduce the most likely root cause of the rejection.

Job Description:
---
{job_description[:4000]}
---

Candidate's Resume (Markdown):
---
{resume_markdown[:4000]}
---

Rejection Email Content:
---
{email_body[:2000]}
---

Analyze this rejection deeply. You must return a JSON response containing the following keys (and nothing else):
1. "root_cause": One of "Knockout Filter", "Experience/Skill Gap", "Role Closed", or "General ATS Template".
2. "reason_explanation": A concise 2-sentence explanation of why they were rejected.
3. "gaps_identified": A list of 2-3 specific technical keywords/skills from the JD that are weak or missing in the resume.
4. "actionable_tip": A concrete 1-sentence optimization tip for future resume variants.

Return ONLY raw JSON. No markdown backticks (e.g., do NOT wrap in ```json). No commentary.
"""
        response_text = ""
        # Prefer cheap/fast model for parsing task
        if self._active_backend == "anthropic" and self._anthropic_client:
            try:
                resp = self._anthropic_client.messages.create(
                    model="claude-3-haiku-20240307",
                    max_tokens=800,
                    messages=[{"role": "user", "content": prompt}],
                )
                response_text = resp.content[0].text
            except Exception as e:
                log.warning("RejectionAnalyzer: Anthropic failed, falling back to OpenAI: %s", e)

        if not response_text and self._openai_client:
            try:
                resp = self._openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=800,
                    messages=[{"role": "user", "content": prompt}],
                )
                response_text = resp.choices[0].message.content
            except Exception as e:
                log.warning("RejectionAnalyzer: OpenAI failed: %s", e)

        # Fallback if no LLM worked
        if not response_text:
            return {
                "root_cause": "General ATS Template",
                "reason_explanation": "Could not analyze the rejection reason due to LLM timeout/unavailability.",
                "gaps_identified": [],
                "actionable_tip": "Review the JD and ensure your summary highlights matching core keywords."
            }

        # Parse JSON
        try:
            # Strip any potential markdown code blocks if the LLM ignored instructions
            cleaned = re.sub(r"^```(?:json)?\s*", "", response_text.strip(), flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.IGNORECASE)
            return json.loads(cleaned)
        except Exception as e:
            log.warning("Failed to parse rejection analysis JSON: %s. Raw was: %s", e, response_text)
            return {
                "root_cause": "General ATS Template",
                "reason_explanation": "Rejection received. Could not parse forensic details.",
                "gaps_identified": [],
                "actionable_tip": "Focus on matching core technical keywords mentioned in the JD."
            }
