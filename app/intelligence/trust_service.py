"""Trust Profile service — gather signals, compute, persist.

Wires the pure scorer in ``trust.py`` to live data:
  * GitHub via the existing harvester (network, cheap)
  * resume presence (Supabase Storage / local file)
  * grounding ratio (passed in by the tailoring pipeline when it runs)

Safe to call on every profile save / resume upload. Never raises into callers.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import UserProfile
from app.intelligence.trust import compute_trust_profile

log = logging.getLogger(__name__)


def _has_resume(user_id: Optional[str]) -> bool:
    """True if the user has a résumé (Supabase Storage in prod, local file in dev)."""
    if settings.use_supabase and user_id and user_id != "local":
        try:
            from app.db.supabase_client import service_client
            sb = service_client()
            files = sb.storage.from_("resume").list(user_id)
            return any((f.get("name") or "").startswith("resume.") for f in (files or []))
        except Exception:
            return False
    import glob
    return bool(glob.glob("./data/resume_master.*"))


def compute_and_store(user_id: Optional[str],
                      grounding_score: Optional[float] = None) -> Optional[dict]:
    """Recompute the Trust Profile for one user and persist it.

    ``grounding_score`` (0-1) is supplied by the tailoring pipeline after a real
    grounding run. On a plain profile-save recompute it's None — in that case we
    preserve the previously-computed consistency score rather than reverting it
    to the "pending" partial credit, so a genuine score never silently drops.

    Returns a small summary dict (tier + overall) or None on failure.
    """
    try:
        with get_session() as session:
            profile = session.exec(
                select(UserProfile).where(UserProfile.user_id == user_id)
            ).first()
            if not profile:
                return None

            # GitHub signal (graceful: empty dict if no URL / private / error)
            github = None
            if (profile.github_url or "").strip():
                try:
                    from app.intelligence.harvester import harvest_github
                    github = harvest_github(profile.github_url)
                except Exception as e:
                    log.debug("trust: github harvest failed for %s: %s", user_id, e)

            has_resume = _has_resume(user_id)

            # A real grounding ratio (from a tailoring run) is stored in its own
            # column. When tailoring supplies a fresh one, persist it. When this is
            # a plain recompute (arg is None), fall back to the stored REAL ratio —
            # never to the pending placeholder, so "pending" can't masquerade as a
            # real "X% grounded" measurement.
            if grounding_score is not None:
                profile.resume_grounded_ratio = grounding_score
            else:
                grounding_score = profile.resume_grounded_ratio

            tp = compute_trust_profile(
                profile, github=github,
                grounding_score=grounding_score, has_resume=has_resume,
            )

            profile.trust_identity_score = tp.identity.score
            profile.trust_technical_score = tp.technical.score
            profile.trust_consistency_score = tp.consistency.score
            profile.trust_activity_score = tp.activity.score
            profile.trust_completeness_score = tp.completeness.score
            profile.trust_tier = tp.tier
            profile.trust_evidence = tp.evidence_json()
            profile.trust_computed_at = datetime.utcnow()
            session.add(profile)

            # Append a history snapshot when the overall score changes, so we can
            # show Momentum later without spamming the table on every recompute.
            from app.db.models import TrustHistory
            last = session.exec(
                select(TrustHistory).where(TrustHistory.user_id == user_id)
                .order_by(TrustHistory.created_at.desc())
            ).first()
            if last is None or last.overall != tp.overall:
                session.add(TrustHistory(
                    user_id=user_id, overall=tp.overall, tier=tp.tier,
                    identity=tp.identity.score, technical=tp.technical.score,
                    consistency=tp.consistency.score, activity=tp.activity.score,
                    completeness=tp.completeness.score,
                ))
            session.commit()

            log.info("trust: %s -> %s (%d)", user_id, tp.tier or "—", tp.overall)
            return {"tier": tp.tier, "overall": tp.overall}
    except Exception as e:
        log.warning("trust: compute_and_store failed for %s: %s", user_id, e)
        return None
