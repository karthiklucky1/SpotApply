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
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

JOB_CARD_VERSION = 1
# v2 adds the `evidence` claim list. v1 cards carried skills only — 27 tech
# nouns — which is what every string route had to score against; the semantic
# route in skill_graph has nothing to compare until a card carries claims. The
# bump forces a recompile per user (8 rows at ~$0.005, the cheap side of the
# ledger) and leaves all ~30-40k JobCards untouched.
USER_CARD_VERSION = 2

# ── Mint-cap accounting (process-local, mirrors reranker budget style) ───────
_mint_lock = threading.Lock()
_mints_today = {"day": "", "count": 0}


def _roll_day() -> None:
    """Caller must hold _mint_lock."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _mints_today["day"] != today:
        _mints_today["day"], _mints_today["count"] = today, 0


def _mint_allowed() -> bool:
    """Read-only predicate: would a mint be permitted right now? Callers that
    are about to spend use _reserve_mint() instead — checking here and
    incrementing after the call let 20 workers all pass before any of them
    incremented, so the cap overshot by the whole pool size."""
    from app.matching.reranker import llm_budget_exhausted
    if llm_budget_exhausted():
        return False
    cap = int(getattr(settings, "card_mint_daily_cap", 0) or 0)
    if cap <= 0:
        return True
    with _mint_lock:
        _roll_day()
        return _mints_today["count"] < cap


def _reserve_mint() -> bool:
    """Take a slot BEFORE the LLM call — check-and-increment under one lock, so
    the cap is a reservation rather than a receipt. Released again by
    _release_mint() when the call fails or the response is unusable, so a
    rejected card still never consumes the cap."""
    from app.matching.reranker import llm_budget_exhausted
    if llm_budget_exhausted():
        return False
    cap = int(getattr(settings, "card_mint_daily_cap", 0) or 0)
    with _mint_lock:
        _roll_day()
        if cap > 0 and _mints_today["count"] >= cap:
            return False
        _mints_today["count"] += 1
        return True


def _release_mint() -> None:
    with _mint_lock:
        _mints_today["count"] = max(0, _mints_today["count"] - 1)


def _register_mint() -> None:
    """Kept for callers that mint outside _reserve_mint()'s flow."""
    with _mint_lock:
        _roll_day()
        _mints_today["count"] += 1


# ── Per-key compile locks (the thundering herd) ──────────────────────────────
#
# Both getters are read -> miss -> LLM -> write with no coordination, and they
# run inside the scoring lane's worker pool (scoring_lane.py:435, 20 threads).
# Bumping a card VERSION invalidates every row at the same instant, so the first
# tick after a bump is the worst case: measured 20 of 20 workers minting the
# same user's card, 6 of them dying on IntegrityError inside the shadow hook's
# blanket except — one mint's worth of value, twenty mints' worth of spend, and
# six lost ledger rows.
#
# A claim-and-skip (app/common/inflight) is wrong here: the 19 losers would
# return None and write no shadow row. They must WAIT and then re-read what the
# winner wrote — double-checked locking, one mint, every caller served.
_MINT_LOCK_TIMEOUT = 45.0        # a mint is ~2-5s; past this, stop pinning threads
_MAX_KEY_LOCKS = 512
_key_locks: dict = {}
_key_locks_guard = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _key_locks_guard:
        lk = _key_locks.get(key)
        if lk is None:
            if len(_key_locks) >= _MAX_KEY_LOCKS:
                # Only drop locks nobody holds — evicting a held one would let a
                # second waiter mint in parallel, which is the bug this fixes.
                for k in [k for k, v in _key_locks.items() if not v.locked()]:
                    del _key_locks[k]
            lk = _key_locks[key] = threading.Lock()
        return lk


@contextmanager
def _minting(key: str):
    """``with _minting(key) as owned:`` — owned=False means the wait timed out;
    the caller re-reads the cache and gives up rather than minting anyway."""
    lk = _key_lock(key)
    owned = lk.acquire(timeout=_MINT_LOCK_TIMEOUT)
    try:
        yield owned
    finally:
        if owned:
            lk.release()


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
    if not _reserve_mint():
        return None
    desc = (getattr(job, "description", "") or "")[:6000]
    user = (f"Title: {job.title}\nCompany: {job.company}\n"
            f"Location: {getattr(job, 'location', '') or 'unknown'}\n"
            f"Remote flag: {bool(getattr(job, 'remote', False))}\n\n"
            f"Posting:\n{desc}")
    card = _haiku_json(_JOB_CARD_SYSTEM, user)
    if not card or not isinstance(card.get("capabilities"), list):
        _release_mint()          # a rejected card must not consume the cap
        return None
    card["_version"] = JOB_CARD_VERSION
    card["_model"] = settings.card_mint_model
    return card


