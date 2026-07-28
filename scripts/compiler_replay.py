"""Compiler-layer replay experiment — which job families are compilable?

The "LLM as compiler" plan (JD → tiny scoring program, runtime is free
arithmetic) is only worth building if a linear program over cheap features can
actually reproduce Claude's judgment for a meaningful share of our job volume.
This script answers that question for $0, using the Claude finals ALREADY
stored in the DB as ground truth — no LLM calls, no panels to hand-write yet.

    python -m scripts.compiler_replay                 # against the real DB
    python -m scripts.compiler_replay --selftest      # synthetic end-to-end check

Per job family (role core × seniority) it:
  1. collects genuine LLM finals (same filter as export_training_data — rows
     stamped by the cheap gates would teach the fit the gates, not the rubric),
  2. builds the compiler plan's feature shape: requirement-in-JD × graded
     evidence-in-résumé per skill (acceptance sets baked in), plus a years gate,
  3. ridge-fits ~14 weights and scores them with LEAVE-ONE-OUT predictions —
     the demo's in-sample R² flatters a 12-weight/20-point fit; held-out
     numbers are the honest version,
  4. reports Spearman ρ, held-out R², and shortlist-decision agreement vs the
     stored Claude score, with a verdict per family.

Read the verdicts as: COMPILABLE families could run on a compiled program with
Claude kept for the borderline band; KEEP-LLM families stay on the LLM path
(the vague-JD case the plan's R² gate exists for). Build the compiler layer
only if COMPILABLE families cover most of the scored volume.
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


_PATTERNS = {skill: [( _alias_pattern(a), credit) for a, credit in aliases.items()]
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


# ── The per-family fit ───────────────────────────────────────────────────────

TOP_SKILLS_PER_FAMILY = 12
RIDGE_LAMBDA = 1.0


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


def fit_family(rows: list[dict], shortlist_threshold: float) -> dict:
    """Ridge-fit a tiny linear program for one family; score it honestly with
    leave-one-out predictions (closed form via the ridge hat matrix — no
    n retrains needed)."""
    req_freq: Counter = Counter()
    for r in rows:
        req_freq.update(r["req"])
    top_skills = [s for s, _n in req_freq.most_common(TOP_SKILLS_PER_FAMILY)]

    X = np.array([
        [r["req"].get(s, 0.0) * r["evidence"].get(s, 0.0) for s in top_skills]
        + [min(r["user_yoe"], 12.0), max(0.0, r["jd_yoe"] - r["user_yoe"])]
        for r in rows
    ])
    y = np.array([r["score"] for r in rows], dtype=float)

    # Center so the ridge penalty never fights the intercept.
    Xm, ym = X.mean(axis=0), y.mean()
    Xc, yc = X - Xm, y - ym
    A = Xc.T @ Xc + RIDGE_LAMBDA * np.eye(Xc.shape[1])
    w = np.linalg.solve(A, Xc.T @ yc)
    H = Xc @ np.linalg.solve(A, Xc.T)          # hat matrix
    pred_in = Xc @ w + ym
    h = np.clip(np.diag(H), 0.0, 0.999)
    resid_loo = (y - pred_in) / (1.0 - h)      # exact LOO residuals for ridge
    pred_loo = y - resid_loo

    ss_tot = ((y - ym) ** 2).sum() or 1e-9
    r2_loo = 1.0 - float((resid_loo ** 2).sum() / ss_tot)
    rho = _spearman(y, pred_loo)
    agree = float(((pred_loo >= shortlist_threshold) == (y >= shortlist_threshold)).mean())

    if rho >= 0.85 and agree >= 0.90:
        verdict = "COMPILABLE"
    elif rho >= 0.70:
        verdict = "BORDERLINE"
    else:
        verdict = "KEEP-LLM"
    return {
        "n": len(rows),
        "users": len({r["user"] for r in rows}),
        "top_skills": top_skills,
        "rho": round(rho, 3),
        "r2_loo": round(r2_loo, 3),
        "decision_agreement": round(agree, 3),
        "verdict": verdict,
        "weights": {s: round(float(v), 2) for s, v in zip(top_skills + ["yoe", "yoe_gap"], w)},
    }


# ── Data collection (real DB) ────────────────────────────────────────────────

def collect_rows(max_rows: int = 0) -> tuple[dict[str, list[dict]], Counter]:
    """Genuine Claude finals grouped by family, with features precomputed.
    Reuses the export script's filter + keyset pagination — cheap-gate stamped
    rows would teach the fit the gates, not the rubric."""
    from app.matching.pipeline import _load_resume
    from scripts.export_training_data import _scored_chunks, is_llm_final

    stats: Counter = Counter()
    resume_feats: dict = {}   # uid -> (evidence dict, yoe) | None

    def _feats_for(uid):
        if uid not in resume_feats:
            try:
                uid_arg = None if (not uid or uid == "local") else uid
                text = (_load_resume(user_id=uid_arg) or "").strip()
                resume_feats[uid] = (grade_evidence(text), resume_yoe(text)) if text else None
            except Exception:
                resume_feats[uid] = None
        return resume_feats[uid]

    families: dict[str, list[dict]] = defaultdict(list)
    for job in _scored_chunks():
        stats["scored_rows"] += 1
        if not is_llm_final(job.rerank_reasoning, job.rerank_breakdown):
            stats["skipped_cheap_gate"] += 1
            continue
        feats = _feats_for(job.user_id)
        if feats is None:
            stats["skipped_no_resume"] += 1
            continue
        evidence, user_yoe = feats
        jd = (job.description or "")
        families[family_key(job.title or "")].append({
            "user": str(job.user_id or "local"),
            "score": float(job.rerank_score),
            "req": jd_requirements(jd),
            "evidence": evidence,
            "user_yoe": user_yoe,
            "jd_yoe": jd_min_yoe(jd),
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

    log.info("")
    log.info("── Compiler replay — per-family agreement with stored Claude finals ──")
    log.info("%-28s %5s %5s %6s %7s %7s  %s",
             "family", "n", "users", "rho", "R2loo", "agree", "verdict")
    for fam, r in sorted(results.items(), key=lambda kv: -kv[1]["n"]):
        log.info("%-28s %5d %5d %6.2f %7.2f %6.0f%%  %s",
                 fam[:28], r["n"], r["users"], r["rho"], r["r2_loo"],
                 r["decision_agreement"] * 100, r["verdict"])
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
        log.info("")
        log.info("  Reading: build the compiler layer only if COMPILABLE covers most")
        log.info("  volume. BORDERLINE families would ship with a wide Sonnet band.")

    summary = {"families": results, "stats": dict(stats),
               "volume": {"fitted": total, "compilable": compilable, "borderline": borderline}}
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, indent=2))
        log.info("  Full report → %s", out_path)
    return summary


# ── Self-test: synthetic end-to-end validation, no DB, no LLM ────────────────

def selftest() -> dict:
    """Run the whole path on synthetic data where ground truth is known:
    three consistent families a linear program CAN express, and one 'vague
    boilerplate' family whose scores are mostly noise. The machinery must
    ship the first three and reject the fourth."""
    rng = np.random.default_rng(11)

    resumes = {
        "u_strong": ("Built production python services for 6 years. Python scheduler, "
                     "fastapi and flask APIs, postgres and redis. Deployed on aws with "
                     "docker and kubernetes (k8s, helm) via github actions. 6+ years "
                     "python, kafka pipelines, system design for distributed systems. "
                     "pytest everywhere. python python."),
        "u_junior": ("Bootcamp graduate. Skills: Python, HTML, CSS, React. One personal "
                     "project using flask. 1 year experience."),
        "u_data":   ("Data engineer, 4+ years airflow and spark on gcp bigquery. dbt, "
                     "etl data pipeline design, python for 5 years, sql postgres. "
                     "airflow airflow spark docker terraform."),
    }
    feats = {u: (grade_evidence(t), resume_yoe(t)) for u, t in resumes.items()}

    fam_skills = {
        "backend|senior":  ["python", "fastapi", "sql", "aws", "docker", "kubernetes", "kafka"],
        "data-eng|mid":    ["python", "airflow", "spark", "gcp", "sql", "data_eng"],
        "platform|senior": ["kubernetes", "terraform", "aws", "docker", "ci_cd", "observability"],
        "swe|mid":         ["python", "javascript", "sql", "aws"],   # the vague one
    }
    hidden_w = {s: rng.uniform(4, 9) for s in SKILLS}

    families: dict[str, list[dict]] = defaultdict(list)
    for fam, pool in fam_skills.items():
        vague = fam == "swe|mid"
        for _i in range(48):
            ask = [s for s in pool if rng.random() < 0.8] or pool[:2]
            jd_yoe = float(rng.choice([0, 2, 3, 5]))
            jd_text = ("We are hiring. Requirements: " + ", ".join(ask)
                       + (f". {int(jd_yoe)}+ years required." if jd_yoe else ""))
            req = jd_requirements(jd_text)
            for user, (evidence, yoe) in feats.items():
                x = sum(hidden_w[s] * req.get(s, 0) * evidence.get(s, 0.0) for s in SKILLS)
                x += 2.5 * min(yoe, 12) - 6.0 * max(0.0, jd_yoe - yoe)
                noise = rng.normal(0, 30.0 if vague else 2.5)
                score = float(np.clip(x * 0.55 + (rng.normal(35, 18) if vague else 8) + noise, 0, 100))
                families[fam].append({
                    "user": user, "score": score, "req": req,
                    "evidence": evidence, "user_yoe": yoe, "jd_yoe": jd_yoe,
                })

    stats: Counter = Counter(usable_finals=sum(len(v) for v in families.values()))
    summary = run_report(families, stats, min_samples=15,
                         shortlist_threshold=60.0, out_path=None)
    verdicts = {f: r["verdict"] for f, r in summary["families"].items()}
    ok_consistent = all(verdicts[f] in ("COMPILABLE", "BORDERLINE")
                        for f in ("backend|senior", "data-eng|mid", "platform|senior"))
    ok_vague = verdicts["swe|mid"] == "KEEP-LLM"
    log.info("")
    log.info("SELFTEST consistent-families fitted: %s", "PASS" if ok_consistent else "FAIL")
    log.info("SELFTEST vague family rejected:      %s", "PASS" if ok_vague else "FAIL")
    summary["selftest_pass"] = bool(ok_consistent and ok_vague)
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
