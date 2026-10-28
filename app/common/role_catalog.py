"""Target-role suggestions: real job titles, a typeahead over them, and
suggestions read from the user's own resume.

Why (founder, 2026-10-28): target roles decide which jobs reach a board, and
many people do not know which titles to ask for. Typing "python developer"
showed nothing — the box accepted whatever was typed, lower-cased — and the
only help was six generic suggestions. Now:

  * ``complete(q)`` — as the user types, real titles that start with or
    contain what they typed, then the NEIGHBOURS of what they typed (the same
    symmetric role families the title gate already uses,
    ``discovery.title_filter._ROLE_FAMILIES``): "python developer" also offers
    Backend Engineer, Software Engineer and Full Stack Engineer, because those
    postings are the same market.
  * ``suggest(profile, chosen)`` — grouped suggestions, each with the reason:
    from the resume (current and past titles, and what the skills point to),
    related to the roles already chosen, and common in the user's field.

Pure string work: no LLM, no network, no database.
"""
from __future__ import annotations

import json
import re
from typing import Iterable, List, Optional

# Ordered roughly by how often people search them: earlier = shown first when
# several titles match equally well.
CATALOG: tuple = (
    # Software
    "Software Engineer", "Software Developer", "Backend Engineer", "Backend Developer",
    "Full Stack Engineer", "Full Stack Developer", "Frontend Engineer", "Frontend Developer",
    "Python Developer", "Python Engineer", "Java Developer", "Java Engineer",
    "JavaScript Developer", "TypeScript Developer", "Node.js Developer", "React Developer",
    "Angular Developer", "Golang Developer", "Go Engineer", "C++ Developer", ".NET Developer",
    "C# Developer", "Ruby on Rails Developer", "PHP Developer", "Scala Engineer", "Rust Engineer",
    "Django Developer", "API Engineer", "Web Developer", "Application Developer",
    "Software Development Engineer", "Software Engineer II", "Senior Software Engineer",
    "Staff Software Engineer", "Junior Software Engineer", "Associate Software Engineer",
    "New Grad Software Engineer", "Software Engineer Intern", "Embedded Software Engineer",
    "Firmware Engineer", "Systems Software Engineer", "Game Developer",
    # AI / ML
    "AI Engineer", "Machine Learning Engineer", "Generative AI Engineer", "LLM Engineer",
    "Applied AI Engineer", "AI/ML Engineer", "AI Full Stack Developer", "AI Software Engineer",
    "Generative AI Platform Engineer", "AI Platform Engineer", "AI Solutions Architect",
    "AI Solutions Engineer", "Forward Deployed Engineer", "Machine Learning Operations Engineer",
    "MLOps Engineer", "Deep Learning Engineer", "NLP Engineer", "Computer Vision Engineer",
    "Applied Scientist", "Research Engineer", "Research Scientist", "Machine Learning Scientist",
    "AI Research Scientist", "Prompt Engineer", "Conversational AI Engineer",
    # Data
    "Data Scientist", "Data Engineer", "Data Analyst", "Analytics Engineer",
    "Business Intelligence Analyst", "Business Analyst", "Big Data Engineer",
    "Data Platform Engineer", "ETL Developer", "Database Administrator", "Quantitative Analyst",
    "Quantitative Researcher", "Product Analyst", "Marketing Analyst", "Financial Analyst",
    # Cloud / infrastructure
    "DevOps Engineer", "Cloud Engineer", "Platform Engineer", "Site Reliability Engineer",
    "Infrastructure Engineer", "Cloud Solutions Engineer", "Cloud Architect",
    "Solutions Architect", "Solutions Engineer", "Systems Engineer", "Network Engineer",
    "Kubernetes Engineer", "Build and Release Engineer", "Production Engineer",
    # Security
    "Security Engineer", "Application Security Engineer", "Cloud Security Engineer",
    "Cybersecurity Analyst", "Security Analyst", "Penetration Tester",
    # Mobile
    "Mobile Engineer", "iOS Engineer", "iOS Developer", "Android Engineer", "Android Developer",
    "React Native Developer", "Flutter Developer",
    # Quality
    "QA Engineer", "QA Automation Engineer", "Software Development Engineer in Test",
    "Test Engineer", "Quality Engineer",
    # Product / design / management
    "Product Manager", "Technical Product Manager", "Product Owner", "Program Manager",
    "Technical Program Manager", "Project Manager", "Scrum Master", "Engineering Manager",
    "UX Designer", "UI Designer", "Product Designer", "UX Researcher",
    "Technical Writer", "Developer Advocate", "Customer Success Engineer", "Sales Engineer",
    "Support Engineer", "Technical Support Engineer", "IT Support Specialist", "Systems Administrator",
    # Engineering (non-software)
    "Mechanical Engineer", "Electrical Engineer", "Civil Engineer", "Chemical Engineer",
    "Industrial Engineer", "Manufacturing Engineer", "Process Engineer", "Aerospace Engineer",
    "Biomedical Engineer", "Hardware Engineer", "Controls Engineer", "Structural Engineer",
    "Project Engineer", "Quality Engineer", "Environmental Engineer", "Embedded Engineer",
    # Business, health, education, legal
    "Accountant", "Staff Accountant", "Auditor", "Investment Analyst", "Operations Analyst",
    "Supply Chain Analyst", "Marketing Manager", "Digital Marketing Specialist",
    "Account Executive", "Sales Development Representative", "Customer Success Manager",
    "Recruiter", "HR Generalist", "Registered Nurse", "Pharmacist", "Physical Therapist",
    "Medical Assistant", "Clinical Research Coordinator", "Teacher", "Instructional Designer",
    "Paralegal", "Compliance Analyst",
)
CATALOG = tuple(dict.fromkeys(CATALOG))   # one entry per title, first position wins

