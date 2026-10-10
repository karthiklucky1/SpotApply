"""Skill-gap analysis: what the user's matched jobs demand vs what they can prove.

For each skill demanded across the user's top matches, classify:
  - ``matched``        — the phrase appears verbatim on the résumé.
  - ``add_visibility`` — not on the résumé, but proof exists on the user's own
                          GitHub (repo language/topic/description) or in their
                          pasted LinkedIn text. Advice: add it to your résumé
                          and LinkedIn *yourself* — we never inject skills.
  - ``learn``          — no evidence anywhere. Advice: build a small project,
                          push it to GitHub, add the skill to LinkedIn.

Deterministic (no LLM): JD phrases come from ats_keywords, evidence checks are
verbatim-phrase lookups against the résumé / GitHub / LinkedIn text blobs. The
only network call is a one-time GitHub harvest when the user has a GitHub URL
but no cached harvest yet (stored to UserPersonalMemory for next time).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import re
from datetime import timedelta
from types import SimpleNamespace

from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Job, UserPersonalMemory, UserProfile
from app.tailoring.ats_keywords import _normalize, _phrase_present, extract_jd_phrases, is_skill_like

log = logging.getLogger(__name__)

# Phrases per job to consider; demand floor keeps one-off JD noise out of the
# dashboard once there are enough scanned jobs to make counts meaningful.
_PHRASES_PER_JOB = 14
_MIN_DEMAND_WITH_MANY_JOBS = 2

# ── Accuracy (2026-10-10) ────────────────────────────────────────────────────
# Measured on 8 synthetic (resume, JD) pairs with 12 true gaps before this
# change: precision 0.08, recall 0.58. The "Learn these" cards were boilerplate
# ("dental and vision", "equal opportunity employer", "design and own") and
# spelling differences ("golang" vs the resume's Go, "kubernetes" vs K8s,
# "postgresql" vs Postgres, "aws" vs Amazon Web Services, "llms" vs LLM). Three
# rules fix that: strip the boilerplate before extraction, keep only phrases a
# candidate can possess (ats_keywords.is_skill_like), and compare SKILLS, not
# strings — every demand and every presence check goes through one canonical
# name (aliases below + data/skill_graph.json + the inventory's acronym table).

_BOILERPLATE_RE = re.compile(
    r"[^.\n]*\b(?:equal (?:employment )?opportunity|regardless of|without regard to|"
    r"dental|vision insurance|401\s*\(?k\)?|paid time off|pto\b|parental leave|"
    r"health insurance|medical, dental|benefits? (?:package|include)|sexual orientation|"
    r"gender identity|national origin|protected veteran|disability status|"
    r"reasonable accommodation|e-verify|background check|drug.?free|"
    r"we celebrate|diverse (?:team|workplace)|inclusive (?:workplace|environment)|"
    r"compensation range|salary range|base salary|equity|stock options)\b[^.\n]*[.\n]?",
    re.I)

# Spelling, not inference: each alias names the SAME skill as its canonical.
_ALIASES = {
    "golang": "go", "go lang": "go",
    "k8s": "kubernetes", "k8": "kubernetes",
    "postgres": "postgresql", "postgre sql": "postgresql",
    "pyspark": "spark", "apache spark": "spark",
    "amazon web services": "aws", "google cloud platform": "gcp", "google cloud": "gcp",
    "microsoft azure": "azure",
    "nodejs": "node.js", "node js": "node.js", "node": "node.js",
    "ml": "machine learning", "nlp": "natural language processing",
    "llm": "llm", "llms": "llm", "large language models": "llm", "large language model": "llm",
    "rest apis": "rest api", "restful api": "rest api", "restful apis": "rest api", "rest": "rest api",
    "data pipelines": "data pipeline",
    "js": "javascript", "ts": "typescript",
    "react.js": "react", "reactjs": "react",
    "ci cd": "ci/cd", "cicd": "ci/cd", "ci/cd pipelines": "ci/cd",
    "tf": "terraform", "gh actions": "github actions",
    "scikit learn": "scikit-learn", "sklearn": "scikit-learn",
    "deep learning": "deep learning", "dl": "deep learning",
    "docker containers": "docker", "containerization": "docker",
    "dynamo db": "dynamodb", "mongo": "mongodb", "elastic search": "elasticsearch",
    "micro services": "microservices", "micro-services": "microservices",
    "fine tuning": "fine-tuning",
    "retrieval augmented generation": "rag", "retrieval-augmented generation": "rag",
    "gen ai": "generative ai", "genai": "generative ai",
    "power bi": "power bi", "powerbi": "power bi",
}

# Lower-case tool names the proper-noun test below cannot see.
_KNOWN_TOOLS = frozenset({
    "dbt", "pandas", "numpy", "scipy", "pytest", "matplotlib", "tableau", "power bi", "looker",
    "dynamodb", "kinesis", "athena", "redshift", "databricks", "excel", "sas", "stata", "matlab",
    "hadoop", "hive", "presto", "trino", "flink", "beam", "dagster", "prefect", "fivetran",
    "salesforce", "hubspot", "figma", "jira", "confluence", "linux", "bash", "shell", "powershell",
    "git", "github", "gitlab", "bitbucket", "nginx", "oauth", "saml", "jwt", "html", "css", "sass",
    "tailwind", "next.js", "nextjs", "vue", "angular", "svelte", "react", "react native", "flutter",
    "swift", "kotlin", "android", "ios", "scala", "ruby", "rails", "php", "laravel", "perl", "haskell",
    "elixir", "erlang", "clojure", ".net", "asp.net", "unity", "unreal", "opencv", "cuda", "onnx",
    "mlflow", "kubeflow", "ray", "dask", "polars", "duckdb", "clickhouse", "cassandra", "neo4j",
    "sqlite", "oracle", "sql server", "mssql", "teradata", "informatica", "talend", "ssis", "sap",
    "servicenow", "splunk", "kibana", "logstash", "new relic", "pagerduty", "ansible", "puppet",
    "chef", "helm", "istio", "argocd", "argo cd", "circleci", "cloudformation", "pulumi", "fargate",
    "ecs", "eks", "gke", "aks", "bigtable", "pub/sub", "firestore", "firebase", "supabase", "auth0",
    "okta", "stripe", "twilio", "segment", "amplitude", "mixpanel", "google analytics", "seo",
    "a/b testing", "kanban", "spring", "spring boot", "hibernate", "maven", "gradle", "junit",
    "selenium", "cypress", "playwright", "jest", "mocha", "webpack", "vite", "redux", "graphql",
    "protobuf", "websockets", "oauth2", "sso", "ldap", "active directory", "vmware", "citrix",
    "sharepoint", "dynamics 365", "workday", "netsuite", "quickbooks", "autocad", "solidworks",
    "revit", "labview", "simulink", "plc", "scada", "hl7", "fhir", "epic", "cerner",
})
_TECH_CHAR_RE = re.compile(r"[0-9+#./]")

# Single letters and two-char names the n-gram extractor drops (it ignores
# tokens under three characters), matched on the RAW text with the context that
# makes them a language: "Go services" / "Golang" (never "go-to-market" or "go
# live"), "R, Python" / "R programming" (never "R&D"), "C/C++" (never "C" the
# grade), "C#", "S3".
_SHORT_SKILLS = {
    "go": re.compile(r"(?<![\w-])(?:Golang|Go)(?![\w-])(?!\s*(?:to|live|forward|beyond|above|ahead|for)\b)"),
    "r": re.compile(r"(?<![\w&/+.])R(?![\w&+])(?=\s*(?:[,/;)]|\band\b|\bor\b|programming|language|studio|shiny|markdown|$))", re.M),
    "c": re.compile(r"(?<![\w#+./])C(?![\w#+])(?=\s*(?:/\s*C\+\+|,\s*C\+\+|\s+and\s+C\+\+|&\s*C\+\+|programming|language))"),
    "c#": re.compile(r"(?<!\w)C#"),
    "c++": re.compile(r"(?<!\w)C\+\+"),
    "s3": re.compile(r"(?<!\w)S3(?!\w)"),
}


def _strip_boilerplate(text: str) -> str:
    return _BOILERPLATE_RE.sub(" ", text or "")


def canon_skill(phrase: str) -> str:
    """ONE name per skill: aliases, then the skill graph's own aliases."""
    key = re.sub(r"\s+", " ", (phrase or "").strip().lower())
    key = _ALIASES.get(key, key)
    try:
        from app.matching.skill_graph import load_graph
        g = load_graph()
        c = g.canon(key)
        if c:
            key = _ALIASES.get(c, c)
    except Exception:
        pass
    return key


