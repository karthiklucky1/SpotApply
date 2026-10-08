"""The owner's resume rules (2026-10-08), as deterministic steps + a checklist.

A friend's recipe that earned interview calls, adopted for every tailored
resume. The model is told all of it (tailor.TAILOR_SYSTEM); what can be made
TRUE without trusting the model is done here, after generation and before the
files are written, and every rule is then checked and reported back to the
user (``build_checklist`` → report.json ``rules_checklist`` → the Tailoring
Studio):

    rewrite + restructure for the job     TAILOR_SYSTEM; the L0 "ship the master"
                                          skip is off (TAILOR_SKIP_COVERAGE_PCT=0)
    email exactly as on the resume        restore_email
    latest company exactly as on resume   prompt + latest_employer check
    natural tone / woven keywords         prompt; Doctor human gate; stuffing check
    strong verbs, real numbers            prompt; fabrication guard on numbers
    nothing invented                      lock layer, fabrication guard, grounding,
                                          strip_unconfirmed (all pre-existing)
    no em dashes                          scrub_em_dashes
    role title from the JD (opt-in)       apply_headline (UserProfile.resume_title_from_jd)
    location                              unchanged: polish_contact_line + profile
    one page, PDF + Word, clean files     app/tailoring/render.py

What this module will NOT do: promise "90+ on any ATS". Third-party ATS scores
are proprietary, and on a job whose skills the resume does not show, the only
way to reach 90 is to claim them — which every honesty gate here exists to
stop. It reports instead how many of the posting's keywords the candidate
GENUINELY has are in the document (``keyword_coverage``), rebuilds once when
that is under 90%, and lists the rest as skills to learn.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

EM_DASH = "—"
HORIZONTAL_BAR = "―"
EN_DASH = "–"

_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DASHES = f"[{EM_DASH}{HORIZONTAL_BAR}]"

# A dash between two ends of a range: "2022 — Mar 2024", "Jan 2022—Present",
# "10—20 engineers". Written as a plain hyphen, which is how every ATS reads it.
_RANGE_RE = re.compile(
    rf"(?<=\d)\s*(?:{_DASHES}|{EN_DASH})\s*"
    rf"(?=(?:{_MONTHS})[a-z]*\.?\s*\d|present\b|current\b|now\b|ongoing\b|today\b)",
    re.IGNORECASE)
_NUMBER_RANGE_RE = re.compile(rf"(?<=\d)\s*(?:{_DASHES}|{EN_DASH})\s*(?=\d)")
# "— Built the …" used as a bullet glyph.
_BULLET_DASH_RE = re.compile(rf"^(\s*){_DASHES}\s+", re.MULTILINE)
# A dash used as punctuation in prose: "fast — and cheap", "fast—and cheap".
_PROSE_DASH_RE = re.compile(rf"\s*{_DASHES}\s*")
# The two stand-ins people type for an em dash: " -- " and a SPACED en dash.
_DOUBLE_HYPHEN_RE = re.compile(r"(?<=\S) -- (?=\S)")
_SPACED_EN_DASH_RE = re.compile(rf"(?<=[^\s\d]) {EN_DASH} (?=\D)")


def scrub_em_dashes(text: str) -> Tuple[str, int]:
    """Remove every em dash (owner rule: "Do NOT use em dashes anywhere").

    Ranges become a hyphen, a dash bullet becomes "- ", prose dashes become a
    comma. Changes punctuation only, never a word, a number or a date — the
    fabrication guard canonicalizes date ranges, so "Jun 2022 — Mar 2024" and
    "Jun 2022 - Mar 2024" are the same fact. Returns (text, replacements)."""
    if not text:
        return text or "", 0
    n = 0
    text, k = _RANGE_RE.subn(" - ", text)
    n += k
    text, k = _NUMBER_RANGE_RE.subn("-", text)
    n += k
    text, k = _BULLET_DASH_RE.subn(lambda m: f"{m.group(1)}- ", text)
    n += k

    def _prose(m: re.Match) -> str:
        return ", "
    out_lines = []
    for line in text.split("\n"):
        line, k = _PROSE_DASH_RE.subn(_prose, line)
        n += k
        line, k = _DOUBLE_HYPHEN_RE.subn(", ", line)
        n += k
        line, k = _SPACED_EN_DASH_RE.subn(", ", line)
        n += k
        if k or ", " in line:
            # Tidy what a replaced dash can leave behind: ", ," / ",." / a
            # trailing comma / a comma right after a heading marker.
            line = re.sub(r",\s*,", ",", line)
            line = re.sub(r",\s*([.;:!?)])", r"\1", line)
            line = re.sub(r"(^\s*#{1,6}\s*), ", r"\1", line)
            line = re.sub(r"\s*,\s*$", "", line)
        out_lines.append(line)
    return "\n".join(out_lines), n


def count_em_dashes(text: str) -> int:
    return len(re.findall(_DASHES, text or ""))


# ── contact ──────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE_RE = re.compile(r"(?<![\d+])\(?\d{3}\)?[\s.\-]*\d{3}\s*[\-.]?\s*\d{4}(?!\d)")
_URL_HINT_RE = re.compile(r"https?://|www\.|linkedin|github|\.com\b|\.io\b|\.dev\b", re.I)
_SECTION_WORDS = re.compile(
    r"^(?:#{1,6}\s*)?(?:professional\s+|technical\s+|work\s+|career\s+)?"
    r"(summary|profile|objective|experience|employment|education|skills|projects|"
    r"certifications?|achievements|publications|leadership|activities)\s*:?\s*$",
    re.IGNORECASE)


def master_email(master: str) -> str:
    """The email the user's own resume gives (the first one, header first)."""
    lines = (master or "").splitlines()
    for ln in lines[:15] + lines[15:]:
        m = _EMAIL_RE.search(ln)
        if m:
            return m.group(0)
    return ""


