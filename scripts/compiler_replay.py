"""Compiler-layer replay experiment v2 — which job families are compilable,
and WHY does a compiled score still disagree with Claude?

v1 asked one question: can a linear program over skill features reproduce the
Claude finals already stored in the DB? (Answer on real data: no — rank rho
~0.25 everywhere.) v2 asks the three questions that decide the architecture:

  1. LIFT — do richer "reasoning" features close the gap? Every family is
     fitted TWICE: v1 features (skills + years) and v2 features (adds
     visa-refusal x needs-sponsorship, country mismatch, remote, seniority
     gap, overqualification, domain match). The report shows rho side by side.
  2. WHY — for pairs where the v2 program still disagrees with Claude by >=15
     points, classify the reason using Claude's own stored rerank_reasoning
     (no new LLM calls — Claude wrote down why at scoring time).
  3. VERDICT — if the remaining disagreement is dominated by nameable,
     deterministic causes (visa, seniority, location), those are worth
     engineering as features/gates. If "holistic/other" dominates, feature
     engineering is done — the distilled apprentice model is the path.

    python -m scripts.compiler_replay                 # against the real DB
    python -m scripts.compiler_replay --selftest      # synthetic end-to-end check

Fits are ridge, scored with exact leave-one-out predictions (hat-matrix
shortcut) — held-out numbers, not in-sample flattery. Zero LLM calls.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# ── Skill vocabulary with acceptance sets (FM-3: alias credit, not literal) ──
# Value = credit multiplier: evidence of "flask" partially satisfies "fastapi".
SKILLS: dict[str, dict[str, float]] = {
    "python":     {"python": 1.0},
    "java":       {"java": 1.0, "kotlin": 0.7},
    "javascript": {"javascript": 1.0, "typescript": 0.95, "node": 0.8, "nodejs": 0.8},
    "golang":     {"golang": 1.0, r"\bgo\b": 0.9},
    "fastapi":    {"fastapi": 1.0, "starlette": 0.9, "flask": 0.7, "django": 0.5},
    "react":      {"react": 1.0, "nextjs": 0.9, "next.js": 0.9, "vue": 0.5, "angular": 0.4},
    "sql":        {"postgres": 1.0, "postgresql": 1.0, "mysql": 0.9, "sql": 0.8, "sqlite": 0.6},
    "nosql":      {"mongodb": 1.0, "dynamodb": 0.9, "cassandra": 0.9, "redis": 0.7},
    "aws":        {"aws": 1.0, "amazon web services": 1.0, "ec2": 0.8, "lambda": 0.7, "s3": 0.7},
    "gcp":        {"gcp": 1.0, "google cloud": 1.0, "bigquery": 0.7},
    "azure":      {"azure": 1.0},
    "docker":     {"docker": 1.0, "container": 0.6},
    "kubernetes": {"kubernetes": 1.0, "k8s": 1.0, "helm": 0.7, "eks": 0.8, "gke": 0.8},
    "terraform":  {"terraform": 1.0, "pulumi": 0.8, "cloudformation": 0.7},
    "ci_cd":      {"ci/cd": 1.0, "github actions": 0.9, "jenkins": 0.8, "gitlab ci": 0.8},
    "kafka":      {"kafka": 1.0, "pubsub": 0.7, "rabbitmq": 0.6, "sqs": 0.6},
    "spark":      {"spark": 1.0, "databricks": 0.8, "flink": 0.8},
    "airflow":    {"airflow": 1.0, "dagster": 0.8, "prefect": 0.8},
    "ml":         {"machine learning": 1.0, "pytorch": 0.9, "tensorflow": 0.9, "scikit": 0.8, "sklearn": 0.8},
    "llm":        {r"\bllm\b": 1.0, "langchain": 0.8, "rag": 0.8, "openai": 0.7, "anthropic": 0.7},
    "data_eng":   {"etl": 1.0, "data pipeline": 1.0, "dbt": 0.8, "warehouse": 0.7},
    "microservices": {"microservice": 1.0, "grpc": 0.8, "rest api": 0.7, "graphql": 0.6},
    "sysdesign":  {"system design": 1.0, "distributed system": 1.0, "scalab": 0.7},
    "security":   {"security": 1.0, "oauth": 0.6, "penetration": 0.8},
    "mobile":     {"android": 1.0, "ios": 1.0, "swift": 0.9, "react native": 0.9, "flutter": 0.9},
    "testing":    {"pytest": 1.0, "unit test": 0.9, "tdd": 0.9, "selenium": 0.7, "playwright": 0.7},
    "observability": {"prometheus": 1.0, "grafana": 0.9, "datadog": 0.9, "opentelemetry": 0.9},
    "csharp":     {"c#": 1.0, ".net": 0.95, "dotnet": 0.95},
    "cpp":        {"c\\+\\+": 1.0, "rust": 0.7},
    "frontend":   {"css": 1.0, "html": 0.8, "tailwind": 0.8, "webpack": 0.6},
}

_YRS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years|yrs|yr)\b", re.IGNORECASE)

_SENIOR_TOKENS = [
    ("staff",     ("staff", "principal", "distinguished", "architect")),
    ("lead",      ("lead", "manager", "head of")),
    ("senior",    ("senior", " sr ", " sr.", " iii")),
    ("junior",    ("junior", " jr ", " jr.", "intern", "entry", "graduate", " i ")),
]

# Rough years a title level implies — used only to shape the seniority-gap
# feature; the per-family fit learns how much (if at all) the gap matters.
_SENIORITY_EXPECTED_YOE = {"junior": 1.0, "mid": 3.0, "senior": 5.0, "lead": 7.0, "staff": 9.0}

_ROLE_CORES = [
    ("ml-eng",    ("machine learning", "ml engineer", "ai engineer", "deep learning")),
    ("data-sci",  ("data scientist", "data science")),
    ("data-eng",  ("data engineer", "analytics engineer")),
    ("platform",  ("devops", "platform", "sre", "site reliability", "infrastructure", "cloud engineer")),
    ("security",  ("security",)),
    ("mobile",    ("mobile", "android", "ios ")),
    ("frontend",  ("frontend", "front-end", "front end", "react developer", "ui engineer")),
    ("fullstack", ("full stack", "fullstack", "full-stack")),
    ("backend",   ("backend", "back-end", "back end", "api engineer")),
    ("qa",        ("qa ", "quality", "test engineer")),
    ("swe",       ("software engineer", "software developer", "swe")),
]

# ── v2: JD-level reasoning signals ───────────────────────────────────────────

_NO_SPONSOR_RE = re.compile(
    r"(?:no|not|unable\s+to|cannot|can't|will\s+not|won't|do(?:es)?\s+not)\s+"
    r"(?:currently\s+)?(?:provide|offer|support)?\s*(?:visa\s+)?sponsor"
    r"|without\s+(?:visa\s+)?sponsorship|no\s+visa"
    r"|citizens?\s+only|citizenship\s+(?:is\s+)?required"
    r"|must\s+be\s+(?:a\s+)?(?:us|u\.s\.?)\s+citizen|security\s+clearance",
    re.IGNORECASE)
_SPONSOR_OK_RE = re.compile(
    r"(?:visa|h-?1b|immigration)\s+sponsorship|will\s+sponsor"
    r"|sponsorship\s+(?:is\s+)?(?:available|offered|possible|provided)",
    re.IGNORECASE)

_DOMAINS = {
    "fintech":   ("fintech", "banking", "payments", "trading", "financial services"),
    "health":    ("healthcare", "health tech", "medical", "clinical", "biotech", "pharma"),
    "crypto":    ("crypto", "blockchain", "web3", "defi"),
    "gaming":    ("gaming", "game studio", "unity", "unreal"),
    "defense":   ("defense", "defence", "military", "aerospace"),
    "ecommerce": ("e-commerce", "ecommerce", "marketplace", "retail tech"),
}

# ── v2: disagreement buckets, mined from Claude's stored reasoning ──────────
# Priority-ordered: blockers first (they cap scores in the rubric), then the
# softer explanations. First match wins; anything unmatched is "holistic".
_DISAGREE_BUCKETS: list[tuple[str, re.Pattern]] = [
    ("visa/work-auth", re.compile(
        r"visa|sponsor|work authoriz|citizen|clearance|right to work|\bopt\b|h-?1b|immigration",
        re.IGNORECASE)),
    ("location", re.compile(
        r"locat|country|on-?site|relocat|based in|time ?zone|hybrid|must be in|geograph",
        re.IGNORECASE)),
    ("seniority", re.compile(
        r"senior|junior|years of experience|\byoe\b|experience level|overqualif|underqualif"
        r"|early.career|too (?:junior|senior)|staff-level|seniority",
        re.IGNORECASE)),
    ("domain/industry", re.compile(
        r"domain|industry|fintech|healthcare|health-?tech|biotech|pharma|crypto|blockchain"
        r"|gaming|regulated|e-?commerce",
        re.IGNORECASE)),
    ("missing-skill", re.compile(
        r"lacks?\b|missing|no (?:evidence|experience|exposure|background)|not demonstrated"
        r"|gap in|unfamiliar|absent|little (?:experience|exposure)|limited (?:experience|exposure)",
        re.IGNORECASE)),
    ("role-mismatch", re.compile(
        r"different role|not a\b.{0,30}\brole|mismatch|wrong door|unrelated|career chang|pivot",
        re.IGNORECASE)),
]
DISAGREE_THRESHOLD = 15.0   # |compiled − Claude| points before we ask "why?"


def bucket_for_reasoning(reasoning: str) -> str:
    text = (reasoning or "")[:600]
    for name, pat in _DISAGREE_BUCKETS:
        if pat.search(text):
            return name
    return "holistic/other"


def _seniority(title: str) -> str:
    t = f" {(title or '').lower()} "
    for name, toks in _SENIOR_TOKENS:
        if any(tok in t for tok in toks):
            return name
    return "mid"


def _role_core(title: str) -> str:
    t = (title or "").lower()
    for name, toks in _ROLE_CORES:
        if any(tok in t for tok in toks):
            return name
    words = re.sub(r"[^a-z ]", " ", t).split()
    return "-".join(words[:3]) or "unknown"


def family_key(title: str) -> str:
    return f"{_role_core(title)}|{_seniority(title)}"


def _alias_pattern(alias: str) -> re.Pattern:
    # Entries that look like regex fragments are used as-is; plain words get
    # word-ish boundaries so "go" can't match inside "google".
    if alias.startswith("\\b") or "\\" in alias or "/" in alias or "." in alias or "+" in alias or "#" in alias:
        return re.compile(alias, re.IGNORECASE)
    return re.compile(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", re.IGNORECASE)


_PATTERNS = {skill: [(_alias_pattern(a), credit) for a, credit in aliases.items()]
             for skill, aliases in SKILLS.items()}


def jd_requirements(jd_text: str) -> dict[str, float]:
    """Skills the JD asks for. Presence-based — the family fit learns weights."""
    text = (jd_text or "")[:6000]
    out = {}
    for skill, pats in _PATTERNS.items():
        for pat, _credit in pats:
            if pat.search(text):
                out[skill] = 1.0
                break
    return out


def grade_evidence(resume_text: str) -> dict[str, float]:
    """FM-1: graded evidence 0–4 per skill, scaled by the best alias credit.
    Crude but deterministic: mention count proxies depth, a nearby years
    figure bumps it. The point is a feature the fit can weight — Claude's
    stored scores supply the judgment."""
    text = (resume_text or "")[:16000]
    out: dict[str, float] = {}
    for skill, pats in _PATTERNS.items():
        best = 0.0
        for pat, credit in pats:
            hits = list(pat.finditer(text))
            if not hits:
                continue
            n = len(hits)
            grade = 1.0 if n == 1 else 2.0 if n <= 3 else 3.0
            # A years figure within ~60 chars of any mention reads as depth.
            for m in hits[:10]:
                window = text[max(0, m.start() - 60):m.end() + 60]
                if _YRS_RE.search(window):
                    grade = min(4.0, grade + 1.0)
                    break
            best = max(best, grade * credit)
        if best:
            out[skill] = best
    return out


def resume_yoe(resume_text: str) -> float:
    yrs = [int(m.group(1)) for m in _YRS_RE.finditer((resume_text or "")[:16000])]
    yrs = [y for y in yrs if y <= 30]
    return float(max(yrs)) if yrs else 0.0


def jd_min_yoe(jd_text: str) -> float:
    yrs = [int(m.group(1)) for m in _YRS_RE.finditer((jd_text or "")[:6000])]
    yrs = [y for y in yrs if y <= 15]
    return float(max(yrs)) if yrs else 0.0


def jd_signals(jd_text: str) -> dict:
    """Visa + domain signals stated in the JD text itself."""
    text = (jd_text or "")[:8000]
    no_sponsor = 1.0 if _NO_SPONSOR_RE.search(text) else 0.0
    sponsor_ok = 1.0 if (not no_sponsor and _SPONSOR_OK_RE.search(text)) else 0.0
    domains = {d for d, toks in _DOMAINS.items()
               if any(t in text.lower() for t in toks)}
    return {"no_sponsor": no_sponsor, "sponsor_ok": sponsor_ok, "domains": domains}


def resume_domains(resume_text: str) -> set:
    low = (resume_text or "")[:16000].lower()
    return {d for d, toks in _DOMAINS.items() if any(t in low for t in toks)}


# ── The per-family fit (v1 features vs v2 features, same machinery) ─────────

TOP_SKILLS_PER_FAMILY = 12
RIDGE_LAMBDA = 1.0

_V2_EXTRA = ["visa_refused_needed", "sponsor_ok_needed", "country_mismatch",
             "remote", "seniority_gap", "overqualified", "domain_match",
             "jd_domain_unmatched"]


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    try:
        from scipy.stats import spearmanr
        rho = spearmanr(a, b).statistic
        return float(rho) if np.isfinite(rho) else 0.0
    except Exception:
        ra = np.argsort(np.argsort(a)).astype(float)
        rb = np.argsort(np.argsort(b)).astype(float)
        c = np.corrcoef(ra, rb)[0, 1]
        return float(c) if np.isfinite(c) else 0.0


def _loo_fit(X: np.ndarray, y: np.ndarray, threshold: float) -> tuple[dict, np.ndarray]:
    """Ridge + exact leave-one-out predictions; returns (metrics, loo_resid)."""
    Xm, ym = X.mean(axis=0), y.mean()
    Xc, yc = X - Xm, y - ym
    A = Xc.T @ Xc + RIDGE_LAMBDA * np.eye(Xc.shape[1])
    w = np.linalg.solve(A, Xc.T @ yc)
    H = Xc @ np.linalg.solve(A, Xc.T)
    pred_in = Xc @ w + ym
    h = np.clip(np.diag(H), 0.0, 0.999)
    resid_loo = (y - pred_in) / (1.0 - h)
    pred_loo = y - resid_loo
    ss_tot = ((y - ym) ** 2).sum() or 1e-9
    metrics = {
        "r2_loo": round(1.0 - float((resid_loo ** 2).sum() / ss_tot), 3),
        "rho": round(_spearman(y, pred_loo), 3),
        "agree": round(float(((pred_loo >= threshold) == (y >= threshold)).mean()), 3),
        "weights": w,
    }
    return metrics, resid_loo


def _feature_rows(rows: list[dict], top_skills: list[str], v2: bool) -> np.ndarray:
    out = []
    for r in rows:
        x = [r["req"].get(s, 0.0) * r["evidence"].get(s, 0.0) for s in top_skills]
        x += [min(r["user_yoe"], 12.0), max(0.0, r["jd_yoe"] - r["user_yoe"])]
        if v2:
            x += [r.get(k, 0.0) for k in _V2_EXTRA]
        out.append(x)
    return np.array(out)


def fit_family(rows: list[dict], shortlist_threshold: float) -> dict:
    """Fit v1 (skills+years) and v2 (adds reasoning features) side by side,
    then bucket the v2 program's remaining big disagreements using Claude's
    stored reasoning text."""
    req_freq: Counter = Counter()
    for r in rows:
        req_freq.update(r["req"])
    top_skills = [s for s, _n in req_freq.most_common(TOP_SKILLS_PER_FAMILY)]
    y = np.array([r["score"] for r in rows], dtype=float)

    m1, _ = _loo_fit(_feature_rows(rows, top_skills, v2=False), y, shortlist_threshold)
    m2, resid2 = _loo_fit(_feature_rows(rows, top_skills, v2=True), y, shortlist_threshold)

    disagreements: list[tuple[str, float]] = []
    for r, resid in zip(rows, resid2):
        if abs(resid) >= DISAGREE_THRESHOLD:
            disagreements.append((bucket_for_reasoning(r.get("reasoning", "")), float(abs(resid))))

    if m2["rho"] >= 0.85 and m2["agree"] >= 0.90:
        verdict = "COMPILABLE"
    elif m2["rho"] >= 0.70:
        verdict = "BORDERLINE"
    else:
        verdict = "KEEP-LLM"
    return {
        "n": len(rows),
        "users": len({r["user"] for r in rows}),
        "top_skills": top_skills,
        "rho_v1": m1["rho"], "r2_v1": m1["r2_loo"], "agree_v1": m1["agree"],
        "rho": m2["rho"], "r2_loo": m2["r2_loo"], "decision_agreement": m2["agree"],
        "verdict": verdict,
        "disagreements": disagreements,
        "weights": {k: round(float(v), 2)
                    for k, v in zip(top_skills + ["yoe", "yoe_gap"] + _V2_EXTRA, m2["weights"])},
    }


# ── Data collection (real DB) ────────────────────────────────────────────────

def collect_rows(max_rows: int = 0) -> tuple[dict[str, list[dict]], Counter]:
    """Genuine Claude finals grouped by family, with v1+v2 features and the
    stored reasoning kept for disagreement mining. Reuses the export script's
    filter + keyset pagination."""
    from app.common.geo import detect_country
    from app.matching.pipeline import _load_resume
    from scripts.export_training_data import _scored_chunks, is_llm_final

    stats: Counter = Counter()
    user_cache: dict = {}   # uid -> dict | None

    def _user_for(uid):
        if uid not in user_cache:
            try:
                uid_arg = None if (not uid or uid == "local") else uid
                text = (_load_resume(user_id=uid_arg) or "").strip()
                if not text:
                    user_cache[uid] = None
                    return None
                needs_sponsor, profile_yoe, user_country = 0.0, 0.0, ""
                try:
                    from sqlmodel import select

                    from app.db.init_db import get_session
                    from app.db.models import UserProfile
                    with get_session() as session:
                        prof = session.exec(select(UserProfile)
                                            .where(UserProfile.user_id == uid_arg)).first()
                    if prof:
                        needs_sponsor = 1.0 if prof.requires_sponsorship else 0.0
                        profile_yoe = float(prof.years_experience or 0)
                        user_country = detect_country(prof.location or "")
                except Exception:
                    log.debug("profile lookup failed for %s — visa/country features "
                              "default to 0 for this user", uid, exc_info=True)
                user_cache[uid] = {
                    "evidence": grade_evidence(text),
                    "yoe": max(resume_yoe(text), profile_yoe),
                    "needs_sponsor": needs_sponsor,
                    "country": user_country,
                    "domains": resume_domains(text),
                }
            except Exception:
                user_cache[uid] = None
        return user_cache[uid]

    families: dict[str, list[dict]] = defaultdict(list)
    for job in _scored_chunks():
        stats["scored_rows"] += 1
        if not is_llm_final(job.rerank_reasoning, job.rerank_breakdown):
            stats["skipped_cheap_gate"] += 1
            continue
        u = _user_for(job.user_id)
        if u is None:
            stats["skipped_no_resume"] += 1
            continue
        jd = (job.description or "")
        sig = jd_signals(jd)
        seniority = _seniority(job.title or "")
        expected = _SENIORITY_EXPECTED_YOE.get(seniority, 3.0)
        jd_country = detect_country(job.location or "")
        remote = 1.0 if job.remote else 0.0
        families[family_key(job.title or "")].append({
            "user": str(job.user_id or "local"),
            "score": float(job.rerank_score),
            "req": jd_requirements(jd),
            "evidence": u["evidence"],
            "user_yoe": u["yoe"],
            "jd_yoe": jd_min_yoe(jd),
            # v2 reasoning features
            "visa_refused_needed": sig["no_sponsor"] * u["needs_sponsor"],
            "sponsor_ok_needed": sig["sponsor_ok"] * u["needs_sponsor"],
            "country_mismatch": 1.0 if (jd_country and u["country"]
                                        and jd_country != u["country"] and not remote) else 0.0,
            "remote": remote,
            "seniority_gap": max(0.0, expected - u["yoe"]),
            "overqualified": max(0.0, u["yoe"] - expected - 4.0),
            "domain_match": 1.0 if (sig["domains"] & u["domains"]) else 0.0,
            "jd_domain_unmatched": 1.0 if (sig["domains"] and not (sig["domains"] & u["domains"])) else 0.0,
            # Claude's own explanation, kept for disagreement mining.
            "reasoning": (job.rerank_reasoning or "")[:400],
        })
        stats["usable_finals"] += 1
        if max_rows and stats["usable_finals"] >= max_rows:
            break
    return families, stats


# ── Report ───────────────────────────────────────────────────────────────────

def run_report(families: dict[str, list[dict]], stats: Counter,
               min_samples: int, shortlist_threshold: float,
               out_path: str | None) -> dict:
    results = {}
    for fam, rows in families.items():
        if len(rows) < min_samples:
            stats["skipped_small_family"] += len(rows)
            continue
        results[fam] = fit_family(rows, shortlist_threshold)

    total = sum(r["n"] for r in results.values())
    compilable = sum(r["n"] for r in results.values() if r["verdict"] == "COMPILABLE")
    borderline = sum(r["n"] for r in results.values() if r["verdict"] == "BORDERLINE")

    # Aggregate WHY the v2 program still disagrees, weighted by miss size.
    bucket_weight: Counter = Counter()
    bucket_count: Counter = Counter()
    for r in results.values():
        for bucket, wgt in r["disagreements"]:
            bucket_weight[bucket] += wgt
            bucket_count[bucket] += 1
    wtotal = sum(bucket_weight.values()) or 1.0
    buckets = {b: {"count": bucket_count[b],
                   "weighted_pct": round(100.0 * bucket_weight[b] / wtotal, 1)}
               for b in bucket_weight}

    log.info("")
    log.info("── Compiler replay v2 — per-family agreement with stored Claude finals ──")
    log.info("%-28s %5s %5s %7s %7s %7s %7s  %s",
             "family", "n", "users", "rho v1", "rho v2", "R2loo", "agree", "verdict")
    for fam, r in sorted(results.items(), key=lambda kv: -kv[1]["n"]):
        log.info("%-28s %5d %5d %7.2f %7.2f %7.2f %6.0f%%  %s",
                 fam[:28], r["n"], r["users"], r["rho_v1"], r["rho"], r["r2_loo"],
                 r["decision_agreement"] * 100, r["verdict"])
    if buckets:
        log.info("")
        log.info("── Why the compiled score still disagrees with Claude (>=%.0f pts) ──",
                 DISAGREE_THRESHOLD)
        for b, d in sorted(buckets.items(), key=lambda kv: -kv[1]["weighted_pct"]):
            log.info("  %-18s %5d pairs  %5.1f%% of weighted disagreement",
                     b, d["count"], d["weighted_pct"])
    log.info("")
    for k, v in sorted(stats.items()):
        log.info("  %s: %s", k, v)
    if total:
        log.info("")
        log.info("  Volume in fitted families: %d finals", total)
        log.info("  COMPILABLE: %d (%.0f%%)   BORDERLINE: %d (%.0f%%)   KEEP-LLM: %d (%.0f%%)",
                 compilable, 100 * compilable / total,
                 borderline, 100 * borderline / total,
                 total - compilable - borderline,
                 100 * (total - compilable - borderline) / total)

    summary = {"families": results, "stats": dict(stats),
               "volume": {"fitted": total, "compilable": compilable, "borderline": borderline},
               "disagreements": {"threshold": DISAGREE_THRESHOLD, "buckets": buckets}}
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        slim = {**summary,
                "families": {f: {k: v for k, v in r.items() if k != "disagreements"}
                             for f, r in results.items()}}
        p.write_text(json.dumps(slim, indent=2))
        log.info("  Full report → %s", out_path)
    return summary


# ── Self-test: synthetic end-to-end validation, no DB, no LLM ────────────────

def selftest() -> dict:
    """Ground-truth checks for the v2 machinery:
    1. a family whose hidden scorer applies a VISA PENALTY must fit clearly
       better with v2 features than v1 (the lift is measurable),
    2. a vague family (mostly noise) must still be rejected (KEEP-LLM),
    3. its disagreements must land in the holistic bucket via the stored
       reasoning text."""
    rng = np.random.default_rng(11)

    users = {
        "u_strong": {
            "text": ("Built production python services for 6 years. Python scheduler, "
                     "fastapi and flask APIs, postgres and redis. Deployed on aws with "
                     "docker and kubernetes (k8s, helm) via github actions. 6+ years "
                     "python, kafka pipelines, system design for distributed systems. "
                     "pytest everywhere. python python."),
            "needs_sponsor": 0.0},
        "u_junior": {
            "text": ("Bootcamp graduate. Skills: Python, HTML, CSS, React. One personal "
                     "project using flask. 1 year experience."),
            "needs_sponsor": 1.0},
        "u_data": {
            "text": ("Data engineer, 4+ years airflow and spark on gcp bigquery. dbt, "
                     "etl data pipeline design, python for 5 years, sql postgres. "
                     "airflow airflow spark docker terraform."),
            "needs_sponsor": 1.0},
    }
    feats = {}
    for name, u in users.items():
        feats[name] = {"evidence": grade_evidence(u["text"]), "yoe": resume_yoe(u["text"]),
                       "needs_sponsor": u["needs_sponsor"], "domains": resume_domains(u["text"])}

    fam_skills = {
        "backend|senior":  ["python", "fastapi", "sql", "aws", "docker", "kubernetes", "kafka"],
        "data-eng|mid":    ["python", "airflow", "spark", "gcp", "sql", "data_eng"],
        "swe|mid":         ["python", "javascript", "sql", "aws"],   # the vague one
    }
    hidden_w = {s: rng.uniform(4, 9) for s in SKILLS}

    families: dict[str, list[dict]] = defaultdict(list)
    for fam, pool in fam_skills.items():
        vague = fam == "swe|mid"
        visa_family = fam == "backend|senior"
        for i in range(48):
            ask = [s for s in pool if rng.random() < 0.8] or pool[:2]
            jd_yoe = float(rng.choice([0, 2, 3, 5]))
            jd_no_sponsor = 1.0 if (visa_family and i % 2 == 0) else 0.0
            jd_text = ("We are hiring. Requirements: " + ", ".join(ask)
                       + (f". {int(jd_yoe)}+ years required." if jd_yoe else "")
                       + (" We are unable to provide visa sponsorship." if jd_no_sponsor else ""))
            req = jd_requirements(jd_text)
            sig = jd_signals(jd_text)
            for uname, u in feats.items():
                x = sum(hidden_w[s] * req.get(s, 0) * u["evidence"].get(s, 0.0) for s in SKILLS)
                x += 2.5 * min(u["yoe"], 12) - 6.0 * max(0.0, jd_yoe - u["yoe"])
                visa_hit = sig["no_sponsor"] * u["needs_sponsor"]
                x -= 80.0 * visa_hit
                if vague:
                    score = float(np.clip(rng.normal(45, 22), 0, 100))
                    reasoning = "Holistic judgement of the overall profile narrative and trajectory."
                else:
                    score = float(np.clip(x * 0.55 + 8 + rng.normal(0, 2.5), 0, 100))
                    reasoning = ("Strong skills but the posting offers no visa sponsorship — blocker."
                                 if visa_hit else "Good skill and seniority match for the role.")
                families[fam].append({
                    "user": uname, "score": score, "req": req, "evidence": u["evidence"],
                    "user_yoe": u["yoe"], "jd_yoe": jd_yoe,
                    "visa_refused_needed": visa_hit,
                    "sponsor_ok_needed": sig["sponsor_ok"] * u["needs_sponsor"],
                    "country_mismatch": 0.0, "remote": 0.0,
                    "seniority_gap": max(0.0, 5.0 - u["yoe"]),
                    "overqualified": max(0.0, u["yoe"] - 9.0),
                    "domain_match": 0.0, "jd_domain_unmatched": 0.0,
                    "reasoning": reasoning,
                })

    stats: Counter = Counter(usable_finals=sum(len(v) for v in families.values()))
    summary = run_report(families, stats, min_samples=15,
                         shortlist_threshold=60.0, out_path=None)
    fams = summary["families"]
    lift = fams["backend|senior"]["rho"] - fams["backend|senior"]["rho_v1"]
    ok_lift = lift >= 0.05 and fams["backend|senior"]["rho"] >= 0.85
    ok_vague = fams["swe|mid"]["verdict"] == "KEEP-LLM"
    buckets = summary["disagreements"]["buckets"]
    top_bucket = max(buckets, key=lambda b: buckets[b]["weighted_pct"]) if buckets else ""
    ok_bucket = top_bucket == "holistic/other"
    log.info("")
    log.info("SELFTEST v2 visa-feature lift (rho %+.2f, v2=%.2f): %s",
             lift, fams["backend|senior"]["rho"], "PASS" if ok_lift else "FAIL")
    log.info("SELFTEST vague family rejected:            %s", "PASS" if ok_vague else "FAIL")
    log.info("SELFTEST holistic bucket dominates (%s):   %s",
             top_bucket or "none", "PASS" if ok_bucket else "FAIL")
    summary["selftest_pass"] = bool(ok_lift and ok_vague and ok_bucket)
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-samples", type=int, default=15,
                    help="min finals per family to attempt a fit")
    ap.add_argument("--max-rows", type=int, default=0, help="0 = all usable finals")
    ap.add_argument("--out", default="data/compiler_replay_report.json")
    ap.add_argument("--selftest", action="store_true",
                    help="run on synthetic data instead of the DB")
    args = ap.parse_args()

    if args.selftest:
        summary = selftest()
        raise SystemExit(0 if summary["selftest_pass"] else 1)

    from app.config import settings
    from app.db.init_db import init_db
    init_db()   # idempotent — a fresh local DB gets empty tables, not a traceback
    families, stats = collect_rows(max_rows=args.max_rows)
    if not stats.get("usable_finals"):
        log.info("No genuine LLM finals found in this DB — run this against the "
                 "production database (same environment as export_training_data).")
    run_report(families, stats, args.min_samples,
               float(settings.shortlist_score_threshold), args.out)


if __name__ == "__main__":
    main()
