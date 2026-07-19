"""Resume X-Ray — how screeners (ATS parsers, keyword filters, AI graders,
and the 6-second human scan) actually see the user's master resume, with
detailed gap explanations and concrete fix-it suggestions.

Design rules:
  * Deterministic-first — every panel works with ZERO LLM spend (the app must
    run on free local analysis; an LLM verdict is an optional enrichment).
  * Honest — verdicts are labeled "simulated"; we never claim to know a
    specific company's real screening rules, and we never invent statistics.
  * Additive — read-only over the resume + profile + skill-gap evidence;
    approving a change goes through the existing verified-achievements flow
    (the human is the ground truth), never a silent rewrite.

Panels returned by compute_resume_xray():
  six_second        what a screener reads in the first pass, scored /6
  ats_parse         simulated ATS field extraction + parse issues
  screeners         per-screener verdicts (parser / keyword filter / AI grader / human scan)
  employment_gaps   date-math gaps between roles, with framing advice
  reject_reasons    ranked, detailed "why you'd be rejected" list
  projects          detailed build-this project cards for top missing skills
  sync              resume vs LinkedIn vs GitHub keyword presence
  semantic          embedding-level JD-requirement coverage (None if model unavailable)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Section / contact detection ───────────────────────────────────────────────
_SECTION_HEADERS = {
    "experience": re.compile(r"^\s*#*\s*(work\s+)?(experience|employment|professional\s+experience)\b", re.I | re.M),
    "education": re.compile(r"^\s*#*\s*education\b", re.I | re.M),
    "skills": re.compile(r"^\s*#*\s*(technical\s+)?skills\b", re.I | re.M),
    "projects": re.compile(r"^\s*#*\s*projects?\b", re.I | re.M),
    "summary": re.compile(r"^\s*#*\s*(summary|profile|objective|about)\b", re.I | re.M),
}
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{8,}\d)")
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w-]+", re.I)
_GITHUB_RE = re.compile(r"github\.com/[\w-]+", re.I)
_BULLET_RE = re.compile(r"^\s*[-*•·▪]\s+(.{5,})", re.M)
_FIRST_PERSON_RE = re.compile(r"\b(I|my|me)\b")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_RANGE_RE = re.compile(
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}|\d{4})"
    r"\s*[-–—to]+\s*"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}|\d{4}|present|current|now)",
    re.I,
)


def _parse_month(tok: str) -> Optional[Tuple[int, int]]:
    """'Mar 2023' / '2023' / 'present' → (year, month) or None."""
    tok = tok.strip().lower().rstrip(".")
    if tok in ("present", "current", "now"):
        now = datetime.utcnow()
        return now.year, now.month
    m = re.match(r"([a-z]{3,})\.?\s*(\d{4})", tok)
    if m:
        mon = _MONTHS.get(m.group(1)[:3])
        return (int(m.group(2)), mon) if mon else None
    if re.fullmatch(r"\d{4}", tok):
        return int(tok), 6  # bare year → assume mid-year (uncertain)
    return None


def _months_between(a: Tuple[int, int], b: Tuple[int, int]) -> int:
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


# ── Panel 1: simulated ATS parse ─────────────────────────────────────────────
def ats_parse(resume_text: str) -> dict:
    """What a form-filling ATS parser can and cannot extract — with issues."""
    text = resume_text or ""
    words = len(text.split())
    bullets = _BULLET_RE.findall(text)
    sections = {name: bool(rx.search(text)) for name, rx in _SECTION_HEADERS.items()}
    ranges = _RANGE_RE.findall(text)
    issues: List[dict] = []

    def issue(sev, what, why):
        issues.append({"severity": sev, "what": what, "why": why})

    if not _EMAIL_RE.search(text):
        issue("fail", "No email address found",
              "An ATS that can't extract contact info files the application as incomplete — some silently drop it.")
    if not _PHONE_RE.search(text):
        issue("warn", "No phone number found",
              "Many ATS forms auto-fill phone from the resume; a blank field is a common silent-failure point.")
    if not sections["experience"]:
        issue("fail", "No 'Experience' section header detected",
              "Parsers map content by section headers. Without one, your work history may land in the wrong field or be dropped.")
    if not sections["education"]:
        issue("warn", "No 'Education' section header detected",
              "Degree knockout questions are auto-answered from this section; unparsed education can fail a knockout you actually pass.")
    if not sections["skills"]:
        issue("warn", "No 'Skills' section header detected",
              "Keyword filters weight the skills section heavily; skills buried in prose count less in many parsers.")
    if not ranges:
        issue("fail", "No employment date ranges detected",
              "Date math drives 'years of experience' auto-screens. Unparseable dates often read as 0 years.")
    if words > 950:
        issue("warn", f"Long resume (~{words} words, likely 2+ pages)",
              "Nothing breaks, but the 6-second scan only covers the top third of page one — length dilutes it.")
    if len(bullets) < 6:
        issue("warn", f"Only {len(bullets)} bullet points detected",
              "Dense paragraphs parse poorly and scan worse — screeners skim bullets, not prose.")
    if len(_FIRST_PERSON_RE.findall(text)) > 5:
        issue("warn", "Frequent first-person pronouns (I/my/me)",
              "A style flag many recruiters use as a proxy for junior writing; costs credibility in the human scan.")

    return {
        "fields": {
            "email": bool(_EMAIL_RE.search(text)),
            "phone": bool(_PHONE_RE.search(text)),
            "linkedin": bool(_LINKEDIN_RE.search(text)),
            "github": bool(_GITHUB_RE.search(text)),
            "sections_found": [k for k, v in sections.items() if v],
            "date_ranges_found": len(ranges),
            "bullets": len(bullets),
            "words": words,
        },
        "issues": issues,
    }


# ── Panel 2: employment gaps (the literal kind) ──────────────────────────────
def employment_gaps(resume_text: str, experience_json: Optional[list] = None) -> List[dict]:
    """Detect gaps >3 months between consecutive roles, with framing advice.
    Prefers structured experience entries; falls back to date ranges in text."""
    periods: List[Tuple[Tuple[int, int], Tuple[int, int], str]] = []
    if experience_json:
        for e in experience_json:
            try:
                start = _parse_month(str(e.get("start") or ""))
                end = _parse_month("present" if e.get("current") else str(e.get("end") or ""))
                if start and end:
                    periods.append((start, end, f"{e.get('title', '?')} @ {e.get('company', '?')}"))
            except Exception:
                continue
    if not periods:
        for m in _RANGE_RE.finditer(resume_text or ""):
            start, end = _parse_month(m.group(1)), _parse_month(m.group(2))
            if start and end:
                periods.append((start, end, "role"))
    if len(periods) < 1:
        return []
    periods.sort(key=lambda p: p[0])
    gaps: List[dict] = []
    for (s1, e1, r1), (s2, e2, r2) in zip(periods, periods[1:]):
        months = _months_between(e1, s2)
        if months > 3:
            gaps.append({
                "months": months,
                "after": r1, "before": r2,
                "window": f"{e1[0]}-{e1[1]:02d} → {s2[0]}-{s2[1]:02d}",
                "detail": (
                    f"A {months}-month unexplained gap between '{r1}' and '{r2}'. "
                    "Screeners don't reject gaps — they reject UNEXPLAINED gaps. One line "
                    "('Career break: relocation / family / full-time upskilling in X — built Y') "
                    "converts the question mark into a data point."),
            })
    # currently-unemployed run-out from the latest role
    latest_end = max(p[1] for p in periods)
    now = datetime.utcnow()
    months_out = _months_between(latest_end, (now.year, now.month))
    if months_out > 3:
        gaps.append({
            "months": months_out, "after": "your most recent role", "before": "today",
            "window": f"{latest_end[0]}-{latest_end[1]:02d} → now",
            "detail": (
                f"Your most recent role appears to have ended ~{months_out} months ago. "
                "Add a current line (freelance, open-source, certification, active project) so "
                "the top of the resume doesn't read as 'inactive'."),
        })
    return gaps


# ── Panel 3: the 6-second scan ───────────────────────────────────────────────
def six_second_scan(resume_text: str, profile) -> dict:
    """What a screener actually absorbs in the first pass, scored out of 6.
    The six checks mirror what recruiters say they look for first: who are you,
    what are you now, how senior, can I see proof, does it match my role, can I
    reach you."""
    text = resume_text or ""
    top = "\n".join(text.splitlines()[:25])  # the visually-first block
    target_roles = [(r or "").strip().lower() for r in
                    ((getattr(profile, "target_roles", "") or "").split(","))if r.strip()]
    from app.tailoring.doctor import _METRIC_RE
    checks = [
        ("identity", bool(_EMAIL_RE.search(top) or (getattr(profile, "first_name", "") or "") != ""),
         "Name + contact visible at the very top"),
        ("title", bool(getattr(profile, "current_title", "") or re.search(r"engineer|developer|scientist|manager|analyst|designer", top, re.I)),
         "A clear current title in the first lines"),
        ("seniority", bool(re.search(r"\d+\+?\s*(years|yrs)", top, re.I) or (getattr(profile, "years_experience", 0) or 0) > 0),
         "Years of experience stated up top"),
        ("metric", bool(_METRIC_RE.search(top)),
         "At least one number/result in the first screen of text"),
        ("role_match", any(t in text.lower() for t in target_roles) if target_roles else False,
         "Your target role's words literally appear in the resume"),
        ("skills_visible", bool(_SECTION_HEADERS["skills"].search(top)) or "skills" in top.lower(),
         "Skills reachable without scrolling"),
    ]
    passed = [{"check": c, "label": lbl} for c, ok, lbl in checks if ok]
    failed = [{"check": c, "label": lbl} for c, ok, lbl in checks if not ok]
    return {"score": len(passed), "out_of": len(checks), "passed": passed, "failed": failed,
            "verdict": ("survives the scan" if len(passed) >= 5 else
                        "borderline — fix the misses" if len(passed) >= 3 else
                        "likely skimmed past")}


# ── Panel 4: screener panel (composed) ───────────────────────────────────────
_GRADES = ["A", "B", "C", "D"]


def screener_panel(parse: dict, six: dict, coverage_pct: Optional[int],
                   metrics_density: float, gaps: List[dict]) -> List[dict]:
    """Four simulated screeners, each with a verdict + the reasons. Honest
    framing: these are simulations of common screening mechanics, not any
    specific company's system."""
    out: List[dict] = []
    fails = sum(1 for i in parse["issues"] if i["severity"] == "fail")
    warns = sum(1 for i in parse["issues"] if i["severity"] == "warn")
    out.append({
        "name": "ATS parser", "kind": "simulated",
        "verdict": "fail" if fails else ("warn" if warns else "pass"),
        "detail": (f"{fails} blocking + {warns} minor parse issues"
                   if (fails or warns) else "Parses cleanly into structured fields"),
    })
    if coverage_pct is not None:
        out.append({
            "name": "Keyword filter", "kind": "simulated",
            "verdict": "pass" if coverage_pct >= 60 else ("warn" if coverage_pct >= 35 else "fail"),
            "detail": f"Covers {coverage_pct}% of the skills your matched jobs actually demand",
        })
    # HiredScore-style composite grade (deterministic, labeled simulated)
    score = 0.0
    if coverage_pct is not None:
        score += min(1.0, coverage_pct / 80.0) * 0.45
    score += min(1.0, metrics_density / 0.5) * 0.25       # ≥50% metric-bearing bullets = full credit
    score += (six["score"] / six["out_of"]) * 0.20
    score += (0.10 if not gaps else 0.0)
    grade = _GRADES[0] if score >= 0.8 else _GRADES[1] if score >= 0.6 else _GRADES[2] if score >= 0.4 else _GRADES[3]
    out.append({
        "name": "AI grader (HiredScore-style)", "kind": "simulated",
        "verdict": {"A": "pass", "B": "pass", "C": "warn", "D": "fail"}[grade],
        "detail": (f"Grade {grade} — keyword coverage, evidence density "
                   f"({int(metrics_density * 100)}% of bullets carry a number), scan quality, and "
                   f"{'no unexplained gaps' if not gaps else str(len(gaps)) + ' unexplained gap(s)'} combined"),
        "grade": grade,
    })
    out.append({
        "name": "Human 6-second scan", "kind": "simulated",
        "verdict": "pass" if six["score"] >= 5 else ("warn" if six["score"] >= 3 else "fail"),
        "detail": f"{six['score']}/{six['out_of']} first-glance checks — {six['verdict']}",
    })
    return out


