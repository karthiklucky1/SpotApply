"""CardRace v2 deterministic matcher g(UserCard, JobCard) — docs/CARDRACE_DESIGN.md §2.2, §9.

Pure CPU arithmetic producing the SAME four-factor shape the Claude reranker
emits (skills / experience / location / work_auth, each {"score","note"}), so
every downstream consumer (rerank_breakdown, blended_score, dashboard) reads it
unchanged.

Two brains (§9.4): this module is Brain 1 — the deterministic 90%. Everything
it cannot decide confidently is *routed*, never guessed:
- every pair is scored TWICE — S_direct (direct evidence only) and S_expanded
  (with skill-graph inference); the spread measures how much of the score is
  assumption (§9.5 disagreement detector);
- low-confidence card fields poison auto-decisions (the caller bands them).

No LLM, no network, no DB in this module: pure functions of two dicts —
microseconds per pair, and trivially testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.matching.skill_graph import SkillGraph, load_graph

# Factor weights mirror the Claude rubric's implied emphasis. Named numbers on
# purpose — calibration reads them, humans can audit them.
WEIGHTS = {"skills": 0.45, "experience": 0.25, "location": 0.15, "work_auth": 0.15}
# Any factor at/below this is a hard blocker; the overall is then capped, exactly
# like the LLM contract ("a hard blocker caps the overall score low regardless").
BLOCKER_FLOOR = 15.0
BLOCKER_OVERALL_CAP = 25.0

_LEVELS = {"intern": 0, "junior": 1, "mid": 2, "senior": 3, "staff+": 4}


@dataclass
class CardMatchResult:
    direct: float                       # overall, direct evidence only
    expanded: float                     # overall, with graph inference
    spread: float                       # expanded - direct: how much is assumption
    breakdown: Dict[str, Dict]          # expanded four factors {"score","note"}
    breakdown_direct: Dict[str, Dict]
    blockers: List[str] = field(default_factory=list)
    low_confidence: List[str] = field(default_factory=list)  # fields unsafe to auto-decide


def _num(x, default: float = 0.0) -> float:
    """Tolerant numeric coercion for LLM-provided fields.

    Cards are model output: `years_experience` arrives as "5+", `importance` as
    "high", scores as None. Raising here meant the pair contributed no shadow row
    at all (the exception was swallowed upstream at DEBUG), silently biasing the
    calibration set. Fall back instead."""
    if isinstance(x, bool) or x is None:
        return default
    if isinstance(x, (int, float)):
        return float(x)
    try:
        import re as _re
        m = _re.search(r"-?\d+(?:\.\d+)?", str(x))
        return float(m.group(0)) if m else default
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ── work_auth ────────────────────────────────────────────────────────────────

def _work_auth_factor(user_card: dict, job_card: dict) -> Tuple[float, str]:
    prof = user_card.get("_profile") or {}
    needs = bool(prof.get("requires_sponsorship", False))
    wa = f"{prof.get('work_authorization') or ''} {prof.get('visa_status') or ''}".lower()
    # "US citizens only" and "citizens OR PERMANENT RESIDENTS only" are different
    # bars, and conflating them is wrong in both directions. The first is real
    # ITAR/cleared work that genuinely excludes a green-card holder; the second
    # admits them. So resolve citizenship and permanent residency separately on
    # both sides and require the posting itself to accept PR before PR counts.
    # (Same taxonomy as intelligence/work_auth.py — keep the two in step.)
    _negated = ("not " in wa) or ("non-" in wa) or ("non " in wa)
    is_citizen = (not _negated) and "citizen" in wa
    is_pr = (not _negated) and any(
        t in wa for t in ("green card", "permanent resident", "lawful permanent", "lpr")
    )
    visa = (job_card.get("visa") or "silent").lower()
    disq = " | ".join(str(d).lower() for d in (job_card.get("disqualifiers") or []))
    wants_clearance = "clearance" in disq
    citizens_only = "citizen" in disq or "permanent resident" in disq
    pr_accepted = any(
        t in disq for t in ("permanent resident", "green card", "lawful permanent")
    )

    if wants_clearance and "clearance" not in wa:
        return 8.0, "role requires a security clearance the candidate does not list"
    if citizens_only and not (is_citizen or (is_pr and pr_accepted)):
        if is_pr and not pr_accepted:
            return 8.0, "posting restricts to U.S. citizens only; candidate is a permanent resident"
        return 8.0, (
            "posting restricts to citizens/permanent residents"
            if pr_accepted else "posting restricts to U.S. citizens"
        )

    if needs:
        if visa == "no_sponsorship":
            return 8.0, "candidate needs sponsorship; posting explicitly refuses it"
        if visa == "sponsors":
            return 95.0, "candidate needs sponsorship; employer explicitly sponsors"
        return 85.0, "candidate needs sponsorship; posting is silent — assumed possible"
    return 92.0, "no sponsorship needed; no citizenship/clearance conflict"


# ── location ─────────────────────────────────────────────────────────────────

def _location_factor(user_card: dict, job_card: dict) -> Tuple[float, str]:
    from app.common.geo import norm_country
    prof = user_card.get("_profile") or {}
    # Both sides go through norm_country: the JobCard's country is free-form LLM
    # output ("us", "usa", "u.s."), the profile's comes from a fixed UI list
    # ("United States"). Raw string equality made a flawless US candidate for a
    # US role score 25 (location 10 = hard blocker), and the old substring test
    # false-matched "us" inside "australia" and "uk" inside "ukraine".
    want_country = norm_country(prof.get("preferred_country") or "")
    remote_ok = bool(prof.get("remote_ok", True))
    relocate = bool(prof.get("open_to_relocation", False))
    _raw_country = (job_card.get("country") or "").strip().lower()
    country = norm_country(_raw_country) or ("unknown" if _raw_country in ("", "unknown") else _raw_country)
    policy = (job_card.get("remote_policy") or "unknown").lower()
    scope = (job_card.get("remote_scope") or "unknown").lower()

    # User chose no country → no country gate anywhere (config.py's own rule):
    # judge only on remote/onsite preference fit.
    if not want_country:
        if policy in ("remote", "unknown"):
            return 85.0, "no country preference set; remote/unspecified role"
        return 70.0, f"no country preference set; role is {policy}"

    if country in ("", "unknown"):
        if policy == "remote" and scope == "global":
            return 88.0, "globally-open remote role"
        return 60.0, "role location unclear from the posting"

    same = bool(country) and country == want_country
    if same:
        if policy == "remote":
            return 95.0, f"remote within {want_country}"
        if policy == "hybrid":
            return 78.0 if (relocate or not remote_ok) else 68.0, f"hybrid in {country}"
        if policy == "onsite":
            if relocate:
                return 85.0, f"on-site in {country}; candidate open to relocation"
            if not remote_ok:
                return 80.0, f"on-site in {country}; candidate prefers on-site"
            return 55.0, f"on-site in {country}; candidate prefers remote"
        return 85.0, f"in {country}, work model unspecified"

    # Different country. A remote role anchored to another country/region still
    # needs work authorization there — the rubric's hard blocker.
    if policy == "remote" and scope == "global":
        return 85.0, "remote, globally open"
    return 10.0, f"role is anchored to {country}, candidate wants {want_country}"


# ── experience ───────────────────────────────────────────────────────────────

def _experience_factor(user_card: dict, job_card: dict) -> Tuple[float, str]:
    years = int(_num(user_card.get("years_experience"), 0))
    eff_level = (user_card.get("effective_level") or "").lower()
    eff_conf = _num(user_card.get("effective_level_confidence"), 0.0)
    years_min = job_card.get("years_min")
    seniority = (job_card.get("seniority") or "unknown").lower()

    def level_bonus_ok() -> bool:
        # Layer 4: strong effective-level evidence closes small formal gaps.
        need = _LEVELS.get(seniority)
        have = _LEVELS.get(eff_level)
        return need is not None and have is not None and have >= need and eff_conf >= 0.6

    if years_min is None:
        need = _LEVELS.get(seniority)
        have = _LEVELS.get(eff_level)
        if need is None:
            return 80.0, "posting is silent on experience"
        if have is None:
            return 65.0, f"role targets {seniority}; candidate level unclear"
        diff = need - have
        if diff <= 0:
            return 88.0, f"candidate level ({eff_level}) meets the {seniority} bar"
        if diff == 1:
            return 62.0, f"one level below the {seniority} bar"
        # 2+ levels below = the rubric's "impossible seniority gap" — hard blocker.
        return 12.0, f"candidate level ({eff_level}) far below the {seniority} bar"

    gap = int(years_min) - years
    if gap <= 0:
        return 90.0, f"{years}y meets the {years_min}y requirement"
    if gap == 1:
        base, note = 78.0, f"{years}y vs {years_min}y asked — slight stretch"
    elif gap == 2:
        base, note = 58.0, f"{years}y vs {years_min}y asked — visible stretch"
    elif gap == 3:
        base, note = 38.0, f"{years}y vs {years_min}y asked — large gap"
    else:
        # >3y formal gap = hard blocker (caps the overall) — UNLESS the compile
        # found genuine level-equivalent evidence (Layer 4), which lifts it into
        # weak-but-band territory so Claude gets to judge the unusual candidate
        # instead of arithmetic silently rejecting them.
        if level_bonus_ok():
            return 35.0, (f"{years}y vs {years_min}y asked — but level-equivalent "
                          f"evidence ({user_card.get('level_rationale', '')[:80]})")
        return 12.0, f"{years}y vs {years_min}y asked — hard seniority gap"
    if level_bonus_ok():
        return _clamp(base + 15.0, 0.0, 85.0), note + f"; level-equivalent evidence ({user_card.get('level_rationale', '')[:80]})"
    return base, note


# ── skills ───────────────────────────────────────────────────────────────────

def _evidence_map(user_card: dict) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for s in user_card.get("skills") or []:
        # Tolerate a bare string ("python") as well as {"name","evidence"}: both
        # shapes pass the mint validator, and raising here cost a ledger row.
        if isinstance(s, str):
            name, ev = s.strip().lower(), 0.5
        elif isinstance(s, dict):
            name = str(s.get("name") or "").strip().lower()
            ev = _num(s.get("evidence"), 0.0)
        else:
            continue
        if not name:
            continue
        if ev > out.get(name, 0.0):
            out[name] = ev
    return out


def _capability_coverage(evidence: Dict[str, float], cap: dict, graph: SkillGraph,
                         use_inference: bool) -> Tuple[float, Optional[str]]:
    """Coverage of ONE capability: 0.7 x best proof + 0.3 x mean over its proofs.
    ``evidence_needed`` entries are alternative concrete proofs of the capability;
    the capability name itself also counts as a proof target."""
    wants = [str(w) for w in (cap.get("evidence_needed") or []) if str(w).strip()]
    name = str(cap.get("name") or "").strip()
    if name:
        wants = [name] + wants
    if not wants:
        return 0.0, None
    covs, best_via, best_cov = [], None, 0.0
    for w in wants:
        cov, via = graph.coverage(evidence, w, use_inference=use_inference)
        covs.append(cov)
        if cov > best_cov:
            best_cov, best_via = cov, via
    score = 0.7 * max(covs) + 0.3 * (sum(covs) / len(covs))
    return score, best_via


def _skills_factor(user_card: dict, job_card: dict, graph: SkillGraph,
                   use_inference: bool) -> Tuple[float, str, bool]:
    """Returns (score, note, judged) — judged=False when the card gave us nothing
    to judge with (caller marks the field low-confidence)."""
    caps = [c for c in (job_card.get("capabilities") or []) if isinstance(c, dict)]
    evidence = _evidence_map(user_card)
    if not caps:
        return 50.0, "posting yielded no capability profile", False
    if not evidence:
        return 20.0, "candidate card lists no skills", True

    total_w, acc = 0.0, 0.0
    notes: List[str] = []
    worst: Tuple[float, str] = (2.0, "")
    for cap in caps:
        w = _num(cap.get("importance"), 0.5)
        w = max(0.05, min(1.0, w))
        cov, via = _capability_coverage(evidence, cap, graph, use_inference)
        total_w += w
        acc += w * cov
        cname = str(cap.get("name") or "?")
        if cov >= 0.55 and via and graph.canon(via) != graph.canon(cname):
            notes.append(f"{cname}: covered via {via}")
        if cov < worst[0]:
            worst = (cov, cname)
    score = 100.0 * acc / total_w if total_w else 0.0

    nice = [str(n) for n in (job_card.get("nice_to_have") or []) if str(n).strip()]
    bonus = 0.0
    for n in nice[:6]:
        cov, _ = graph.coverage(evidence, n, use_inference=use_inference)
        if cov >= 0.6:
            bonus += 3.0
    score = _clamp(score + min(bonus, 9.0))

    if worst[0] < 0.4 and worst[1]:
        notes.append(f"gap: {worst[1]}")
    return score, ("; ".join(notes) if notes else "scored on direct skill overlap"), True


# ── overall ──────────────────────────────────────────────────────────────────

def _overall(factors: Dict[str, Tuple[float, str]]) -> Tuple[float, List[str]]:
    weighted = sum(WEIGHTS[k] * factors[k][0] for k in WEIGHTS)
    blockers = [f"{k}: {factors[k][1]}" for k in WEIGHTS if factors[k][0] <= BLOCKER_FLOOR]
    if blockers:
        weighted = min(weighted, BLOCKER_OVERALL_CAP)
    return round(_clamp(weighted), 1), blockers


def match_cards(user_card: dict, job_card: dict,
                graph: Optional[SkillGraph] = None) -> CardMatchResult:
    """The pair judgment. Deterministic; same inputs → same outputs, always."""
    g = graph if graph is not None else load_graph()

    wa = _work_auth_factor(user_card, job_card)
    loc = _location_factor(user_card, job_card)
    exp = _experience_factor(user_card, job_card)
    sk_dir, sk_dir_note, judged = _skills_factor(user_card, job_card, g, use_inference=False)
    sk_exp, sk_exp_note, _ = _skills_factor(user_card, job_card, g, use_inference=True)

    fx_direct = {"skills": (sk_dir, sk_dir_note), "experience": exp,
                 "location": loc, "work_auth": wa}
    fx_expanded = {"skills": (sk_exp, sk_exp_note), "experience": exp,
                   "location": loc, "work_auth": wa}

    direct, _ = _overall(fx_direct)
    expanded, blockers = _overall(fx_expanded)

    low_conf: List[str] = []
    conf = job_card.get("confidence") or {}
    for fld in ("skills", "experience", "location", "visa"):
        if _num(conf.get(fld, 1.0), 0.0) < 0.5:
            low_conf.append(fld)
    if not judged:
        low_conf.append("skills")
    if (user_card.get("effective_level_confidence") is not None
            and _num(user_card.get("effective_level_confidence"), 0.0) < 0.3
            and job_card.get("years_min") is None):
        low_conf.append("experience")

    def _bd(fx):
        return {k: {"score": round(v[0], 1), "note": v[1]} for k, v in fx.items()}

    return CardMatchResult(
        direct=direct,
        expanded=expanded,
        spread=round(expanded - direct, 1),
        breakdown=_bd(fx_expanded),
        breakdown_direct=_bd(fx_direct),
        blockers=blockers,
        low_confidence=sorted(set(low_conf)),
    )