def _variants(canonical: str) -> list[str]:
    """Every spelling that names this canonical skill."""
    out = {canonical}
    for alias, c in _ALIASES.items():
        if c == canonical:
            out.add(alias)
    return sorted(out, key=len, reverse=True)


def skill_present(canonical: str, raw_text: str, normalized_text: str) -> bool:
    """Is this skill on the text under ANY of its names? Verbatim phrase, the
    punctuation-tolerant inventory pattern with acronym expansion
    (requirements._present_in), or the guarded short-token regex."""
    if not canonical:
        return False
    short = _SHORT_SKILLS.get(canonical)
    if short is not None:
        # The guarded regex is the ONLY safe test for a one-letter name: a
        # phrase match reads "we go live" as Go and "R&D" as R.
        return bool(short.search(raw_text or ""))
    for v in _variants(canonical):
        if normalized_text and _phrase_present(v, normalized_text):
            return True
    try:
        from app.tailoring.requirements import _present_in
        for v in _variants(canonical):
            if v in _SHORT_SKILLS:
                continue                       # the regex above is the only safe test
            if _present_in(v, raw_text or ""):
                return True
    except Exception:
        pass
    return False


def _is_curated(canonical: str) -> bool:
    from app.tailoring.ats_keywords import _TECH_PHRASE_SET
    if canonical in _TECH_PHRASE_SET or canonical in _KNOWN_TOOLS or canonical in _ALIASES.values():
        return True
    try:
        from app.matching.skill_graph import load_graph
        g = load_graph()
        return canonical in g.aliases or canonical in g.aliases.values()
    except Exception:
        return False