def _read_job_card(key: str) -> Optional[dict]:
    """Cached card for this posting, or None. A stored card is only reusable if
    it was minted by the CURRENT schema — without the version check, bumping
    JOB_CARD_VERSION (which you do precisely when the card shape changes) kept
    serving old-shape payloads to match_cards() forever, so the constant did
    nothing and the agreement data feeding the calibration gates was silently
    mixed-schema."""
    from app.db.init_db import get_session
    from app.db.models import JobCardRow
    from sqlmodel import select

    with get_session() as session:
        row = session.exec(select(JobCardRow).where(JobCardRow.card_key == key)).first()
        if row is not None and row.version == JOB_CARD_VERSION:
            try:
                return json.loads(row.payload)
            except Exception:
                return None      # corrupt payload → re-mint
    return None


def get_or_mint_job_card(job, allow_mint: bool = True) -> Optional[dict]:
    """Read the shared card for this posting; mint + persist when missing."""
    from app.db.init_db import get_session
    from app.db.models import JobCardRow
    from sqlmodel import select

    key = job_card_key(job)
    card = _read_job_card(key)
    if card is not None or not allow_mint:
        return card

    # One mint per posting per process, even with 20 tenants scoring the same
    # job in the same tick. Losers wait, then re-read what the winner wrote —
    # they must NOT skip, or they write no shadow row.
    with _minting(f"job:{key}") as owned:
        card = _read_job_card(key)
        if card is not None:
            return card
        if not owned:
            log.debug("job card mint lock timed out for %s", key)
            return None
        card = mint_job_card(job)
        if card is None:
            return None
        started = datetime.utcnow()
        try:
            with get_session() as session:
                row = session.exec(
                    select(JobCardRow).where(JobCardRow.card_key == key)).first()
                if row is None:
                    row = JobCardRow(card_key=key)
                elif row.updated_at > started:
                    return card  # a fresher writer won; don't clobber it
                row.version = JOB_CARD_VERSION
                row.model = settings.card_mint_model
                row.payload = json.dumps(card)
                row.updated_at = datetime.utcnow()
                session.add(row)
                session.commit()
        except Exception as e:
            # Another PROCESS (or replica) inserted the same key first. The card
            # in hand is still valid — return it rather than losing the mint we
            # already paid for and the ledger row that depends on it.
            log.debug("job card write lost a race for %s: %s", key, e)
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
  "evidence": [
    {"claim": "<what the candidate has DONE, one capability per line>",
     "strength": <0.0-1.0>,
     "basis": "<project | production | recent-production>"}
  ],
  "years_experience": <int>,
  "effective_level": "<junior | mid | senior | staff+>",
  "effective_level_confidence": <0.0-1.0>,
  "level_rationale": "<one sentence: impact/scale/ownership evidence>",
  "role_families": ["<target role family>", ...],
  "domains": ["<domain tag>", ...]
}
Evidence rubric (Layer 3 — depth, not presence), for BOTH lists:
- 0.3-0.45: named only in a skills list
- 0.5-0.7: used in a real project
- 0.75-0.9: production use
- 0.9-1.0: recent production use with stated scale/impact

"evidence" is the important list: 10-20 claims that a job requirement could be
matched AGAINST. Write each one the way a posting words a requirement — a verb,
the thing done, the technology, the scale where the résumé states it:
  GOOD: "built and deployed FastAPI backends on AWS ECS serving production traffic"
  GOOD: "managed Kubernetes workloads and CI/CD pipelines on GCP"
  GOOD: "translated requirements across product, compliance and business teams"
  BAD:  "python"          <- that is a skill, it belongs in "skills"
  BAD:  "strong engineer" <- no capability, nothing to match against
Cover NON-technical capabilities too (collaboration, mentoring, ownership,
stakeholder communication, on-call) whenever the résumé shows them: those are
real requirements in postings and a skills list can never carry them.
Every claim must be supported by the material — do not invent scale, seniority
or technologies the résumé does not state.

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


