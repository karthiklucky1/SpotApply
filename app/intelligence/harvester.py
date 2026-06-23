"""Weekly personal profile harvester — 'recruiter memory'.

Harvests the user's OWN public footprint (GitHub via the official API; LinkedIn
intentionally NOT auto-scraped — see below) and asks the LLM to write a weekly
recruiter brief: new skills/projects to add to the master resume, and profile
improvement suggestions. Results are stored per-user in UserPersonalMemory.

Safety / legality:
  - GitHub: official public REST API, optional token. Fully ToS-compliant.
  - LinkedIn: automated scraping violates LinkedIn's ToS and risks the user's
    account, so we do NOT crawl it. We record the user's own URL and prompt for
    a manual paste instead. (A future, compliant path is the official LinkedIn
    API with user OAuth.)
  - Everything is scoped to a single user_id. We only ever touch the user's own
    public profiles, never third parties.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

_GH_API = "https://api.github.com"


def _github_username(url: str) -> str | None:
    if not url:
        return None
    u = url.strip().rstrip("/")
    if "github.com" in u:
        part = u.split("github.com/", 1)[-1].split("/")[0].split("?")[0]
        return part or None
    # bare username
    if "/" not in u and " " not in u:
        return u
    return None


def harvest_github(github_url: str) -> dict:
    """Fetch recent public repos + activity for the user's own GitHub."""
    from app.config import settings
    try:
        import httpx
    except Exception as e:
        return {"ok": False, "reason": f"httpx_unavailable: {e}", "repos": [], "events": []}

    username = _github_username(github_url)
    if not username:
        return {"ok": False, "reason": "no_github_url", "repos": [], "events": []}

    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"

    repos, events = [], []
    try:
        with httpx.Client(timeout=15, headers=headers) as client:
            r = client.get(f"{_GH_API}/users/{username}/repos",
                           params={"sort": "pushed", "per_page": 10, "type": "owner"})
            if r.status_code == 200:
                for repo in r.json():
                    if repo.get("fork"):
                        continue
                    repos.append({
                        "name": repo.get("name"),
                        "description": repo.get("description") or "",
                        "language": repo.get("language") or "",
                        "topics": repo.get("topics", []),
                        "pushed_at": repo.get("pushed_at"),
                        "stars": repo.get("stargazers_count", 0),
                        "url": repo.get("html_url"),
                    })
            ev = client.get(f"{_GH_API}/users/{username}/events/public",
                            params={"per_page": 30})
            if ev.status_code == 200:
                for e in ev.json():
                    if e.get("type") == "PushEvent":
                        for c in (e.get("payload", {}) or {}).get("commits", [])[:3]:
                            events.append({
                                "repo": (e.get("repo", {}) or {}).get("name"),
                                "message": (c.get("message") or "").split("\n")[0][:140],
                                "at": e.get("created_at"),
                            })
    except Exception as e:
        log.warning("GitHub harvest failed for %s: %s", username, e)
        return {"ok": False, "reason": str(e), "repos": repos, "events": events}

    return {"ok": True, "username": username, "repos": repos, "events": events[:20]}


def harvest_linkedin(linkedin_url: str) -> dict:
    """LinkedIn is intentionally NOT auto-scraped (ToS / account safety)."""
    return {
        "ok": False,
        "reason": "linkedin_autocrawl_disabled",
        "url": linkedin_url or "",
        "note": "Automated LinkedIn crawling violates LinkedIn's ToS and risks "
                "your account. Paste your headline/about manually to include it.",
    }


def _llm_brief(profile, gh: dict) -> tuple[str, str]:
    """Return (recommendations_markdown, parsed_updates_json) — LLM with fallback."""
    repos = gh.get("repos", []) if gh else []
    events = gh.get("events", []) if gh else []
    existing_skills = (getattr(profile, "key_skills", "") or "")

    # Deterministic fallback brief.
    new_langs = sorted({r.get("language") for r in repos if r.get("language")
                        and r.get("language").lower() not in existing_skills.lower()})
    fallback_lines = ["### Weekly recruiter brief"]
    if repos:
        top = repos[0]
        fallback_lines.append(
            f"- Your most recently active repo is **{top['name']}**"
            f"{(' — ' + top['description']) if top['description'] else ''}.")
    if new_langs:
        fallback_lines.append(
            f"- Consider adding to your résumé skills: **{', '.join(new_langs)}** "
            "(seen in recent repos but not in your key skills).")
    if events:
        fallback_lines.append(f"- {len(events)} recent commits — keep the streak visible on your profile.")
    if len(fallback_lines) == 1:
        fallback_lines.append("- No new public GitHub activity this week.")
    fallback = "\n".join(fallback_lines)
    parsed = {"new_languages": new_langs,
              "active_repos": [r["name"] for r in repos[:5]],
              "recent_commits": len(events)}

    try:
        from app.config import settings
        from anthropic import Anthropic
        if not settings.anthropic_api_key or not repos:
            return fallback, json.dumps(parsed)
        client = Anthropic(api_key=settings.anthropic_api_key)
        prompt = (
            "You are a technical recruiter reviewing a candidate's recent GitHub "
            "activity. Write a concise weekly brief (<140 words, markdown bullets) "
            "telling them: (1) new skills/technologies to add to their master "
            "resume key_skills, (2) one concrete LinkedIn/profile improvement. Be "
            "specific to the repos.\n\n"
            f"Current key_skills: {existing_skills or 'n/a'}\n"
            f"Recent repos: {json.dumps(repos)[:3000]}\n"
            f"Recent commits: {json.dumps(events)[:1500]}"
        )
        resp = client.messages.create(
            model=settings.cover_letter_model, max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        brief = resp.content[0].text.strip()
        return (brief or fallback), json.dumps(parsed)
    except Exception as e:
        log.debug("harvester LLM brief skipped: %s", e)
        return fallback, json.dumps(parsed)


def _notify_telegram(text: str) -> None:
    try:
        import httpx
        from app.config import settings
        if not (settings.telegram_bot_token and settings.telegram_chat_id):
            return
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text[:3500]},
            timeout=10,
        )
    except Exception as e:
        log.debug("telegram notify skipped: %s", e)