# ── Panel 5: detailed project suggestions ────────────────────────────────────
# Concrete, buildable specs for the most-demanded skills. Each earns a real,
# grounded resume bullet — the anti-keyword-stuffing path: earn it, then list it.
_PROJECT_TEMPLATES = {
    "kafka": {
        "name": "Clickstream pipeline on Kafka",
        "what": "Stream simulated e-commerce click events through Kafka into a Postgres sink with a small consumer that computes rolling conversion rates.",
        "stack": "Python, kafka-python or Redpanda (single-binary Kafka), Docker Compose, Postgres",
        "steps": ["Docker-compose a 1-broker Kafka + producer emitting 1K events/min",
                  "Consumer group that aggregates per-minute funnel metrics into Postgres",
                  "Add a dead-letter topic + replay script — the part interviews ask about"],
        "bullet": "Built a Kafka streaming pipeline processing 1K events/min with consumer-group aggregation, dead-letter handling and replay",
    },
    "kubernetes": {
        "name": "Deploy your own app to k3s",
        "what": "Take any project you already have and run it on a lightweight Kubernetes cluster with real manifests, probes, and an autoscaler.",
        "stack": "k3s or kind, Helm, GitHub Actions",
        "steps": ["Containerize the app; write Deployment/Service/Ingress manifests",
                  "Add liveness/readiness probes + HPA on CPU",
                  "CI job that helm-upgrades on every push to main"],
        "bullet": "Deployed a production-style service to Kubernetes (k3s) with health probes, HPA autoscaling and Helm-based CI/CD",
    },
    "aws": {
        "name": "Serverless URL shortener on AWS free tier",
        "what": "Lambda + API Gateway + DynamoDB with infrastructure as code — small enough for a weekend, real enough to discuss cold starts and IAM.",
        "stack": "AWS Lambda, DynamoDB, API Gateway, Terraform or SAM",
        "steps": ["Define the whole stack in Terraform/SAM (no console clicking)",
                  "Add a custom domain + CloudWatch alarms on p99 latency",
                  "Load-test and write down the cold-start numbers you measured"],
        "bullet": "Shipped a serverless service on AWS (Lambda/DynamoDB/API Gateway) fully defined in Terraform, with CloudWatch p99 alarming",
    },
    "terraform": {
        "name": "Terraform-ize a real environment",
        "what": "Reproduce your app's whole hosting setup (DNS, compute, DB, secrets) as a Terraform module with remote state.",
        "stack": "Terraform, any cloud's free tier",
        "steps": ["Module with variables for env (dev/prod), remote state in object storage",
                  "Plan/apply via CI with manual approval gate",
                  "Write a destroy/rebuild runbook and actually run it"],
        "bullet": "Authored reusable Terraform modules with remote state and CI plan/apply gates; environment rebuildable from zero in minutes",
    },
    "react": {
        "name": "Rebuild one real screen of a product you use",
        "what": "Pick a data-heavy screen (dashboard, feed) and rebuild it with live state, optimistic updates, and accessibility passes.",
        "stack": "React, TypeScript, Vite, React Query",
        "steps": ["Typed API layer with React Query caching + optimistic mutation",
                  "Keyboard navigation + ARIA pass (screeners love the word 'accessibility' because it's rare)",
                  "Lighthouse to 95+ and record the before/after numbers"],
        "bullet": "Built a TypeScript/React dashboard with cached optimistic updates and full keyboard accessibility; Lighthouse 95+",
    },
    "llm": {
        "name": "Grounded RAG service over your own documents",
        "what": "A retrieval-augmented QA service with citation-forcing and an eval set — the exact pattern companies are hiring for.",
        "stack": "Python, FastAPI, an embedding model, FAISS/pgvector, any LLM API or a local model",
        "steps": ["Chunk + embed a real corpus (your notes, docs of a tool you use)",
                  "Answer endpoint that must cite retrieved chunks or refuse",
                  "20-question eval set with automatic scoring — report the accuracy number"],
        "bullet": "Built a RAG question-answering service with citation-grounded responses and a 20-case automated eval (X% accuracy)",
    },
    "rag": "llm", "genai": "llm", "machine learning": "llm",
    "postgresql": {
        "name": "Query-performance surgery on a public dataset",
        "what": "Load a 10M-row public dataset and take three slow queries to <100ms with real EXPLAIN-driven work.",
        "stack": "Postgres, any SQL client",
        "steps": ["Load data; write 3 realistic slow queries",
                  "Fix with indexes/partitioning/rewrites, EXPLAIN ANALYZE before/after",
                  "Write the numbers down: 'query X: 4.2s → 80ms'"],
        "bullet": "Optimized analytical queries on a 10M-row Postgres dataset from seconds to <100ms via indexing and plan analysis",
    },
    "docker": {
        "name": "Shrink and harden a real image",
        "what": "Take any project image from 1GB+ to <100MB with multi-stage builds, then add a vulnerability scan gate.",
        "stack": "Docker, Trivy, GitHub Actions",
        "steps": ["Multi-stage build, distroless/alpine base, layer-cache-friendly ordering",
                  "Trivy scan in CI failing on criticals",
                  "Record image size + build-time before/after"],
        "bullet": "Reduced a service image 10x via multi-stage builds and added CI vulnerability scanning (Trivy) with hard gates",
    },
    "go": {
        "name": "Concurrent job-queue worker in Go",
        "what": "A worker pool that pulls jobs from Redis with graceful shutdown, retries with backoff, and Prometheus metrics.",
        "stack": "Go, Redis, Prometheus",
        "steps": ["Worker pool with context-based cancellation",
                  "Exponential backoff + dead-letter after N attempts",
                  "Expose /metrics; graph throughput under load"],
        "bullet": "Built a Go worker service (goroutine pool, graceful shutdown, retry/backoff) instrumented with Prometheus metrics",
    },
}