def profile_facts(profile) -> dict:
    """The deterministic facts stamped onto every UserCard.

    Deliberately NOT part of the LLM prompt — the model never decides work
    authorization or location preferences, the profile does. But they ARE part of
    the cache key, because card_match's work_auth and location factors read
    nothing else. Without them in the hash, a user who got a green card or
    switched preferred country kept a cached card carrying the old facts and went
    on being gated by them indefinitely: their résumé text had not changed, so
    nothing ever triggered a recompile.
    """
    if profile is None:
        return {}
    return {
        "requires_sponsorship": bool(getattr(profile, "requires_sponsorship", False)),
        "work_authorization": (getattr(profile, "work_authorization", "")
                               or getattr(profile, "work_auth_status", "") or ""),
        # card_match reads this alongside work_authorization, the same pair
        # intelligence/work_auth.py assesses — a clearance or visa category
        # recorded only here would otherwise be invisible to the matcher.
        "visa_status": (getattr(profile, "visa_status", "") or ""),
        "preferred_country": (getattr(profile, "preferred_country", "") or "").lower(),
        "remote_ok": bool(getattr(profile, "remote_ok", True)),
        "open_to_relocation": bool(getattr(profile, "open_to_relocation", False)),
        "location": (getattr(profile, "location", "") or ""),
    }


def resume_hash(profile, resume_text: str) -> str:
    material = user_card_material(profile, resume_text)
    facts = json.dumps(profile_facts(profile), sort_keys=True)
    return hashlib.sha256(f"{material}\n--facts--\n{facts}".encode()).hexdigest()[:16]


def compile_user_card(profile, resume_text: str) -> Optional[dict]:
    if not _reserve_mint():
        return None
    card = _haiku_json(_USER_CARD_SYSTEM, user_card_material(profile, resume_text),
                       max_tokens=1600)
    if not card or not isinstance(card.get("skills"), list):
        _release_mint()          # a rejected card must not consume the cap
        return None
    card["_version"] = USER_CARD_VERSION
    card["_model"] = settings.card_mint_model
    # Deterministic profile facts ride along verbatim — the LLM never decides
    # work authorization or location preferences; the profile does. Same helper
    # the cache key uses, so what the card carries and what invalidates it can
    # never drift apart.
    if profile is not None:
        card["_profile"] = profile_facts(profile)
    return card


def get_or_compile_user_card(user_id: Optional[str], profile, resume_text: str,
                             allow_mint: bool = True) -> Optional[dict]:
    """Read the user's card; recompile when the résumé/profile material changed."""
    from app.db.init_db import get_session
    from app.db.models import UserCardRow
    from sqlmodel import select

    # FAIL CLOSED on identity. `user_id or "local"` mapped every None-owner job to
    # the local/founder card row, which in Supabase mode means reading one
    # identity's compiled résumé card for another user's match — and writing that
    # user's material back over it. NULL-owner Job rows do occur, so this was
    # reachable. Only single-user local mode gets the "local" identity.
    uid = user_id or ("local" if not settings.use_supabase else None)
    if not uid:
        log.warning("UserCard requested with no user_id in multi-tenant mode — "
                    "refusing (would read/write the 'local' identity's card).")
        return None
    want_hash = resume_hash(profile, resume_text)

    def _read() -> Optional[dict]:
        # Version AND material must both match — see _read_job_card.
        with get_session() as session:
            row = session.exec(
                select(UserCardRow).where(UserCardRow.user_id == uid)).first()
            if (row is not None and row.resume_hash == want_hash
                    and row.version == USER_CARD_VERSION):
                try:
                    return json.loads(row.payload)
                except Exception:
                    return None
        return None

    card = _read()
    if card is not None or not allow_mint:
        return card

    # One compile per user per process. A version bump invalidates every user's
    # card simultaneously, so without this the first tick after a bump costs
    # `scoring_workers` compiles per user instead of one.
    with _minting(f"user:{uid}") as owned:
        card = _read()
        if card is not None:
            return card
        if not owned:
            log.debug("user card compile lock timed out for %s", uid)
            return None
        card = compile_user_card(profile, resume_text)
        if card is None:
            return None
        started = datetime.utcnow()
        try:
            with get_session() as session:
                row = session.exec(
                    select(UserCardRow).where(UserCardRow.user_id == uid)).first()
                if row is None:
                    row = UserCardRow(user_id=uid)
                elif row.updated_at > started:
                    # Someone wrote AFTER we began, so their material is at least
                    # as fresh as ours. Overwriting would leave the row claiming
                    # to be current for material that is stale — the user then
                    # stays gated on old facts with nothing to trigger a redo.
                    return card
                row.version = USER_CARD_VERSION
                row.model = settings.card_mint_model
                row.resume_hash = want_hash
                row.payload = json.dumps(card)
                row.updated_at = datetime.utcnow()
                session.add(row)
                session.commit()
        except Exception as e:
            log.debug("user card write lost a race for %s: %s", uid, e)
        return card
