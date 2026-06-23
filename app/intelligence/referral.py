"""Referral & outreach co-pilot — DRAFT ONLY.

Agencies get people hired by going through the back door (referrals + direct
outreach), not the ATS. This module DRAFTS those messages for the user to send
themselves from their own account. It never connects to LinkedIn, never scrapes
third parties, and never auto-sends — that keeps the user's accounts safe and
the whole thing within ToS / the law.

Three drafts per job:
  1. referral_request  — ask a connection at the company to refer you
  2. hiring_manager    — a concise value pitch to the hiring manager
  3. visa_alumni       — (sponsorship-needing users) a warm note to someone who
                         went through the visa process at that company
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _fallback_drafts(name: str, title: str, company: str, role: str,
                     skills: str, selling: str, needs_sponsorship: bool) -> list[dict]:
    first = (name or "there").split(" ")[0] if name else ""
    me = name or "I"
    skills_short = ", ".join([s.strip() for s in (skills or "").split(",") if s.strip()][:3])
    skill_line = f" My background is in {skills_short}." if skills_short else ""
    drafts = [
        {
            "type": "referral_request",
            "label": "Referral request",
            "channel": "LinkedIn / email to a connection at the company",
            "body": (
                f"Hi {{name}}, hope you're well! I noticed {company} is hiring a "
                f"{role} and it looks like a strong fit for my background.{skill_line} "
                f"Would you be open to referring me through your employee referral "
                f"link? Happy to send my resume and a few bullet points to make it "
                f"easy. Thanks so much either way!"
            ),
        },
        {
            "type": "hiring_manager",
            "label": "Hiring-manager note",
            "channel": "LinkedIn DM / email to the hiring manager",
            "body": (
                f"Hi {{name}}, I'm reaching out about the {role} role at {company}. "
                f"{('As a ' + title + ', ') if title else ''}I think I'd ramp fast —"
                f"{(' ' + skills_short + ' are right in my wheelhouse.') if skills_short else ''} "
                f"I've applied through your site; I'd love 10 minutes to share why I'm "
                f"a strong fit. Would that be welcome?"
            ),
        },
    ]
    if needs_sponsorship:
        drafts.append({
            "type": "visa_alumni",
            "label": "Visa-alumni connection",
            "channel": "LinkedIn connection request to a fellow visa-process alum",
            "body": (
                f"Hi {{name}}, I came across your profile and noticed you navigated "
                f"the visa journey while building your career at {company}. I'm "
                f"exploring the {role} role there and would love to ask one quick "
                f"question about how {company} approaches work authorization. "
                f"{selling or ''} Thanks for considering!"
            ).strip(),
        })
    return drafts


def generate_referral_drafts(application_id: int, user_id: str | None = None) -> dict:
    """Return draft outreach messages for one application (user must send them)."""
    from app.db.init_db import get_session
    from app.db.models import Application, Job
    from app.autofill.answer_pack import _get_or_create_profile

    with get_session() as session:
        application = session.get(Application, application_id)
        if not application:
            raise ValueError(f"Application {application_id} not found")
        job = session.get(Job, application.job_id)

    profile = _get_or_create_profile(user_id=user_id)
    name = f"{getattr(profile,'first_name','') or ''} {getattr(profile,'last_name','') or ''}".strip()
    title = getattr(profile, "current_title", "") or ""
    skills = getattr(profile, "key_skills", "") or ""
    role = job.title
    company = job.company or "the company"

    # Legal work-auth selling point for the visa-alumni draft.
    selling, needs_sponsorship = "", False
    try:
        from app.intelligence.work_auth import assess_profile
        fr = assess_profile(profile)
        selling = fr.selling_point or ""
        needs_sponsorship = bool(fr.needs_future_sponsorship)
    except Exception:
        pass

    drafts = _fallback_drafts(name, title, company, role, skills, selling, needs_sponsorship)

    # Try to upgrade the drafts with the LLM (cheap Haiku). Non-fatal on failure.
    try:
        from app.config import settings
        from anthropic import Anthropic
        if settings.anthropic_api_key:
            client = Anthropic(api_key=settings.anthropic_api_key)
            prompt = (
                "You are a job-search outreach coach. Rewrite each draft below to be "
                "warm, specific, and under 90 words. Keep the placeholder {name} for "
                "the recipient. Return STRICT JSON: a list of objects with keys "
                "type,label,channel,body — same types/labels/channels as given.\n\n"
                f"Candidate: {name or 'the candidate'}, {title or 'applicant'}. "
                f"Skills: {skills or 'n/a'}. Role: {role} at {company}. "
                f"Needs sponsorship: {needs_sponsorship}. Selling point: {selling or 'n/a'}.\n\n"
                f"Drafts: {drafts}"
            )
            resp = client.messages.create(
                model=settings.cover_letter_model, max_tokens=900,
                messages=[{"role": "user", "content": prompt}],
            )
            import json, re
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed and all("body" in d for d in parsed):
                drafts = parsed
    except Exception as e:
        log.debug("referral LLM enrichment skipped: %s", e)

    return {
        "application_id": application_id,
        "company": company,
        "title": role,
        "note": "Drafts only — review, personalize the recipient, and send from your own account. JobAgent never sends these for you.",
        "drafts": drafts,
    }
