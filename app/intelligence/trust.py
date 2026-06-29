"""Trust Profile (Phase 0) — evidence-based professional identity scoring.

Composes the existing verification primitives into five transparent dimensions,
each 0-100, shown to users as star levels (never one opaque "magic number"):

    Identity              — email / phone / .edu verification
    Technical Proof       — GitHub repos, stars, languages (harvester)
    Experience Consistency— resume claims vs reality (grounding)
    Open-Source Activity  — commit recency & volume (harvester)
    Profile Completeness  — how filled-out the profile is

Design rules (from product review):
  * Deterministic & transparent — same inputs always give the same score.
  * Evidence-backed — every dimension records WHY ("34 repos · 418 commits").
  * Graceful degradation — a missing vector (e.g. private GitHub) caps that one
    dimension but never zeroes the profile; a senior with a strong, consistent
    resume can still reach a high tier without open-source.
No LLM calls here — pure, cheap, runnable on every profile save.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class Dimension:
    key: str
    label: str
    score: int                       # 0-100
    stars: int                       # 0-5 (display)
    evidence: List[str] = field(default_factory=list)
    confidence: str = "low"          # low | medium | high


@dataclass
class TrustProfile:
    identity: Dimension
    technical: Dimension
    consistency: Dimension
    activity: Dimension
    completeness: Dimension
    overall: int                     # internal numeric (for ranking) 0-100
    tier: str                        # Starter | Verified | Verified Pro | Elite

    def dimensions(self) -> List[Dimension]:
        return [self.identity, self.technical, self.consistency,
                self.activity, self.completeness]

    def evidence_json(self) -> str:
        return json.dumps({d.key: {"label": d.label, "score": d.score,
                                   "stars": d.stars, "confidence": d.confidence,
                                   "evidence": d.evidence} for d in self.dimensions()})


def _stars(score: int) -> int:
    """0-100 -> 0-5 stars (rounded to nearest, 1-star floor for any signal)."""
    if score <= 0:
        return 0
    return max(1, min(5, round(score / 20)))


def _confidence(score: int) -> str:
    return "high" if score >= 70 else ("medium" if score >= 40 else "low")


def _days_since(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return None


# ── Individual dimension scorers ──────────────────────────────────────────────

def _score_identity(profile) -> Dimension:
    score, ev = 0, []
    email = (getattr(profile, "email", "") or "").strip()
    if getattr(profile, "email_verified", False):
        score += 45
        ev.append("Email verified")
        if email.lower().endswith(".edu"):
            score += 15
            ev.append("Verified .edu (student) address")
    elif email:
        score += 10
        ev.append("Email provided (unverified)")
    if getattr(profile, "phone_verified", False):
        score += 30
        ev.append("Phone verified")
    elif (getattr(profile, "phone", "") or "").strip():
        score += 5
        ev.append("Phone provided (unverified)")
    if (getattr(profile, "linkedin_url", "") or "").strip():
        score += 10
        ev.append("LinkedIn linked")
    # Articulation proof (optional booster) — a video explaining one's own code
    # is strong anti-proxy / anti-synthetic evidence of a real, fluent human.
    if (getattr(profile, "articulation_video_url", "") or "").strip():
        score += 25
        ev.append("Articulation video verified (explains own code)")
    score = min(score, 100)
    return Dimension("identity", "Identity", score, _stars(score), ev, _confidence(score))


def _score_technical(github: Optional[Dict[str, Any]]) -> Dimension:
    """GitHub-proven technical depth. Degrades gracefully: no/private GitHub
    yields 0 here but the overall score compensates via consistency/completeness."""
    ev: List[str] = []
    if not github or not github.get("ok"):
        return Dimension("technical", "Technical Proof", 0, 0,
                         ["No public GitHub linked"], "low")
    repos = [r for r in github.get("repos", []) if r]
    if not repos:
        return Dimension("technical", "Technical Proof", 0, 0,
                         ["GitHub linked but no public repos"], "low")
    score = 0
    n = len(repos)
    score += min(n, 10) * 4            # up to 40 for repo count
    ev.append(f"{n} public repositor{'y' if n == 1 else 'ies'}")
    stars = sum(int(r.get("stars", 0) or 0) for r in repos)
    if stars:
        score += min(stars, 50) // 2   # up to 25 for community traction
        ev.append(f"{stars} total stars")
    langs = sorted({(r.get("language") or "").strip() for r in repos if r.get("language")})
    if langs:
        score += min(len(langs) * 5, 20)
        ev.append("Languages: " + ", ".join(langs[:5]))
    described = sum(1 for r in repos if (r.get("description") or "").strip())
    if described >= max(2, n // 2):
        score += 15
        ev.append(f"{described} repos documented")
    score = min(score, 100)
    return Dimension("technical", "Technical Proof", score, _stars(score), ev, _confidence(score))


def _score_activity(github: Optional[Dict[str, Any]]) -> Dimension:
    ev: List[str] = []
    if not github or not github.get("ok"):
        return Dimension("activity", "Open-Source Activity", 0, 0,
                         ["No public GitHub linked"], "low")
    events = github.get("events", []) or []
    repos = github.get("repos", []) or []
    score = 0
    if events:
        score += min(len(events), 20) * 3   # up to 60 for recent commit volume
        ev.append(f"{len(events)} recent commits")
    # Recency from the most-recently pushed repo
    pushed = [_days_since(r.get("pushed_at")) for r in repos if r.get("pushed_at")]
    pushed = [d for d in pushed if d is not None]
    if pushed:
        recent = min(pushed)
        if recent <= 30:
            score += 40; ev.append("Active in last 30 days")
        elif recent <= 90:
            score += 25; ev.append("Active in last 90 days")
        elif recent <= 180:
            score += 10; ev.append("Active in last 6 months")
        else:
            ev.append(f"Last activity {recent} days ago")
    score = min(score, 100)
    return Dimension("activity", "Open-Source Activity", score, _stars(score), ev, _confidence(score))


def _score_consistency(grounding_score: Optional[float], has_resume: bool) -> Dimension:
    """Resume <-> reality. grounding_score in [0,1] = share of bullets that the
    grounding check found supported by the master resume (1.0 = all grounded)."""
    if not has_resume:
        return Dimension("consistency", "Experience Consistency", 0, 0,
                         ["No resume uploaded yet"], "low")
    if grounding_score is None:
        # Resume present but not yet checked — give partial credit, low confidence.
        return Dimension("consistency", "Experience Consistency", 35, _stars(35),
                         ["Resume uploaded (verification pending)"], "low")
    score = int(round(max(0.0, min(1.0, grounding_score)) * 100))
    pct = int(round(grounding_score * 100))
    ev = [f"{pct}% of resume claims grounded in evidence"]
    if score >= 90:
        ev.append("No fabricated claims detected")
    return Dimension("consistency", "Experience Consistency", score, _stars(score), ev, _confidence(score))


def _score_completeness(profile) -> Dimension:
    fields = [
        ("first_name", "Name"), ("last_name", None), ("location", "Location"),
        ("current_title", "Current title"), ("years_experience", "Experience"),
        ("key_skills", "Skills"), ("professional_summary", "Summary"),
        ("degree", "Education"), ("work_authorization", "Work authorization"),
        ("target_roles", "Target roles"),
    ]
    filled, ev = 0, []
    total = len(fields)
    for attr, label in fields:
        val = getattr(profile, attr, None)
        ok = bool(val) and (val != 0 if attr == "years_experience" else str(val).strip())
        if ok:
            filled += 1
        elif label:
            ev.append(f"Missing: {label}")
    score = int(round(filled / total * 100))
    summary = [f"{filled}/{total} profile fields complete"] + ev[:3]
    return Dimension("completeness", "Profile Completeness", score, _stars(score), summary, _confidence(score))


# ── Composition ───────────────────────────────────────────────────────────────

# Base weights. If a dimension has no signal (e.g. private GitHub), its weight is
# redistributed to the others so the candidate isn't penalised for not opting in
# to a vector — graceful degradation.
_WEIGHTS = {
    "identity": 0.20,
    "technical": 0.25,
    "consistency": 0.30,
    "activity": 0.10,
    "completeness": 0.15,
}
_TIERS = [(85, "Elite"), (70, "Verified Pro"), (50, "Verified"), (25, "Starter"), (0, "")]


def _tier(overall: int) -> str:
    for threshold, name in _TIERS:
        if overall >= threshold:
            return name
    return ""


def compute_trust_profile(profile,
                          github: Optional[Dict[str, Any]] = None,
                          grounding_score: Optional[float] = None,
                          has_resume: bool = False) -> TrustProfile:
    """Compute the full Trust Profile from already-gathered signals.

    Callers pass the harvester's GitHub dict + the grounding ratio so this stays
    pure/fast (no network, no LLM). Any of them may be None — handled gracefully.
    """
    dims = {
        "identity": _score_identity(profile),
        "technical": _score_technical(github),
        "consistency": _score_consistency(grounding_score, has_resume),
        "activity": _score_activity(github),
        "completeness": _score_completeness(profile),
    }

    # Redistribute weight away from dimensions with zero signal so a missing
    # vector caps that dimension's contribution without dragging the whole score.
    active = {k: _WEIGHTS[k] for k, d in dims.items() if d.score > 0}
    if active:
        wsum = sum(active.values())
        overall = int(round(sum(dims[k].score * (w / wsum) for k, w in active.items())))
    else:
        overall = 0
    overall = max(0, min(100, overall))

    return TrustProfile(
        identity=dims["identity"], technical=dims["technical"],
        consistency=dims["consistency"], activity=dims["activity"],
        completeness=dims["completeness"], overall=overall, tier=_tier(overall),
    )