# What a skill on the resume says about the titles worth searching. Keys are
# matched as whole words, case-insensitively, inside each listed skill.
SKILL_ROLES: tuple = (
    (("langchain", "llamaindex", "rag", "llm", "llms", "openai", "azure openai", "gpt",
      "generative ai", "genai", "prompt engineering", "vector database", "agents"),
     ("AI Engineer", "Generative AI Engineer", "LLM Engineer")),
    (("pytorch", "tensorflow", "scikit-learn", "sklearn", "machine learning", "deep learning",
      "keras", "xgboost", "ai/ml", "ml"),
     ("Machine Learning Engineer", "AI Engineer")),
    (("mlflow", "kubeflow", "sagemaker", "vertex ai", "mlops"),
     ("MLOps Engineer", "Machine Learning Engineer")),
    (("nlp", "spacy", "hugging face", "huggingface", "transformers"), ("NLP Engineer",)),
    (("opencv", "computer vision", "yolo"), ("Computer Vision Engineer",)),
    (("python",), ("Python Developer", "Backend Engineer")),
    (("fastapi", "flask", "django"), ("Backend Engineer", "Python Developer")),
    (("java", "spring", "spring boot"), ("Java Developer", "Backend Engineer")),
    (("go", "golang"), ("Golang Developer", "Backend Engineer")),
    (("node", "node.js", "nodejs", "express"), ("Node.js Developer", "Full Stack Engineer")),
    (("react", "react.js", "reactjs", "next.js", "nextjs"), ("Full Stack Engineer", "Frontend Engineer")),
    (("angular", "vue", "vue.js", "typescript", "javascript", "html", "css"), ("Frontend Engineer",)),
    (("microservices", "rest apis", "rest api", "graphql", "grpc"), ("Backend Engineer",)),
    (("docker", "kubernetes", "terraform", "ansible", "jenkins", "ci/cd", "helm"),
     ("DevOps Engineer", "Platform Engineer")),
    (("aws", "gcp", "azure", "google cloud"), ("Cloud Engineer", "Cloud Solutions Engineer")),
    (("spark", "airflow", "kafka", "dbt", "snowflake", "databricks", "etl", "hadoop"),
     ("Data Engineer",)),
    (("sql", "tableau", "power bi", "looker", "excel", "statistics"), ("Data Analyst",)),
    (("pandas", "numpy", "r"), ("Data Scientist",)),
    (("swift", "swiftui"), ("iOS Engineer",)),
    (("kotlin",), ("Android Engineer",)),
    (("flutter", "react native"), ("Mobile Engineer",)),
    (("selenium", "cypress", "playwright", "pytest", "junit"), ("QA Automation Engineer",)),
    (("c++", "embedded", "rtos", "firmware"), ("Embedded Software Engineer",)),
)