def project_suggestions(learn_gaps: List[dict], max_projects: int = 5) -> List[dict]:
    """Turn the top 'learn' skill gaps into detailed, buildable project cards."""
    out: List[dict] = []
    for item in learn_gaps[:max_projects * 2]:
        skill = (item.get("skill") or "").lower()
        tpl = None
        for key, val in _PROJECT_TEMPLATES.items():
            if key in skill:
                tpl = _PROJECT_TEMPLATES[val] if isinstance(val, str) else val
                break
        if tpl is None:
            tpl = {
                "name": f"Weekend proof-of-work: {item.get('skill')}",
                "what": (f"Build the smallest real thing that exercises {item.get('skill')} end-to-end, "
                         "deploy or publish it, and write a README with what you measured."),
                "stack": f"{item.get('skill')} + whatever you already know",
                "steps": ["Define one concrete outcome (a working endpoint, a report, a deployed page)",
                          "Build it in a public repo with an honest README",
                          "Add one measured number (speed, size, accuracy) — numbers make bullets"],
                "bullet": f"Built and published a working {item.get('skill')} project with measured results",
            }
        out.append({
            "skill": item.get("skill"),
            "demand": item.get("demand"),
            "demand_pct": item.get("pct"),
            "example_jobs": item.get("example_jobs", []),
            **tpl,
        })
        if len(out) >= max_projects:
            break
    return out