def _proper_noun_in(phrase: str, raw_text: str) -> bool:
    """Does the posting write this phrase as a NAME — each word capitalised,
    not at the start of a sentence, not inside an ALL-CAPS header line? That is
    how "Tableau", "Snowflake" and "Spring Boot" appear and how "daily",
    "deployment" and "learning systems" never do."""
    words = (phrase or "").split()
    if not words:
        return False
    body = r"\s+".join(re.escape(w[:1].upper() + w[1:]) for w in words)
    for m in re.finditer(rf"(?<![\w-]){body}(?![\w-])", raw_text or ""):
        line_start = raw_text.rfind("\n", 0, m.start()) + 1
        line = raw_text[line_start: raw_text.find("\n", m.end()) if raw_text.find("\n", m.end()) >= 0 else len(raw_text)]
        letters = re.sub(r"[^A-Za-z]", "", line)
        if letters and letters.isupper() and len(letters) > 3:
            continue                                   # a section header
        before = raw_text[line_start: m.start()].rstrip()
        if before and before[-1] not in ".!?:;":
            return True                                # mid-sentence capital = a name
        if before and before[-1] in ",;" :
            return True
    return False


def _accept(canonical: str, phrase: str, raw_jd: str) -> bool:
    """A demand must be something a candidate can learn or prove: a curated
    technology, a token with tech punctuation (c++, node.js, ci/cd, s3), or a
    name the posting capitalises mid-sentence."""
    if _is_curated(canonical):
        return True
    if any(_TECH_CHAR_RE.search(t) for t in canonical.split()):
        return True
    return _proper_noun_in(phrase, raw_jd)


def demanded_skills(jd_text: str, per_job: int = _PHRASES_PER_JOB) -> list[str]:
    """Canonical skills one posting asks for — skill-like phrases only, with
    the boilerplate gone and the short language names added back."""
    text = _strip_boilerplate(jd_text or "")
    try:
        phrases = extract_jd_phrases(text, top_n=per_job * 2)
    except Exception:
        phrases = []
    out: list[str] = []
    seen: set[str] = set()
    for p in phrases:
        if not is_skill_like(p):
            continue
        c = canon_skill(p)
        if not c or c in seen or not _accept(c, p, text):
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= per_job:
            break
    for name, rx in _SHORT_SKILLS.items():
        if name not in seen and rx.search(text):
            seen.add(name)
            out.append(name)
    return out


def _user_arg(user_id: Optional[str]) -> Optional[str]:
    return user_id if user_id and user_id != "local" else None