_SENIOR_RE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|head|director|manager|architect)\b", re.I)
_ACRONYMS = {"ai", "ml", "qa", "ui", "ux", "it", "hr", "nlp", "llm", "api", "sde", "sre", "etl",
             "bi", "aws", "gcp", "rn", "cto", "vp", "mlops", "devops", "ios", "ai/ml", "c++", "c#",
             ".net", "php", "sql", "ci/cd"}
_SPECIAL_CASE = {"ios": "iOS", "mlops": "MLOps", "devops": "DevOps", "node.js": "Node.js",
                 "javascript": "JavaScript", "typescript": "TypeScript", "golang": "Golang",
                 ".net": ".NET", "ai/ml": "AI/ML"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def canonical(title: str) -> str:
    """A typed title in proper case: the catalog's own spelling when it is a
    catalog title, else word-by-word with acronyms kept ("python developer" →
    "Python Developer", "ai engineer" → "AI Engineer")."""
    t = _norm(title)
    for c in CATALOG:
        if c.lower() == t:
            return c
    out = []
    for w in (title or "").split():
        lw = w.lower()
        if lw in _SPECIAL_CASE:
            out.append(_SPECIAL_CASE[lw])
        elif lw in _ACRONYMS:
            out.append(lw.upper())
        elif w[:1].islower():
            out.append(w[:1].upper() + w[1:])
        else:
            out.append(w)
    return " ".join(out)


def _families_of(phrase: str) -> set:
    """Indexes of the role families (title_filter._ROLE_FAMILIES) a phrase is
    in, read through the title gate's own alias table first ("react" →
    frontend → the software family) — the same two tables the gate uses."""
    from app.discovery.title_filter import _ROLE_FAMILIES, _ROLE_TERM_ALIASES, _term_pattern
    p = _norm(phrase)
    expanded = [p] + [a for key, aliases in _ROLE_TERM_ALIASES.items()
                      if _term_pattern(key).search(p) for a in aliases]
    text = " | ".join(expanded)
    return {i for i, fam in enumerate(_ROLE_FAMILIES)
            if any(_term_pattern(m).search(text) for m in fam)}


_FAMILY_INDEX: Optional[dict] = None


def _family_index() -> dict:
    global _FAMILY_INDEX
    if _FAMILY_INDEX is None:
        _FAMILY_INDEX = {c: _families_of(c) for c in CATALOG}
    return _FAMILY_INDEX


def related(roles: Iterable[str], exclude: Iterable[str] = (), limit: int = 8,
            allow_senior: bool = True) -> List[str]:
    """Catalog titles in the same role family as any of ``roles``."""
    fams: set = set()
    for r in roles:
        fams |= _families_of(r)
    if not fams:
        return []
    skip = {_norm(x) for x in list(exclude) + list(roles)}
    out = []
    for c, cf in _family_index().items():
        if cf & fams and _norm(c) not in skip and (allow_senior or not _SENIOR_RE.search(c)):
            out.append(c)
            if len(out) >= limit:
                break
    return out


def complete(query: str, limit: int = 8, exclude: Iterable[str] = ()) -> List[dict]:
    """Titles for what the user is typing: [{"title", "why"}], best first.
    Prefix matches, then word-prefix matches (any order: "ml eng"), then
    substrings, then the role family of what was typed."""
    q = _norm(query)
    if len(q) < 2:
        return []
    skip = {_norm(x) for x in exclude}
    toks = q.split()
    scored = []
    for idx, c in enumerate(CATALOG):
        cl = c.lower()
        if cl in skip:
            continue
        words = re.split(r"[\s/()-]+", cl)
        if cl == q:
            s = 100
        elif cl.startswith(q):
            s = 90
        elif all(any(w.startswith(t) for w in words) for t in toks):
            s = 70 + (5 if words and words[0].startswith(toks[0]) else 0)
        elif q in cl:
            s = 50
        elif q.replace(" ", "") in cl.replace(" ", ""):
            s = 45                                  # "dev ops" -> DevOps Engineer
        else:
            continue
        scored.append((-s, idx, c))
    scored.sort()
    out = [{"title": c, "why": ""} for _s, _i, c in scored[:limit]]
    if len(out) < limit:
        have = {_norm(o["title"]) for o in out} | skip
        # Neighbours of what was typed AND of its best matches: "python dev"
        # is not a family term, but the Python Developer it completes to is.
        for c in related([q] + [o["title"] for o in out[:1]], exclude=have, limit=limit - len(out)):
            out.append({"title": c, "why": "similar role"})
    return out[:limit]


def _profile_titles(profile) -> List[str]:
    titles = []
    cur = (getattr(profile, "current_title", "") or "").strip()
    if cur:
        titles.append(cur)
    try:
        for e in json.loads(getattr(profile, "experience_json", None) or "[]")[:4]:
            t = str((e or {}).get("title") or "").strip()
            if t:
                titles.append(t)
    except (TypeError, ValueError):
        pass
    return titles


def _skills(profile) -> List[str]:
    return [s.strip() for s in (getattr(profile, "key_skills", "") or "").split(",") if s.strip()]


def suggest(profile, chosen: Iterable[str] = (), *, popular: Iterable[str] = (),
            per_group: int = 6) -> List[dict]:
    """Grouped suggestions for the roles page: [{"key", "label", "items":
    [{"title", "why"}]}]. Never repeats a chosen role or an earlier group's
    title. A user with under three years of experience is not offered
    Senior/Staff/Lead titles from the skill or family groups."""
    chosen = [c for c in chosen if c and c.strip()]
    seen = {_norm(c) for c in chosen}
    try:
        years = int(getattr(profile, "years_experience", 0) or 0)
    except (TypeError, ValueError):
        years = 0
    allow_senior = years >= 3

    def take(items: List[tuple], limit: int) -> List[dict]:
        out = []
        for title, why in items:
            n = _norm(title)
            if not title or n in seen or len(title) > 80:
                continue
            seen.add(n)
            out.append({"title": canonical(title) if title.islower() else title, "why": why})
            if len(out) >= limit:
                break
        return out

    groups: List[dict] = []
    # 1. From the resume: their own titles, then what the skills point to.
    resume_items: List[tuple] = []
    titles = _profile_titles(profile) if profile is not None else []
    for i, t in enumerate(titles):
        resume_items.append((t, "your current title" if i == 0 and getattr(profile, "current_title", "")
                             else "a title on your resume"))
    skills = _skills(profile) if profile is not None else []
    hits: dict = {}
    for s in skills:
        sl = _norm(s)
        # The most specific key wins: "Azure OpenAI" is an AI skill, not a
        # cloud one, though it contains "azure".
        matched = []
        for keys, roles in SKILL_ROLES:
            ks = [k for k in keys if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", sl)]
            if ks:
                matched.append((max(len(k) for k in ks), roles))
        best = max((m[0] for m in matched), default=0)
        for length, roles in matched:
            if length != best:
                continue
            for r in roles:
                hits.setdefault(r, [])
                if s not in hits[r]:
                    hits[r].append(s)
    for r, by in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        if allow_senior or not _SENIOR_RE.search(r):
            resume_items.append((r, "fits your skills: " + ", ".join(by[:3])))
    g = take(resume_items, per_group + 2)
    if g:
        groups.append({"key": "resume", "label": "From your resume", "items": g})
    # 2. Neighbours of what they already picked (or of their title).
    # Round-robin over the chosen roles so each one contributes, and the reason
    # names the role a suggestion is actually close to.
    base = chosen or titles[:1]
    per_role = [[(r, "similar to " + b) for r in
                 related([b], exclude=list(seen), limit=per_group, allow_senior=allow_senior)]
                for b in base]
    rel = [x for tier in zip(*[lst + [None] * (per_group - len(lst)) for lst in per_role])
           for x in tier if x]
    g = take(rel, per_group)
    if g:
        groups.append({"key": "related", "label": "Similar roles" if chosen else "Close to your title",
                       "items": g})
    # 3. Common in their field (the caller's department-aware pool).
    g = take([(p, "") for p in popular if allow_senior or not _SENIOR_RE.search(p)], per_group)
    if g:
        groups.append({"key": "popular", "label": "Popular in your field", "items": g})
    return groups