def _header_end(lines: List[str]) -> int:
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## "):
            return i
    return min(len(lines), 6)


def restore_email(md: str, master: str) -> Tuple[str, str]:
    """Make the resume header carry exactly the email the user's resume gives.

    Returns (md, status): ``kept`` (already right), ``replaced`` (another
    address swapped for it), ``restored`` (it was missing; added to the
    contact line), ``no_master_email`` (the resume has none — nothing added)."""
    want = master_email(master)
    if not want:
        return md, "no_master_email"
    lines = (md or "").split("\n")
    end = _header_end(lines)
    status = "kept"
    found_right = False
    for i in range(end):
        def _swap(m: re.Match) -> str:
            nonlocal status, found_right
            if m.group(0).lower() == want.lower():
                found_right = True
                return m.group(0)
            status = "replaced"
            found_right = True
            return want
        lines[i] = _EMAIL_RE.sub(_swap, lines[i])
    if not found_right:
        target = None
        for i in range(end):
            s = lines[i].strip()
            if s and not s.startswith("#") and (
                    "|" in s or _PHONE_RE.search(s) or _URL_HINT_RE.search(s)):
                target = i
                break
        if target is not None:
            lines[target] = f"{lines[target].rstrip()} | {want}"
        else:
            name_i = next((i for i in range(end) if lines[i].strip().startswith("# ")), None)
            lines.insert((name_i + 1) if name_i is not None else 0, want)
        status = "restored"
    return "\n".join(lines), status


def header_email(md: str) -> str:
    lines = (md or "").split("\n")
    for ln in lines[:_header_end(lines)]:
        m = _EMAIL_RE.search(ln)
        if m:
            return m.group(0)
    return ""


# ── headline (the role shown under the name) ─────────────────────────────────

_LOCATION_LINE_RE = re.compile(
    r"^[A-Za-z .'-]+,\s*(?:[A-Z]{2}|[A-Za-z .'-]+)(?:\s+\d{5})?$")


def _is_contact_or_location(line: str) -> bool:
    s = line.strip()
    return bool(_EMAIL_RE.search(s) or _PHONE_RE.search(s) or _URL_HINT_RE.search(s)
                or _LOCATION_LINE_RE.match(s) or s.lower() in ("remote", "hybrid"))


