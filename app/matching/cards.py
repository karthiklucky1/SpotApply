"""CardRace v2 cards — understand once, serve many (docs/CARDRACE_DESIGN.md §2, §9).

JobCard  = ONE Haiku structured read per DISTINCT posting, shared by every tenant.
UserCard = ONE compile per user (re-done only on résumé/role change).
The per-pair judgment then happens in app/matching/card_match.py as pure CPU
arithmetic — the LLM is paid to READ, never to judge pairs.

Cost discipline:
- Minting checks ``llm_budget_exhausted()`` and a dedicated daily mint cap
  (``card_mint_daily_cap``) — cards are cheap (~$0.005) but not free, and a
  runaway lane must never be able to spend unbounded.
- Mints do NOT register against plan finals: a card is a shared platform asset,
  not one user's Tier-2 score.
- Everything here is best-effort: any failure returns None and the caller
  (shadow hook today, card lane later) simply carries on. The live scoring
  path never depends on a card existing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

JOB_CARD_VERSION = 1
USER_CARD_VERSION = 1

# ── Mint-cap accounting (process-local, mirrors reranker budget style) ───────
_mint_lock = threading.Lock()
_mints_today = {"day": "", "count": 0}


def _mint_allowed() -> bool:
    from app.matching.reranker import llm_budget_exhausted
    if llm_budget_exhausted():
        return False
    cap = int(getattr(settings, "card_mint_daily_cap", 0) or 0)
    if cap <= 0:
        return True
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _mint_lock:
        if _mints_today["day"] != today:
            _mints_today["day"], _mints_today["count"] = today, 0
        return _mints_today["count"] < cap


def _register_mint() -> None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _mint_lock:
        if _mints_today["day"] != today:
            _mints_today["day"], _mints_today["count"] = today, 0
        _mints_today["count"] += 1


# ── JSON plumbing ─────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Optional[dict]:
    """Parse the model's JSON, tolerating fences/prose around it."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _haiku_json(system: str, user: str, max_tokens: int = 900) -> Optional[dict]:
    """One structured-extraction call on the mint model. None on any failure."""
    from app.matching.reranker import _shared_llm_clients
    anthropic_client, _openai, _active = _shared_llm_clients()
    if anthropic_client is None:
        return None
    try:
        resp = anthropic_client.messages.create(
            model=settings.card_mint_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return _extract_json("".join(b.text for b in resp.content if hasattr(b, "text")))
    except Exception as e:
        log.debug("card mint call failed: %s", e)
        return None


# ── JobCard ───────────────────────────────────────────────────────────────────

_JOB_CARD_SYSTEM = """You turn ONE job posting into a machine-readable JobCard.
Extract only what the posting actually says; use null/[] when it is silent.
Every list item is a short lowercase phrase (e.g. "python", "ml deployment").
Return a single JSON object, no prose, exactly this shape:
{
  "role_family": "<e.g. backend engineer | ml engineer | data engineer | frontend engineer | fullstack engineer | devops engineer | data scientist | product manager | other>",
  "seniority": "<intern | junior | mid | senior | staff+ | unknown>",
  "years_min": <int or null>,
  "years_max": <int or null>,
  "capabilities": [
    {"name": "<capability>", "importance": <0.0-1.0>,
     "evidence_needed": ["<phrase>", ...]}
  ],
  "nice_to_have": ["<skill>", ...],
  "disqualifiers": ["<e.g. security clearance required | us citizens only | licensure required>", ...],
  "remote_policy": "<remote | hybrid | onsite | unknown>",
  "remote_scope": "<global | country | region | unknown>",
  "country": "<lowercase country the role is anchored to, or 'unknown'>",
  "visa": "<sponsors | no_sponsorship | silent>",
  "salary_min": <int or null>, "salary_max": <int or null>,
  "confidence": {"skills": <0.0-1.0>, "experience": <0.0-1.0>,
                 "location": <0.0-1.0>, "visa": <0.0-1.0>}
}
Rules:
- "capabilities" are the 2-5 things success in the role requires (with importance
  weight); "evidence_needed" lists the concrete skills/experiences that would
  prove each one. This is the success profile, not a keyword dump.
- "visa" is "no_sponsorship" ONLY when the posting explicitly refuses sponsorship
  or demands citizenship/permanent residency; "sponsors" only when it explicitly
  offers; otherwise "silent".
- Low confidence (< 0.5) on any field you had to guess."""


def job_card_key(job) -> str:
    """Stable cross-tenant key: dedupe slug when present, else content hash,
    else source:external_id. All tenants' copies of one posting share one card."""
    slug = getattr(job, "cross_source_slug", None)
    if slug:
        return f"slug:{slug}"
    ch = getattr(job, "content_hash", None)
    if ch:
        return f"hash:{ch}"
    return f"ext:{getattr(job, 'source', '')}:{getattr(job, 'external_id', '')}"


def mint_job_card(job) -> Optional[dict]:
    """One LLM read of the posting → JobCard dict (no persistence)."""
    if not _mint_allowed():
        return None
    desc = (getattr(job, "description", "") or "")[:6000]
    user = (f"Title: {job.title}\nCompany: {job.company}\n"
            f"Location: {getattr(job, 'location', '') or 'unknown'}\n"
            f"Remote flag: {bool(getattr(job, 'remote', False))}\n\n"
            f"Posting:\n{desc}")
    card = _haiku_json(_JOB_CARD_SYSTEM, user)
    if not card or not isinstance(card.get("capabilities"), list):
        return None
    _register_mint()
    card["_version"] = JOB_CARD_VERSION
    card["_model"] = settings.card_mint_model
    return card


def get_or_mint_job_card(job, allow_mint: bool = True) -> Optional[dict]:
    """Read the shared card for this posting; mint + persist when missing."""
    from app.db.init_db import get_session
    from app.db.models import JobCardRow
    from sqlmodel import select

    key = job_card_key(job)
    with get_session() as session:
        row = session.exec(select(JobCardRow).where(JobCardRow.card_key == key)).first()
        if row is not None:
            try:
                return json.loads(row.payload)
            except Exception:
                pass  # corrupt payload → re-mint below
    if not allow_mint:
        return None
    card = mint_job_card(job)
    if card is None:
        return None
    with get_session() as session:
        row = session.exec(select(JobCardRow).where(JobCardRow.card_key == key)).first()
        if row is None:
            row = JobCardRow(card_key=key)
        row.version = JOB_CARD_VERSION
        row.model = settings.card_mint_model
        row.payload = json.dumps(card)
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
    return card


# ── UserCard ──────────────────────────────────────────────────────────────────

_USER_CARD_SYSTEM = """You turn ONE candidate (résumé + profile facts) into a
machine-readable UserCard. Judge only from the material given. Return a single
JSON object, no prose, exactly this shape:
{
  "skills": [
    {"name": "<lowercase skill>",
     "evidence": <0.0-1.0>,
     "basis": "<skills-list | project | production | recent-production>"}
  ],
  "years_experience": <int>,
  "effective_level": "<junior | mid | senior | staff+>",
  "effective_level_confidence": <0.0-1.0>,
  "level_rationale": "<one sentence: impact/scale/ownership evidence>",
  "role_families": ["<target role family>", ...],
  "domains": ["<domain tag>", ...]
}
Evidence rubric (Layer 3 — depth, not presence):
- 0.3-0.45: named only in a skills list
- 0.5-0.7: used in a real project
- 0.75-0.9: production use
- 0.9-1.0: recent production use with stated scale/impact
effective_level weighs impact + scale + ownership, not just years — a 2-year
engineer who built and owned a system serving real users can be "mid"; never
inflate past what the evidence shows."""


def user_card_material(profile, resume_text: str) -> str:
    p = profile

    def g(n, d=""):
        return (getattr(p, n, d) or d) if p is not None else d

    return (f"Target roles: {g('target_roles')}\n"
            f"Current title: {g('current_title')}\n"
            f"Stated years of experience: {g('years_experience', 0)}\n"
            f"Stated key skills: {g('key_skills')}\n\n"
            f"Résumé:\n{(resume_text or '')[:12000]}")


def resume_hash(profile, resume_text: str) -> str:
    return hashlib.sha256(user_card_material(profile, resume_text).encode()).hexdigest()[:16]


def compile_user_card(profile, resume_text: str) -> Optional[dict]:
    if not _mint_allowed():
        return None
    card = _haiku_json(_USER_CARD_SYSTEM, user_card_material(profile, resume_text),
                       max_tokens=1200)
    if not card or not isinstance(card.get("skills"), list):
        return None
    _register_mint()
    card["_version"] = USER_CARD_VERSION
    card["_model"] = settings.card_mint_model
    # Deterministic profile facts ride along verbatim — the LLM never decides
    # work authorization or location preferences; the profile does.
    if profile is not None:
        card["_profile"] = {
            "requires_sponsorship": bool(getattr(profile, "requires_sponsorship", False)),
            "work_authorization": (getattr(profile, "work_authorization", "")
                                   or getattr(profile, "work_auth_status", "") or ""),
            "preferred_country": (getattr(profile, "preferred_country", "") or "").lower(),
            "remote_ok": bool(getattr(profile, "remote_ok", True)),
            "open_to_relocation": bool(getattr(profile, "open_to_relocation", False)),
            "location": (getattr(profile, "location", "") or ""),
        }
    return card


def get_or_compile_user_card(user_id: Optional[str], profile, resume_text: str,
                             allow_mint: bool = True) -> Optional[dict]:
    """Read the user's card; recompile when the résumé/profile material changed."""
    from app.db.init_db import get_session
    from app.db.models import UserCardRow
    from sqlmodel import select

    uid = user_id or "local"
    want_hash = resume_hash(profile, resume_text)
    with get_session() as session:
        row = session.exec(select(UserCardRow).where(UserCardRow.user_id == uid)).first()
        if row is not None and row.resume_hash == want_hash:
            try:
                return json.loads(row.payload)
            except Exception:
                pass
    if not allow_mint:
        return None
    card = compile_user_card(profile, resume_text)
    if card is None:
        return None
    with get_session() as session:
        row = session.exec(select(UserCardRow).where(UserCardRow.user_id == uid)).first()
        if row is None:
            row = UserCardRow(user_id=uid)
        row.version = USER_CARD_VERSION
        row.model = settings.card_mint_model
        row.resume_hash = want_hash
        row.payload = json.dumps(card)
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
    return card