def _top_jobs(user_id: Optional[str], limit: int) -> list:
    """The user's best-ranked open postings — PROJECTED (five columns) and
    ordered in SQL. This used to `select(Job)` every scored open row and sort
    in Python: whole rows, full descriptions, for a 30-job answer."""
    from sqlalchemy import nullslast
    uid = _user_arg(user_id)
    with get_session() as session:
        q = select(Job.id, Job.title, Job.company, Job.description,
                   Job.blended_score, Job.rerank_score).where(
            Job.user_id == uid,  # noqa: E711
            Job.rerank_score != None,  # noqa: E711
            Job.is_closed == False,  # noqa: E712
        ).order_by(nullslast(Job.blended_score.desc()), nullslast(Job.rerank_score.desc())
                   ).limit(max(1, int(limit)) * 2)
        rows = session.exec(q).all()
    jobs = [SimpleNamespace(id=r[0], title=r[1], company=r[2], description=r[3],
                            blended_score=r[4], rerank_score=r[5]) for r in rows]
    jobs.sort(key=lambda j: (j.blended_score or j.rerank_score or 0), reverse=True)
    return jobs[:limit]


def _github_evidence(user_id: Optional[str], profile: Optional[UserProfile]) -> dict:
    """Return {connected, username, blob, repos:[{name, text}], harvested_at}.

    Uses the latest cached UserPersonalMemory(github) row; harvests live once
    when the user has a GitHub URL but no cached row yet.
    """
    uid = _user_arg(user_id)
    out = {"connected": False, "username": "", "blob": "", "repos": [], "harvested_at": None}

    row = None
    with get_session() as session:
        q = (
            select(UserPersonalMemory)
            .where(UserPersonalMemory.user_id == uid,  # noqa: E711
                   UserPersonalMemory.source == "github")
            .order_by(UserPersonalMemory.created_at.desc())  # type: ignore[attr-defined]
        )
        row = session.exec(q).first()

    gh: dict = {}
    if row and row.raw_content:
        try:
            raw = json.loads(row.raw_content)
            gh = raw.get("github", raw) or {}
            out["harvested_at"] = row.created_at.isoformat() if row.created_at else None
        except Exception:
            gh = {}

    github_url = (getattr(profile, "github_url", "") or "").strip() if profile else ""
    # A harvest that came back empty (rate limit, 404, private) is CACHED for a
    # day like a full one: it used to re-harvest and INSERT a new row on every
    # skill-gap or X-ray call — unbounded rows, up to 30 s inside the request.
    recent_attempt = bool(row and row.created_at
                          and row.created_at >= datetime.utcnow() - timedelta(hours=24))
    if not gh.get("repos") and github_url and not recent_attempt:
        try:
            from app.intelligence.harvester import harvest_github
            gh = harvest_github(github_url) or {}
            with get_session() as session:
                session.add(UserPersonalMemory(
                    user_id=uid, source="github",
                    raw_content=json.dumps({"github": gh}),
                    parsed_updates="", recommendations="",
                    created_at=datetime.utcnow(),
                ))
                session.commit()
            out["harvested_at"] = datetime.utcnow().isoformat()
        except Exception as e:
            log.warning("Skill-gap GitHub harvest failed: %s", e)
            gh = gh or {}

    repos = gh.get("repos") or []
    if not repos:
        return out

    out["connected"] = True
    out["username"] = gh.get("username") or ""
    parts = []
    for r in repos:
        text = " ".join([
            str(r.get("name") or ""),
            str(r.get("description") or ""),
            str(r.get("language") or ""),
            " ".join(r.get("topics") or []),
        ])
        out["repos"].append({"name": r.get("name") or "", "text": _normalize(text)})
        parts.append(text)
    for e in gh.get("events") or []:
        parts.append(str(e.get("message") or ""))
    out["blob"] = _normalize(" ".join(parts))
    return out


def _linkedin_evidence(user_id: Optional[str]) -> dict:
    """Latest pasted-LinkedIn text (user-supplied — we never scrape LinkedIn)."""
    uid = _user_arg(user_id)
    with get_session() as session:
        q = (
            select(UserPersonalMemory)
            .where(UserPersonalMemory.user_id == uid,  # noqa: E711
                   UserPersonalMemory.source == "linkedin")
            .order_by(UserPersonalMemory.created_at.desc())  # type: ignore[attr-defined]
        )
        row = session.exec(q).first()
    if not row or not (row.raw_content or "").strip():
        return {"connected": False, "blob": ""}
    return {"connected": True, "blob": _normalize(row.raw_content)}