# ── Panel 6: semantic coverage (embeddings — optional) ───────────────────────
def semantic_coverage(resume_text: str, demanded_phrases: List[str],
                      threshold: float = 0.60, max_phrases: int = 40) -> Optional[dict]:
    """Meaning-level coverage: which demanded skills/phrases the resume covers
    semantically even without the literal keyword (and which it truly lacks).
    Uses the same MiniLM model the matcher already loads. Returns None when the
    model is unavailable — callers must treat this panel as optional."""
    try:
        from app.matching.matcher import _get_embed_model
        model = _get_embed_model()
        bullets = _BULLET_RE.findall(resume_text or "") or [
            ln.strip() for ln in (resume_text or "").splitlines() if len(ln.strip()) > 30][:60]
        phrases = [p for p in demanded_phrases if p][:max_phrases]
        if not bullets or not phrases:
            return None
        eb = model.encode(bullets, normalize_embeddings=True)
        ep = model.encode(phrases, normalize_embeddings=True)
        sims = ep @ eb.T                     # (phrases, bullets) cosine
        best = sims.max(axis=1)
        covered, missing = [], []
        for phrase, s in zip(phrases, best):
            bucket = covered if float(s) >= threshold else missing
            bucket.append({"phrase": phrase, "similarity": round(float(s), 2)})
        return {
            "threshold": threshold,
            "covered": sorted(covered, key=lambda x: -x["similarity"]),
            "missing": sorted(missing, key=lambda x: x["similarity"]),
            "note": ("Semantic = your experience covers the idea even without the exact word. "
                     "'Missing' here means the resume shows no related experience at all."),
        }
    except Exception as e:
        log.debug("semantic coverage unavailable: %s", e)
        return None


