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
                     skills: str, selling: str, needs_sponsorship: bool,
                     job_url: str = "") -> list[dict]:
    """Drafts built around how a referral MECHANICALLY works inside the ATS.

    Two facts dictate the shape of every ask below:

      1. The referrer must select a SPECIFIC LIVE JOB — you cannot be referred
         as a general prospect. So every draft carries the requisition link.
      2. The referrer must describe their RELATIONSHIP TO YOU, in writing, on
         the record. That is precisely why a cold "will you refer me?" gets
         silence: the form asks how they know you and they cannot honestly
         answer "I don't".

    So the asks are ordered by descending likelihood of a yes — forward-the-req
    (costs them nothing, needs no relationship claim) beats a call, which beats
    an information request, which beats the cold referral ask. We no longer lead
    with the cold ask, and we no longer offer a resume on first contact.
    See docs/research/hiring-machine-2026-08.md §1.8, §1.9.
    """
    skills_short = ", ".join([s.strip() for s in (skills or "").split(",") if s.strip()][:3])
    skill_line = f" My background is in {skills_short}." if skills_short else ""
    link_line = f" Here's the exact req: {job_url}" if job_url else ""
    drafts = [
        {
            # Highest-yield ask. Requires no relationship claim from them, so it
            # sidesteps the ATS field that makes strangers decline — and it still
            # gets you out of the inbound pile.
            "type": "referral_request",
            "label": "Ask them to forward the req (highest yield)",
            "channel": "LinkedIn / email to a connection at the company",
            "body": (
                f"Hi {{name}}, I'm applying for the {role} role at {company} and wanted to "
                f"reach out directly.{skill_line} Would you be willing to forward the req to "
                f"the recruiter with a quick note that I reached out? I know a formal referral "
                f"puts your name on it, so no pressure at all either way.{link_line}"
            ),
        },
        {
            # Converts stranger -> acquaintance first, which is the actual unlock.
            # Two concrete windows: a low-cost yes beats an open-ended one.
            "type": "referral_intro_call",
            "label": "Ask for 15 minutes first",
            "channel": "LinkedIn / email to a connection at the company",
            "body": (
                f"Hi {{name}}, I'm looking at the {role} role at {company}.{skill_line} "
                f"Would you be open to a quick 15 minutes — say Wednesday or Thursday "
                f"afternoon? If it still looks like a fit afterwards, I'd love to ask about "
                f"a referral then.{link_line}"
            ),
        },
        {
            # Pure information, near-zero cost, high yes rate.
            "type": "referral_who_owns",
            "label": "Just ask who owns the req",
            "channel": "LinkedIn / email to a connection at the company",
            "body": (
                f"Hi {{name}}, quick question if you have a second — do you know who owns "
                f"the {role} req at {company}? Happy to take it from there myself.{link_line}"
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
                f"Would you be up for a quick chat this week or next — Wednesday or "
                f"Thursday work well on my end?"
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


def get_company_github_repos(company: str) -> list[str]:
    """Search for the company's GitHub organization and return up to 3 popular repos."""
    from app.config import settings
    import httpx
    import re

    token = settings.github_token
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    slug = None
    try:
        q = re.sub(r"[^a-z0-9 ]", "", company.lower()).strip()
        r = httpx.get(
            "https://api.github.com/search/users",
            params={"q": f"{q} type:org", "per_page": 3},
            headers=headers,
            timeout=5,
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            if items:
                slug = items[0]["login"]
    except Exception:
        pass

    if not slug:
        return []

    try:
        r = httpx.get(
            f"https://api.github.com/orgs/{slug}/repos",
            params={"sort": "stars", "per_page": 3},
            headers=headers,
            timeout=5,
        )
        if r.status_code == 200:
            return [repo["name"] for repo in r.json()]
    except Exception:
        pass
    return []


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

    # Get winners from SerpAPI/X-Ray to check for university matches
    winners = []
    try:
        from app.intelligence.linkedin_xray import find_champions
        res = find_champions(company, role)
        if res.get("ok"):
            winners = res.get("people", [])
    except Exception:
        pass

    # Legal work-auth selling point for the visa-alumni draft.
    selling, needs_sponsorship = "", False
    try:
        from app.intelligence.work_auth import assess_profile
        fr = assess_profile(profile)
        selling = fr.selling_point or ""
        needs_sponsorship = bool(fr.needs_future_sponsorship)
    except Exception:
        pass

    # Build fallbacks
    drafts = _fallback_drafts(name, title, company, role, skills, selling, needs_sponsorship,
                              job_url=(getattr(job, "url", "") or ""))

    # Insider Intelligence leverage hook → strengthens the hiring-manager note.
    try:
        import json as _json
        _ins = _json.loads(getattr(job, "corporate_insights", None) or "{}")
        _hook = (_ins.get("leverage_hook") or "").strip()
    except (ValueError, TypeError):
        _hook = ""
    if _hook:
        for d in drafts:
            if d["type"] == "hiring_manager":
                d["body"] = d["body"].rstrip() + " " + _hook
                d["label"] = "Hiring-manager note (with insider hook)"
                break

    # University Alumni check
    uni = getattr(profile, "university", "").strip()
    if uni:
        matched_alum = None
        for w in winners:
            if uni.lower() in w.get("headline", "").lower():
                matched_alum = w
                break
        target_name = matched_alum["name"] if matched_alum else "{Alumni Name}"
        drafts.append({
            "type": "university_alumni",
            "label": "University Alumni connection",
            "channel": "LinkedIn connection request to a fellow alum",
            "body": (
                f"Hi {target_name.split(' ')[0] if target_name else 'there'}, I noticed you also went to {uni} "
                f"and now work at {company} as a {role}. I'm exploring the team there "
                f"and would love to connect with a fellow alum to hear about your experience. Go "
                f"{uni.split(' ')[-1]}!"
            )
        })

    # GitHub check
    repos = []
    try:
        repos = get_company_github_repos(company)
    except Exception:
        pass

    repo_mention = f"the {repos[0]}" if repos else "open-source projects"
    drafts.append({
        "type": "github_outreach",
        "label": "GitHub outreach note",
        "channel": "LinkedIn message targeting open-source contributions",
        "body": (
            f"Hi {{name}}, I noticed your profile and also saw {company}'s active open-source contributions on GitHub"
            + (f", particularly the {repos[0]} repository." if repos else ".")
            + f" As a developer working with similar tech, I'd love to connect and follow your work."
        )
    })

    # Try to upgrade the drafts with the LLM (cheap Haiku). Non-fatal on failure.
    try:
        from app.config import settings
        from app.common.llm import shared_anthropic
        if settings.anthropic_api_key:
            client = shared_anthropic()
            # Length and structure here are not taste — they come from 4M+
            # measured outreach messages: LinkedIn notes under 400 characters
            # reply +22% vs average while 1,200+ run -11%, and the structure
            # "specific common ground -> proof -> low-cost ask" converts at
            # 25-50% versus 5-25% for one that leads with credentials.
            # See docs/research/hiring-machine-2026-08.md §1.9.
            prompt = (
                "You are a job-search outreach coach. Rewrite each draft below to be "
                "warm and specific. Keep the placeholder {name} for the recipient. "
                "Return STRICT JSON: a list of objects with keys "
                "type,label,channel,body — same types/labels/channels as given.\n\n"
                "HARD RULES (these come from measured reply-rate data — do not relax them):\n"
                "- LinkedIn drafts: UNDER 300 characters. Email drafts: 150-199 words.\n"
                "- Structure every message as: one line of specific common ground, then "
                "one or two concrete proof points, then ONE low-cost ask.\n"
                "- Do NOT lead with credentials.\n"
                "- Do NOT explain how the candidate found the recipient.\n"
                "- Do NOT offer, attach or mention a resume on first contact.\n"
                "- No cover-letter language ('I am writing to express my interest', "
                "'I am passionate about your mission').\n"
                "- Keep any requisition URL that appears in a draft — a referrer must "
                "select a specific live job, so the link is load-bearing.\n"
                "- Keep the two concrete time windows where a draft offers them.\n"
                "- Never claim a relationship or shared history that is not in the "
                "candidate details below.\n\n"
                f"Candidate: {name or 'the candidate'}, {title or 'applicant'}. "
                f"Skills: {skills or 'n/a'}. University: {uni or 'n/a'}. "
                f"Role: {role} at {company}. "
                f"Needs sponsorship: {needs_sponsorship}. Selling point: {selling or 'n/a'}.\n\n"
                f"Drafts: {drafts}"
            )
            resp = client.messages.create(
                model=settings.cover_letter_model, max_tokens=1200,
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

    # Post-process drafts to add clickable helper links/instructions
    for d in drafts:
        if d.get("type") == "university_alumni" and uni:
            import urllib.parse
            q_str = f'site:linkedin.com/in/ "{company}" "{uni}"'
            search_url = f"https://www.google.com/search?q={urllib.parse.quote(q_str)}"
            d["channel"] = f"LinkedIn connection request to a fellow alum. Find them via: {search_url}"
        elif d.get("type") == "github_outreach":
            import urllib.parse
            search_url = f"https://github.com/search?q={urllib.parse.quote(company)}&type=users"
            d["channel"] = f"LinkedIn message targeting open-source contributions. Search org: {search_url}"

    return {
        "application_id": application_id,
        "company": company,
        "title": role,
        "note": "Drafts only — review, personalize the recipient, and send from your own account. JobAgent never sends these for you.",
        "drafts": drafts,
    }