def _headline_like(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#") or s.startswith(("- ", "* ")):
        return False
    core = s.strip("*_ ").strip()
    if not core or _is_contact_or_location(core) or _SECTION_WORDS.match(core):
        return False
    return len(core.split()) <= 14 and not core.endswith((".", ":"))


def master_headline(master: str) -> str:
    """The title line a resume gives under the name ("Software Engineer"),
    or "" when it has none. Read from the header: lines before the first
    section heading, after the name."""
    lines = (master or "").splitlines()
    seen_name = False
    for ln in lines[:12]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("## ") or _SECTION_WORDS.match(s):
            break
        if not seen_name:
            seen_name = True              # "# Name" or the plain first line
            continue
        if _is_contact_or_location(s):
            continue
        if _headline_like(s):
            return s.strip("*_ ").strip()
        break                             # a paragraph: no headline
    return ""


_TITLE_TAIL_RE = re.compile(
    r"\s+(?:[-|–—/]|@|at)\s+(?=.*(?:remote|hybrid|on-?site|onsite|\d|"
    r"[A-Z][a-z]+,\s*[A-Z]{2}\b|united states|usa\b|\bus\b|req|job id|contract|temporary))",
    re.IGNORECASE)


def sanitize_jd_title(title: str) -> str:
    """A posting's title as a headline: no "(Remote)", no "- Austin, TX", no
    requisition ids, no numbers (the fabrication guard reads every number as a
    claim). "Senior Backend Engineer (Remote) - R12345" → "Senior Backend Engineer"."""
    t = (title or "").strip()
    t = re.sub(r"[\(\[\{][^)\]\}]*[\)\]\}]", " ", t)
    m = _TITLE_TAIL_RE.search(t)
    if m:
        t = t[:m.start()]
    t = t.split(" | ")[0]
    t = re.sub(r"(?:^|\s)#?[A-Za-z]{0,4}[-_]?\d[\w-]*", " ", t)   # R12345, JR-1234, #77, 2
    t = re.sub(r"\s+", " ", t).strip(" -–—,/|:;")
    return t[:60].strip()


def apply_headline(md: str, headline: str) -> str:
    """Set the one plain line directly under "# Name" to ``headline`` (or
    remove a headline the model wrote when ``headline`` is empty). Never
    touches a role line ("### …" / "**Title** | …") — changing a held title
    would be fabrication; this is the title the candidate is applying as."""
    lines = (md or "").split("\n")
    name_i = next((i for i, ln in enumerate(lines) if ln.strip().startswith("# ")), None)
    if name_i is None:
        return md
    end = _header_end(lines)
    for i in range(name_i + 1, max(end, name_i + 1)):
        if _headline_like(lines[i]):
            del lines[i]
            break
    if headline:
        lines.insert(name_i + 1, headline)
    return "\n".join(lines)


def headline_of(md: str) -> str:
    """The headline line the delivered resume shows, if any."""
    lines = (md or "").split("\n")
    name_i = next((i for i, ln in enumerate(lines) if ln.strip().startswith("# ")), None)
    if name_i is None:
        return ""
    for i in range(name_i + 1, _header_end(lines)):
        if _headline_like(lines[i]):
            return lines[i].strip()
    return ""


def prompt_block(master: str, job_title: str, *, title_from_jd: bool) -> str:
    """The per-job facts the model must copy exactly, read from the master."""
    lines = ["", "", "COPY EXACTLY FROM THE MASTER RESUME:"]
    email = master_email(master)
    if email:
        lines.append(f"  - Email: {email} (the only email address on the resume)")
    role = latest_role(master)
    if role:
        lines.append(f"  - Most recent role: {role['title']} at {role['org']}"
                     + (f", {role['dates']}" if role.get("dates") else "")
                     + f". Name the employer exactly \"{role['org']}\".")
    target = sanitize_jd_title(job_title) if title_from_jd else ""
    if target:
        lines.append(f"  - The candidate is applying as a {target}. Write the summary for that "
                     f"role from their real experience, without claiming they held the title "
                     f"{target}; never change a job title in Experience.")
    else:
        lines.append("  - Describe the candidate with the job titles they actually held, as "
                     "the master resume gives them.")
    return "\n".join(lines)


# ── latest employer ──────────────────────────────────────────────────────────

def latest_role(master: str) -> Optional[Dict[str, str]]:
    """The candidate's most recent paid role as the resume writes it."""
    try:
        from app.tailoring.inventory import build_inventory
        inv = build_inventory(master or "")
    except Exception:
        return None
    jobs = [e for e in inv.engagements if e.is_employment and (e.org or "").strip()]
    if not jobs:
        return None
    best = max(jobs, key=lambda e: ((e.end or 0), (e.start or 0)))
    return {"title": best.title.strip(), "org": best.org.strip(),
            "dates": (best.dates_verbatim or "").strip()}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[*_`#|]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def employer_kept(md: str, org: str) -> bool:
    return bool(org) and _norm(org) in _norm(md)


# ── keywords ─────────────────────────────────────────────────────────────────

def keyword_coverage(master: str, md: str, jd: str) -> Dict:
    """How many of the posting's keywords the candidate GENUINELY has (already
    on their resume) made it into this draft, and the overall match.

    ``achievable`` are the posting's top phrases the master resume already
    contains; ``kept`` those also in the draft. ``overall_pct`` is the plain
    share of the posting's phrases in the draft — capped by what the resume
    honestly supports, so it is reported, never forced."""
    try:
        from app.tailoring.ats_keywords import analyze
        m = analyze(jd or "", master or "")
        t = analyze(jd or "", md or "")
    except Exception:
        return {"achievable": 0, "kept": 0, "kept_pct": None, "overall_pct": None,
                "missing_achievable": [], "total": 0}
    achievable = list(dict.fromkeys(m.matched))
    kept = [p for p in achievable if p in set(t.matched)]
    return {
        "achievable": len(achievable),
        "kept": len(kept),
        "kept_pct": round(100 * len(kept) / len(achievable)) if achievable else None,
        "overall_pct": round(100 * (t.coverage_pct or 0)),
        "missing_achievable": [p for p in achievable if p not in set(kept)][:10],
        "total": len(t.top_phrases or []),
    }


def keyword_stuffing(md: str, jd: str) -> List[str]:
    """Lines outside Skills that are just a run of job keywords, and keywords
    repeated past the point a person would. Returns readable problems."""
    problems: List[str] = []
    try:
        from app.tailoring.ats_keywords import extract_jd_phrases
        phrases = [p.lower() for p in extract_jd_phrases(jd or "", top_n=24) if p]
    except Exception:
        return problems
    if not phrases:
        return problems
    low = (md or "").lower()
    for p in phrases:
        n = len(re.findall(rf"(?<!\w){re.escape(p)}(?!\w)", low))
        if n > 3:
            problems.append(f'"{p}" appears {n} times')
    section = ""
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("## "):
            section = s[3:].lower()
            continue
        if "skill" in section or not s:
            continue
        parts = [x.strip().lower() for x in re.split(r"[,|;]", s.lstrip("-*• ")) if x.strip()]
        hits = sum(1 for x in parts if x in phrases)
        if len(parts) >= 4 and hits >= 4 and hits / len(parts) >= 0.6:
            problems.append("a line outside Skills is a list of keywords")
    return problems[:5]


# ── the checklist ────────────────────────────────────────────────────────────

def _row(key: str, label: str, ok: Optional[bool], detail: str = "") -> Dict:
    return {"key": key, "label": label, "ok": ok, "detail": detail}


def build_checklist(*, master: str, md: str, jd: str, rewritten: bool,
                    email_status: str, latest: Optional[Dict[str, str]],
                    human_passed: Optional[bool], fabrications: list,
                    grounding_status: str, headline: str, headline_source: str,
                    location_note: str, filename_docx: str, filename_pdf: str,
                    company: str, metadata_clean: Optional[bool],
                    pages: Optional[int], coverage: Dict,
                    trimmed: List[str]) -> List[Dict]:
    """Every rule, checked against the files actually written. ``ok`` is
    True / False, or None for "could not check" — never a guess."""
    rows: List[Dict] = []
    rows.append(_row("rewrite", "Rewritten and restructured for this job", bool(rewritten),
                     "" if rewritten else "your resume was used as it is"))
    em = header_email(md)
    rows.append(_row(
        "email", "Email exactly as on your resume",
        None if email_status == "no_master_email" else bool(em) and em.lower() == master_email(master).lower(),
        em or "your resume gives no email"))
    if latest and latest.get("org"):
        rows.append(_row("employer", "Latest company named exactly as on your resume",
                         employer_kept(md, latest["org"]), latest["org"]))
    rows.append(_row("tone", "Natural, human tone", human_passed,
                     "" if human_passed is not False else "vary a couple of bullets in your own words"))
    stuffing = keyword_stuffing(md, jd)
    rows.append(_row("keywords_woven", "Job keywords woven into sentences, no keyword lists",
                     not stuffing, "; ".join(stuffing)))
    try:
        from app.tailoring.doctor import ACTION_VERBS
        bullets = [ln.strip()[2:] for ln in (md or "").splitlines()
                   if ln.strip().startswith(("- ", "* "))]
        verb_led = sum(1 for b in bullets if b.split() and
                       b.split()[0].lower().strip("*_.,;") in ACTION_VERBS)
        if bullets:
            rows.append(_row("verbs", "Strong action verbs", verb_led / len(bullets) >= 0.6,
                             f"{verb_led} of {len(bullets)} bullets lead with one"))
    except Exception:
        pass
    truthful = None if grounding_status == "unverified" else (
        not fabrications and grounding_status == "passed")
    rows.append(_row("truthful", "Nothing invented: every fact checked against your resume",
                     truthful, "" if truthful is not None else "the fact check could not run"))
    dashes = count_em_dashes(md)
    rows.append(_row("em_dashes", "No em dashes", dashes == 0,
                     "" if dashes == 0 else f"{dashes} left"))
    if coverage.get("kept_pct") is not None:
        rows.append(_row(
            "keywords", "Job keywords you genuinely have are in your resume",
            coverage["kept_pct"] >= 90,
            f'{coverage["kept"]} of {coverage["achievable"]} ({coverage["kept_pct"]}%); '
            f'overall match with the posting {coverage["overall_pct"]}%, the rest are '
            f'skills your resume doesn\'t show'))
    rows.append(_row("title", "Title under your name", True,
                     f"{headline} (from the job posting)" if headline_source == "job"
                     else f"{headline} (as on your resume)" if headline
                     else "none (your resume has none)"))
    rows.append(_row("location", "Location", True, location_note))
    rows.append(_row("one_page", "Fits on one page", None if pages is None else pages == 1,
                     "" if pages is None else f"{pages} page{'s' if pages != 1 else ''}"
                     + (f"; {len(trimmed)} least relevant bullet(s) left out to fit"
                        if trimmed else "")))
    rows.append(_row("files", "PDF and Word files ready", bool(filename_pdf and filename_docx),
                     ", ".join(f for f in (filename_pdf, filename_docx) if f)))
    stem = filename_docx.rsplit(".", 1)[0] if filename_docx else ""
    named_ok = bool(re.fullmatch(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_Resume", stem)) and (
        not (company or "").strip() or len(stem.split("_")) >= 3)
    rows.append(_row("filename", "File named YourName_Company_Resume", named_ok, stem))
    rows.append(_row("metadata", "No AI or tool traces in the files", metadata_clean,
                     "" if metadata_clean is not None else "could not be checked"))
    return rows


# ── trace scan ───────────────────────────────────────────────────────────────

TRACE_WORDS = ("python-docx", "generated by", "fpdf", "pyfpdf", "pypdf", "reportlab",
               "claude", "anthropic", "openai", "chatgpt", "gpt-4", "spotapply",
               "hirepath", "microsoft macintosh word")


def file_traces(path) -> Optional[List[str]]:
    """Generator/AI traces in a file's METADATA (docx docProps, pdf Info/XMP).
    Never the resume's text: "Claude" can be a real skill on a resume."""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None
    found: List[str] = []
    try:
        if p.suffix.lower() == ".docx":
            import zipfile
            with zipfile.ZipFile(p) as z:
                blob = " ".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist()
                                if n.startswith("docProps/")).lower()
        elif p.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            r = PdfReader(str(p))
            parts = [str(v) for v in (r.metadata or {}).values()]
            raw = p.read_bytes().decode("latin-1", "ignore")
            # Metadata only, wherever it is written: Info-dict style entries
            # and any XMP packet. Not the page text (see the docstring).
            parts += re.findall(r"/(?:Producer|Creator|Author|Title|Subject|Keywords)\s*\(([^)]*)\)", raw)
            parts += re.findall(r"<x:xmpmeta.*?</x:xmpmeta>", raw, re.S)
            blob = " ".join(parts).lower()
        else:
            return None
    except Exception:
        return None
    for w in TRACE_WORDS:
        if w in blob:
            found.append(w)
    return found
