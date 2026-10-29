"""Every skill the tailored resume LISTS must be one the master resume shows.

Grounding fact-checks bullets and summary sentences; the fabrication guard
checks employers, titles, dates, degrees, credentials and numbers. The Skills
section was neither: it is a list, not a claim-bearing sentence, so an item the
model added there reached the employer unchecked. ``requirements.review`` caught
the skills the posting itself asks for, but only the first six it found missing
(``unevidenced[:6]``), and never a word that is not a requirement at all — a
term from the ATS block's ranking, or one a posting planted for AI tools.

The fix only REMOVES. An item is kept when the master names it (any spelling the
skill matcher accepts, its singular/plural, a known alias or acronym, or all of
its words); anything else comes out of the list, and the caller reports it as
removed. A missing keyword costs a search hit; an invented one costs the
interview.

Deterministic, no model.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# One skill, several common spellings. Small on purpose: a pair missing here
# costs one listed keyword (the bullets keep the real term), a wrong pair here
# lets an unearned skill through.
_ALIAS_GROUPS: Tuple[Tuple[str, ...], ...] = (
    ("postgresql", "postgres", "psql"),
    ("javascript", "js", "ecmascript"),
    ("typescript", "ts"),
    ("kubernetes", "k8s"),
    ("golang", "go"),
    ("node.js", "nodejs", "node"),
    ("react", "react.js", "reactjs"),
    ("vue", "vue.js", "vuejs"),
    ("next.js", "nextjs"),
    ("express", "express.js", "expressjs"),
    ("c#", "csharp"),
    ("c++", "cpp"),
    ("gcp", "google cloud", "google cloud platform"),
    ("aws", "amazon web services"),
    ("azure", "microsoft azure"),
    ("ci/cd", "cicd", "continuous integration", "continuous delivery",
     "continuous deployment"),
    ("machine learning", "ml"),
    ("artificial intelligence", "ai"),
    ("natural language processing", "nlp"),
    ("large language models", "large language model", "llm", "llms"),
    ("rest", "restful", "rest api", "rest apis", "restful api", "restful apis"),
    ("sql server", "mssql", "microsoft sql server"),
    ("excel", "microsoft excel", "ms excel"),
    ("power bi", "powerbi"),
    ("scikit-learn", "sklearn", "scikit learn"),
    ("pytorch", "torch"),
    ("amazon s3", "s3"),
    ("ec2", "amazon ec2"),
)
_ALIASES = {name: group for group in _ALIAS_GROUPS for name in group}

_FILLER = frozenset({"and", "or", "of", "in", "the", "a", "an", "for", "with", "to", "on",
                     "basic", "basics", "familiar", "familiarity", "proficient", "advanced",
                     "intermediate", "beginner", "working", "knowledge", "exposure",
                     "experience", "skills", "tools", "etc"})
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
_SKILLS_HEADING_RE = re.compile(
    r"\b(?:skills?|technolog|tools?|competenc|stack|expertise|languages|proficienc)", re.I)
_LABEL_RE = re.compile(r"^(\s*(?:[-*•]\s+)?(?:\*\*)?[A-Za-z][A-Za-z /&+#.()-]{1,40}?(?:\*\*)?\s*:"
                       r"\s*(?:\*\*)?\s*)")
_SUFFIXES = ("", "s", "es", "ful", "ing", "ed", "al", "ment", "ments")


def _norm_item(item: str) -> str:
    return re.sub(r"\s+", " ", (item or "").strip(" .*`_")).lower()


def _split_items(body: str) -> List[str]:
    """Split a skills line on , ; | • — but never inside brackets, so
    "Machine Learning (scikit-learn, PyTorch)" stays one item."""
    items, cur, depth = [], [], 0
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if depth == 0 and ch in ",;|•":
            items.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    items.append("".join(cur))
    return [i for i in items if i.strip()]


def skills_section_items(md: str) -> List[str]:
    """The items the Skills section(s) of a markdown resume list, in order."""
    out: List[str] = []
    in_skills = False
    for line in (md or "").splitlines():
        h = _HEADING_RE.match(line)
        if h:
            in_skills = bool(_SKILLS_HEADING_RE.search(h.group(1)))
            continue
        if not in_skills or not line.strip():
            continue
        body = _LABEL_RE.sub("", line, count=1).strip().lstrip("-*•").strip()
        for item in _split_items(body):
            if _norm_item(item) and _norm_item(item) not in (_norm_item(o) for o in out):
                out.append(item.strip())
    return out


def _master_words(master: str) -> frozenset:
    return frozenset(re.findall(r"[a-z0-9+#]+", (master or "").lower()))


def _word_present(word: str, words: frozenset) -> bool:
    if word in words:
        return True
    stem = word[:-2] if word.endswith("es") and len(word) > 4 else (
        word[:-1] if word.endswith("s") and len(word) > 3 else word)
    return any(stem + suf in words for suf in _SUFFIXES) if len(stem) >= 3 else False


def _name_supported(name: str, master: str, words: frozenset) -> bool:
    from app.tailoring.inventory import ACRONYMS, _skill_pattern
    from app.tailoring.requirements import _number_forms
    name = _norm_item(name)
    content = [w for w in re.findall(r"[a-z0-9+#]+", name) if w not in _FILLER and not w.isdigit()]
    if not content:
        return True           # "basic", "5 years": a qualifier, not a skill
    forms = set(_number_forms(name)) | set(_ALIASES.get(name, ()))
    exp = ACRONYMS.get(name)
    if exp:
        forms.add(exp.lower())
    forms |= {acr for acr, full in ACRONYMS.items() if full.lower() == name}
    if any(_skill_pattern(f).search(master or "") for f in forms if f):
        return True
    return all(_word_present(w, words) for w in content)


_PAREN_RE = re.compile(r"\(([^()]*)\)")


def _repair(item: str, master: str, words: frozenset) -> Tuple[Optional[str], List[str]]:
    """(the item as the master supports it, what came out of it): unchanged,
    trimmed of unsupported bracketed sub-items ("ML (PyTorch, Jax)" ->
    "ML (PyTorch)"), or None when the item itself is not on the master."""
    item = (item or "").strip()
    head = _PAREN_RE.sub(" ", item).strip()
    inner = [p.strip() for m in _PAREN_RE.finditer(item) for p in _split_items(m.group(1))]
    if head and not _name_supported(head, master, words):
        return None, [item]
    kept = [p for p in inner if _name_supported(p, master, words)]
    dropped = [p for p in inner if p not in kept]
    if not dropped:
        return item, []
    if not head:
        return None, [item]
    return (f"{head} ({', '.join(kept)})" if kept else head), dropped


def item_supported(item: str, master: str, words: frozenset = None) -> bool:
    words = words if words is not None else _master_words(master)
    return not _repair(item, master, words)[1]


def unsupported_skill_items(master_md: str, tailored_md: str) -> List[str]:
    """Items the tailored Skills section lists that the master never shows
    (wholly, or in one of their bracketed sub-items)."""
    words = _master_words(master_md)
    return [i for i in skills_section_items(tailored_md)
            if not item_supported(i, master_md, words)]


def prune_skills(master_md: str, md: str) -> Tuple[str, List[str]]:
    """Take what the master does not show out of the Skills section lines (only
    there). A line left with nothing but its label is dropped. Returns
    (markdown, the items or sub-items removed)."""
    words = _master_words(master_md)
    removed: List[str] = []
    out: List[str] = []
    in_skills = False
    for line in (md or "").splitlines():
        h = _HEADING_RE.match(line)
        if h:
            in_skills = bool(_SKILLS_HEADING_RE.search(h.group(1)))
            out.append(line)
            continue
        if not in_skills or not line.strip():
            out.append(line)
            continue
        m = _LABEL_RE.match(line)
        label = m.group(1) if m else ""
        rest = line[len(label):]
        lead = "" if label else re.match(r"^\s*(?:[-*•]\s+)?", rest).group(0)
        body = rest[len(lead):]
        sep_m = re.search(r"\s*[,;|•]\s*", body)
        sep = sep_m.group(0).strip() if sep_m else ","
        sep = ", " if sep == "," else f" {sep} "
        parts = _split_items(body)
        kept, changed = [], False
        for p in parts:
            fixed, dropped = _repair(p, master_md, words)
            if dropped:
                changed = True
                removed.extend(dropped)
            if fixed:
                kept.append(fixed)
        if not changed:
            out.append(line)
        elif kept:
            out.append(f"{label}{lead}{sep.join(kept)}")
        # else: every item went; the label alone says nothing, so the line goes
    return "\n".join(out), removed