def _load_resume_text(user_id: Optional[str], profile: Optional[UserProfile]) -> tuple[str, bool]:
    try:
        from app.matching.pipeline import _load_resume
        return _load_resume(user_id=_user_arg(user_id)), True
    except Exception as e:
        log.info("Skill-gap: no resume loaded (%s) — falling back to profile skills", e)
        fallback = " ".join([
            getattr(profile, "key_skills", "") or "",
            getattr(profile, "professional_summary", "") or "",
        ]) if profile else ""
        return fallback, False


def _advice(skill: str, status: str, demand: int, repo: str = "") -> str:
    if status == "add_visibility":
        where = f" (your GitHub repo “{repo}” already shows it)" if repo else " (found in your LinkedIn text)"
        return (
            f"You already have proof of {skill}{where}, but it's invisible to "
            f"recruiters scanning your resume — add it to your resume and your "
            f"LinkedIn skills yourself."
        )
    return (
        f"{demand} of your top matches want {skill} and you have no proof of it "
        f"yet. Build a small project using {skill}, push it to GitHub with a "
        f"clear README, and add the skill to your LinkedIn — recruiters verify "
        f"there, not on claims."
    )


def compute_skill_gap(user_id: Optional[str], top_n_jobs: int = 30) -> dict:
    """Aggregate skill demand across the user's top matches and classify each
    skill by the strongest evidence the user can show for it."""
    uid = _user_arg(user_id)
    profile = None
    with get_session() as session:
        q = select(UserProfile).where(UserProfile.user_id == uid)  # noqa: E711
        profile = session.exec(q).first()

    jobs = _top_jobs(user_id, top_n_jobs)
    if not jobs:
        return {"scanned_jobs": 0, "matched": [], "add_visibility": [], "learn": [],
                "resume_loaded": False, "github": {"connected": False},
                "linkedin": {"connected": False}}

    resume_text, resume_loaded = _load_resume_text(user_id, profile)
    resume_norm = _normalize(resume_text)
    gh = _github_evidence(user_id, profile)
    li = _linkedin_evidence(user_id)
    gh_raw = " ".join(r.get("text", "") for r in gh["repos"]) + " " + (gh.get("blob") or "")

    # canonical skill -> {demand, jobs:[(title, company)]}
    demand: dict[str, dict] = {}
    for job in jobs:
        jd = job.description or ""
        if not jd.strip():
            continue
        for key in demanded_skills(jd):
            entry = demand.setdefault(key, {"demand": 0, "jobs": []})
            entry["demand"] += 1
            if len(entry["jobs"]) < 3:
                entry["jobs"].append({"title": job.title, "company": job.company})

    min_demand = _MIN_DEMAND_WITH_MANY_JOBS if len(jobs) >= 10 else 1
    matched, add_visibility, learn = [], [], []
    for skill, entry in demand.items():
        if entry["demand"] < min_demand:
            continue
        item = {
            "skill": skill,
            "demand": entry["demand"],
            "pct": round(100 * entry["demand"] / len(jobs)),
            "example_jobs": entry["jobs"],
        }
        if resume_text and skill_present(skill, resume_text, resume_norm):
            item["status"] = "matched"
            matched.append(item)
            continue
        repo_hit = next((r["name"] for r in gh["repos"] if skill_present(skill, r["text"], r["text"])), "")
        if repo_hit or (gh["blob"] and skill_present(skill, gh_raw, gh["blob"])):
            item["status"] = "add_visibility"
            item["evidence"] = {"source": "github", "repo": repo_hit}
            item["advice"] = _advice(skill, "add_visibility", entry["demand"], repo_hit)
            add_visibility.append(item)
            continue
        if li["blob"] and skill_present(skill, li["blob"], li["blob"]):
            item["status"] = "add_visibility"
            item["evidence"] = {"source": "linkedin", "repo": ""}
            item["advice"] = _advice(skill, "add_visibility", entry["demand"])
            add_visibility.append(item)
            continue
        item["status"] = "learn"
        item["advice"] = _advice(skill, "learn", entry["demand"])
        learn.append(item)

    for bucket in (matched, add_visibility, learn):
        bucket.sort(key=lambda x: x["demand"], reverse=True)

    return {
        "scanned_jobs": len(jobs),
        "resume_loaded": resume_loaded,
        "github": {"connected": gh["connected"], "username": gh["username"],
                   "harvested_at": gh["harvested_at"]},
        "linkedin": {"connected": li["connected"]},
        "matched": matched[:40],
        "add_visibility": add_visibility[:40],
        "learn": learn[:40],
    }