def run_harvest(user_id: str | None = None, notify: bool = False) -> dict:
    """Harvest one user's GitHub, write an LLM recruiter brief, store it."""
    from app.db.init_db import get_session
    from app.db.models import UserPersonalMemory
    from app.autofill.answer_pack import _get_or_create_profile

    profile = _get_or_create_profile(user_id=user_id)
    gh = harvest_github(getattr(profile, "github_url", "") or "")
    li = harvest_linkedin(getattr(profile, "linkedin_url", "") or "")
    recommendations, parsed = _llm_brief(profile, gh)

    raw = json.dumps({"github": gh, "linkedin": li})
    with get_session() as session:
        row = UserPersonalMemory(
            user_id=user_id, source="github",
            raw_content=raw[:20000], parsed_updates=parsed,
            recommendations=recommendations, created_at=datetime.utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        out = {"id": row.id, "created_at": row.created_at.isoformat(),
               "recommendations": recommendations, "parsed_updates": parsed,
               "github_ok": gh.get("ok", False)}

        # Create a notification for new profile suggestions
        try:
            from app.db.models import UserNotification
            
            clean_message = recommendations
            if "### Weekly recruiter brief" in clean_message:
                clean_message = clean_message.replace("### Weekly recruiter brief", "").strip()
            # Clean up bullet/dash formatting
            clean_message = "\n".join([line.lstrip("-* ").strip() for line in clean_message.split("\n") if line.strip()])
            
            notif = UserNotification(
                user_id=user_id or "local",
                title="Profile & Repo Suggestions 🧠",
                message=clean_message[:1000],
                type="profile_suggestions",
                link="/dashboard",
            )
            session.add(notif)
            session.commit()
        except Exception as ne:
            log.warning("Failed to create profile suggestion notification: %s", ne)

    if notify and recommendations:
        _notify_telegram("🧠 JobAgent weekly profile brief:\n\n" + recommendations)
    return out


def ingest_linkedin_text(user_id: str | None, text: str) -> dict:
    """Legal LinkedIn path: the user PASTES their own headline/about/experience;
    we extract skills + write a brief. No scraping, no automation."""
    from app.db.init_db import get_session
    from app.db.models import UserPersonalMemory
    text = (text or "").strip()
    if not text:
        raise ValueError("empty LinkedIn text")

    recommendations = (
        "### From your LinkedIn paste\n"
        "- Saved — we'll weave these details into résumé tailoring and cover letters."
    )
    try:
        from app.config import settings
        from anthropic import Anthropic
        if settings.anthropic_api_key:
            client = Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                model=settings.cover_letter_model, max_tokens=400,
                messages=[{"role": "user", "content": (
                    "From this LinkedIn profile text, list (markdown, <120 words): "
                    "(1) the strongest skills/keywords to add to a master resume, "
                    "(2) one concrete headline/about improvement.\n\n" + text[:6000])}],
            )
            recommendations = resp.content[0].text.strip() or recommendations
    except Exception as e:
        log.debug("LinkedIn paste brief skipped: %s", e)

    with get_session() as session:
        row = UserPersonalMemory(
            user_id=user_id, source="linkedin",
            raw_content=text[:20000], parsed_updates="",
            recommendations=recommendations, created_at=datetime.utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        
        # Create a notification for LinkedIn suggestions
        try:
            from app.db.models import UserNotification
            
            clean_message = recommendations
            if "### From your LinkedIn paste" in clean_message:
                clean_message = clean_message.replace("### From your LinkedIn paste", "").strip()
            # Clean up bullet/dash formatting
            clean_message = "\n".join([line.lstrip("-* ").strip() for line in clean_message.split("\n") if line.strip()])
            
            notif = UserNotification(
                user_id=user_id or "local",
                title="LinkedIn Profile Suggestions 🧠",
                message=clean_message[:1000],
                type="profile_suggestions",
                link="/dashboard",
            )
            session.add(notif)
            session.commit()
        except Exception as ne:
            log.warning("Failed to create LinkedIn notification: %s", ne)
            
        # Trigger cross-profile alignment checks
        try:
            check_cross_profile_alignment(user_id)
        except Exception as ae:
            log.warning("Cross profile alignment checks failed during paste: %s", ae)

        return {"id": row.id, "created_at": row.created_at.isoformat(),
                "recommendations": recommendations}


def run_harvest_all_users() -> int:
    """Weekly cron entry: harvest every user who has a GitHub URL. Returns count."""
    from app.db.init_db import get_session
    from app.db.models import UserProfile
    from sqlmodel import select
    n = 0
    with get_session() as session:
        profiles = session.exec(select(UserProfile)).all()
    for p in profiles:
        if not (getattr(p, "github_url", "") or "").strip():
            continue
        try:
            run_harvest(user_id=p.user_id, notify=False)
            n += 1
        except Exception as e:
            log.warning("harvest failed for user %s: %s", p.user_id, e)
    log.info("Weekly harvest complete for %d users", n)
    return n


def check_cross_profile_alignment(user_id: str | None = None) -> None:
    """Compare user's resume and LinkedIn profile (from recruiter memory) to generate notifications."""
    from app.db.init_db import get_session
    from app.db.models import UserPersonalMemory, UserNotification
    from app.matching.pipeline import _load_resume
    from sqlmodel import select
    import json
    import anthropic
    from app.config import settings

    user_id_db = user_id if user_id and user_id != "local" else None
    
    # 1. Load Resume text
    try:
        resume_text = _load_resume(user_id=user_id_db)
    except Exception:
        resume_text = None

    if not resume_text or len(resume_text.strip()) < 30:
        return

    # 2. Load latest LinkedIn paste
    with get_session() as session:
        q = select(UserPersonalMemory).where(
            UserPersonalMemory.user_id == user_id_db,
            UserPersonalMemory.source == "linkedin"
        ).order_by(UserPersonalMemory.created_at.desc())
        latest_li = session.exec(q).first()

    if not latest_li or not (latest_li.raw_content or "").strip():
        # If no LinkedIn paste is on file, notify them once
        with get_session() as session:
            existing = session.exec(
                select(UserNotification).where(
                    UserNotification.user_id == (user_id or "local"),
                    UserNotification.title == "Enhance suggestions: Link LinkedIn 🔗",
                    UserNotification.read == False
                )
            ).first()
            if not existing:
                notif = UserNotification(
                    user_id=user_id or "local",
                    title="Enhance suggestions: Link LinkedIn 🔗",
                    message="Paste your LinkedIn profile text in Settings -> Recruiter memory so we can review your online brand alignment!",
                    type="profile_suggestions",
                    link="/dashboard"
                )
                session.add(notif)
                session.commit()
        return

    linkedin_text = latest_li.raw_content

    # 3. Call Claude to compare
    if not settings.anthropic_api_key:
        return

    prompt = f"""You are a senior recruiter auditing a candidate's online brand alignment. 
Compare their Resume and their LinkedIn profile text to find inconsistencies, missing experience, or missing skills.

Resume:
{resume_text[:6000]}

LinkedIn Profile Text:
{linkedin_text[:6000]}

Identify:
1. Experience discrepancies (e.g., if the resume shows 2 years of experience at a company or overall, but LinkedIn has no experience, different dates, or is missing that job completely).
2. Key skills present on the resume but missing from the LinkedIn profile.
3. Concrete profile alignment suggestions.

Return ONLY a JSON list of objects, where each object has:
- "label": a short, punchy alert title (e.g. "Missing LinkedIn Experience", "Missing LinkedIn Skills")
- "ok": a boolean (use false if there is a discrepancy/action item for the user)
- "detail": a specific, actionable recruiter recommendation (e.g. "Your resume lists 2 years of experience as a Software Engineer at Stripe, but this role is completely missing from your LinkedIn profile. Add this Stripe experience to your profile to align your brand.", "Add 'React' and 'Python' skills to your LinkedIn skills section as they are prominent in your resume but missing on LinkedIn.")

If everything is perfectly aligned, return an empty list [].
Return only valid JSON, no markdown, no explanation."""

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        
        # Strip markdown fences if present
        import re
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        
        findings = json.loads(raw)
        if not isinstance(findings, list):
            return

        with get_session() as session:
            for f in findings:
                if not f.get("ok"):
                    title = f"LinkedIn Suggestion: {f['label']} 🔗"
                    # Check if unread notification with same title already exists
                    existing = session.exec(
                        select(UserNotification).where(
                            UserNotification.user_id == (user_id or "local"),
                            UserNotification.title == title,
                            UserNotification.read == False
                        )
                    ).first()
                    if not existing:
                        notif = UserNotification(
                            user_id=user_id or "local",
                            title=title,
                            message=f.get("detail") or "Consider updating your LinkedIn profile.",
                            type="profile_suggestions",
                            link="/dashboard"
                        )
                        session.add(notif)
            session.commit()
    except Exception as e:
        log.warning("Failed to run cross-profile alignment checks: %s", e)