# ── Composition ──────────────────────────────────────────────────────────────
def compute_resume_xray(user_id: Optional[str]) -> dict:
    """The full X-ray. Reuses skill_gap's evidence loaders so resume/GitHub/
    LinkedIn are fetched exactly the way the rest of the app sees them."""
    from app.intelligence.skill_gap import compute_skill_gap, _load_resume_text, _user_arg
    from app.db.init_db import get_session
    from app.db.models import UserProfile
    from sqlmodel import select
    import json as _json

    uid = _user_arg(user_id)
    with get_session() as session:
        profile = session.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()

    resume_text, resume_loaded = _load_resume_text(user_id, profile)
    if not resume_loaded or not (resume_text or "").strip():
        return {"resume_loaded": False}

    gap = compute_skill_gap(user_id)
    matched_n = len(gap.get("matched", []))
    demanded_n = matched_n + len(gap.get("add_visibility", [])) + len(gap.get("learn", []))
    coverage_pct = round(100 * (matched_n + len(gap.get("add_visibility", []))) / demanded_n) if demanded_n else None

    parse = ats_parse(resume_text)
    exp_json = None
    try:
        exp_json = _json.loads(profile.experience_json) if profile and profile.experience_json else None
    except Exception:
        pass
    gaps = employment_gaps(resume_text, exp_json)
    six = six_second_scan(resume_text, profile)

    from app.tailoring.doctor import _METRIC_RE
    bullets = _BULLET_RE.findall(resume_text)
    metrics_density = (sum(1 for b in bullets if _METRIC_RE.search(b)) / len(bullets)) if bullets else 0.0

    screeners = screener_panel(parse, six, coverage_pct, metrics_density, gaps)

    # Ranked reject reasons — every entry states WHY in screener mechanics.
    reject: List[dict] = []
    for i in parse["issues"]:
        if i["severity"] == "fail":
            reject.append({"rank": 1, "reason": i["what"], "detail": i["why"], "fix": "See ATS parse panel"})
    if coverage_pct is not None and coverage_pct < 50:
        reject.append({"rank": 2, "reason": f"Keyword coverage {coverage_pct}% vs your matched jobs",
                       "detail": "Keyword filters shortlist by demanded-skill overlap; below ~half coverage you rarely surface.",
                       "fix": "Add the 'add to resume' skills you already have evidence for (sync panel); earn the rest (projects panel)."})
    for g in gaps:
        reject.append({"rank": 3, "reason": f"{g['months']}-month gap ({g['window']})", "detail": g["detail"],
                       "fix": "One explaining line converts it from a flag to a footnote."})
    if metrics_density < 0.3 and bullets:
        reject.append({"rank": 4, "reason": f"Only {int(metrics_density * 100)}% of bullets carry a number",
                       "detail": "Screeners (human and AI) treat unmeasured claims as unverifiable filler; measured bullets read as evidence.",
                       "fix": "Answer the Metric-gap questions in the Resume Coach — your real numbers become grounded bullets."})
    for f in six["failed"]:
        reject.append({"rank": 5, "reason": f"6-second scan miss: {f['label']}",
                       "detail": "The first-glance pass decides whether the rest gets read at all.", "fix": "See 6-second panel"})
    reject.sort(key=lambda r: r["rank"])

    demanded_phrases = [i["skill"] for i in
                        (gap.get("matched", []) + gap.get("add_visibility", []) + gap.get("learn", []))]
    semantic = semantic_coverage(resume_text, demanded_phrases)

    return {
        "resume_loaded": True,
        "six_second": six,
        "ats_parse": parse,
        "screeners": screeners,
        "employment_gaps": gaps,
        "reject_reasons": reject[:12],
        "keyword_coverage_pct": coverage_pct,
        "metrics_density_pct": int(metrics_density * 100),
        "projects": project_suggestions(gap.get("learn", [])),
        "sync": {
            "github": gap.get("github", {}),
            "linkedin": gap.get("linkedin", {}),
            "add_to_resume": gap.get("add_visibility", []),   # proof exists elsewhere, missing on resume
            "matched": gap.get("matched", [])[:15],
        },
        "semantic": semantic,
        "scanned_jobs": gap.get("scanned_jobs", 0),
        "disclaimer": ("Simulated screening mechanics — real employers vary. Every verdict here is "
                       "computed from your actual resume, profile and matched jobs; nothing is invented."),
    }
