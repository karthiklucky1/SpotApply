"""Corporate Intelligence parse — read between the lines of a job posting.

One LLM call per job (on-demand when the user opens it, cached on the Job row)
that extracts what the posting *reveals* rather than what it says:

  - pain_point            the immediate problem they're opening their wallet for
  - reporting_to          who the role reports to (funded strategic team vs. maintenance)
  - strategic_importance  High / Medium / Low, inferred from the reporting line
  - migration             legacy → new stack moves telegraphed by the text
  - hidden_strategy       what this hire says about the next 6-12 months
  - culture_decryption    corporate euphemisms translated to operational reality
  - leverage_hook         a 1-2 sentence line the candidate can use in outreach
  - salary                stated comp range if present in the text
  - work_model            the EXACT remote/hybrid/on-site rule, not a vague tag

Strictly inference from the text — the prompt demands null over guessing, and
we surface everything in the UI as AI inference, not fact.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

_PROMPT = """You are an expert Corporate Intelligence Analyst. Analyze the raw job posting below and look past the superficial requirements to extract the hidden structural metadata, pain points, and strategic direction of the hiring company.

Infer STRICTLY from linguistic clues, tooling requirements and org structure in the text. Use null for anything the text does not support — never guess.

Return ONLY a JSON object with these keys:
{
  "pain_point": "One sentence: the immediate, painful technical/operational problem they are paying to solve right now, or null",
  "reporting_to": "Title this role reports to if stated or strongly implied, or null",
  "strategic_importance": "High, Medium or Low — based on reporting line, budget signals and team focus, or null",
  "migration": {"is_migrating": true/false, "details": "what they are moving from/to, or null"},
  "hidden_strategy": "One sentence: what this hire telegraphs about their next 6-12 month roadmap, or null",
  "culture_decryption": [{"phrase": "exact buzzword from the text", "meaning": "the operational reality it usually implies"}],
  "leverage_hook": "1-2 sentences, written in first person, that an applicant can say to prove they instantly understand this company's current headache, or null",
  "salary": {"text": "the exact stated compensation range e.g. '$150K-$190K/yr', or null"},
  "work_model": "The EXACT location rule, e.g. 'Hybrid - 3 days/week in the NYC office' or 'Fully remote (US time zones)' or 'On-site', or null"
}

JOB POSTING:
---
{JD}
---"""


def analyze(title: str, company: str, description: str) -> dict | None:
    """Run the intelligence parse. Returns the parsed dict, {} when the LLM
    can't extract anything, or None when the call itself failed/unavailable."""
    from app.config import settings
    if not settings.anthropic_api_key or not (description or "").strip():
        return None
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=settings.anthropic_api_key)
        jd = f"Role: {title}\nCompany: {company}\n\n{description[:12000]}"
        resp = client.messages.create(
            model=settings.scoring_model,
            max_tokens=700,
            messages=[{"role": "user", "content": _PROMPT.replace("{JD}", jd)}],
        )
        text = (resp.content[0].text or "").strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        log.warning("corporate insights parse failed for %s @ %s: %s", title, company, e)
        return None
