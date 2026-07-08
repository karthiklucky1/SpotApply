"""Resume Doctor — post-tailor quality gate.

Two layers:
1. Fast text analysis (no LLM) — banned words, bullet quality, ATS coverage, integrity.
2. Haiku LLM verdict — cheap 2-sentence hiring signal written by claude-haiku.

Score 0–100. Pass threshold: >= 65.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Banned words from the tailor system prompt ────────────────────────────────
BANNED_WORDS = [
    "leveraged", "synergized", "synergize", "cutting-edge", "harnessing",
    "harness", "kernel-based", "orchestrated seamless", "state-of-the-art",
    "spearheaded", "drove efficiency", "revolutionized", "demonstrated expertise",
    "passionate about", "results-driven", "detail-oriented", "self-starter",
    "go-getter", "thought leader", "proactive", "dynamic", "innovative solution",
    "best-of-breed", "value-add", "deep dive", "move the needle",
]

# ── Strong action verbs (first word of a bullet should be one of these) ───────
ACTION_VERBS = {
    "architected","built","engineered","developed","designed","deployed",
    "implemented","optimized","scaled","automated","reduced","improved",
    "created","established","delivered","managed","led","trained","fine-tuned",
    "migrated","refactored","integrated","launched","shipped","streamlined",
    "monitored","instrumented","accelerated","collaborated","partnered",
    "authored","researched","evaluated","benchmarked","maintained","extended",
}

# ── Metric patterns (number + unit or % or x multiplier) ──────────────────────
_METRIC_RE = re.compile(
    r'(\d[\d,\.]*\s*(%|x\b|k\b|m\b|ms\b|s\b|gb\b|tb\b|\+))'
    r'|(\b\d{1,3}[,\d]*\s*(requests?|users?|records?|queries|jobs?|models?|items?|nodes?|services?))',
    re.IGNORECASE,
)

# ── Ground truth derived from the master resume — must survive tailoring ──────
# Anchors are extracted per check from the user's OWN master resume (employers,
# employment date ranges, degree, institution), never from a hardcoded list, so
# every tenant is verified against their real history.
_ANCHOR_DATE_RE = re.compile(
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}"
    r"\s*[-–—]\s*"
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{4}|present|current|now)",
    re.IGNORECASE,
)
_ANCHOR_DEGREE_RE = re.compile(
    r"\b(?:master|bachelor|doctor)(?:'s)?\s+of\s+[a-z][a-z ]{1,40}[a-z]|\bph\.?d\b|\bm\.?b\.?a\b",
    re.IGNORECASE,
)
_ANCHOR_SCHOOL_RE = re.compile(
    r"(?:[a-z][a-z&.'-]*\s+){0,4}(?:university|college|institute|polytechnic)"
    r"(?:\s+of(?:\s+[a-z][a-z&.'-]*){1,3})?",
    re.IGNORECASE,
)
_ANCHOR_LOCATIONISH_RE = re.compile(r"(,\s*[a-z]{2}\s*$)|\bremote\b|\bhybrid\b|\bonsite\b", re.IGNORECASE)
_MAX_ANCHORS = 14


def _anchor_pattern(text: str) -> str:
    """Turn a literal anchor into a whitespace/dash-tolerant regex."""
    # Normalize every dash variant BEFORE escaping so the tolerance class is
    # inserted exactly once per dash.
    norm = re.sub(r"[-–—]", "-", " ".join(text.split()))
    pat = re.escape(norm)
    return pat.replace(r"\ ", r"\s+").replace(r"\-", r"\s*[-–—]\s*")


def _derive_anchors(master_md: str) -> List[Tuple[str, str]]:
    """Extract (pattern, description) integrity anchors from a master resume:
    employer names + employment date ranges (from '… | Company | Jun 2020 -
    Mar 2022 | …' style experience lines), degree phrases, and institutions.
    Returns [] when nothing parseable is found (integrity check is skipped)."""
    anchors: List[Tuple[str, str]] = []
    seen: set[str] = set()

    def _add(text: str, desc: str) -> None:
        key = " ".join(text.lower().split())
        if key and key not in seen and len(anchors) < _MAX_ANCHORS:
            seen.add(key)
            anchors.append((_anchor_pattern(key), desc))

    for line in master_md.splitlines():
        s = line.strip()
        if not s:
            continue
        date_m = _ANCHOR_DATE_RE.search(s)
        if date_m and ("|" in s):
            _add(date_m.group(0), "employment dates")
            # Pipe-separated experience header: drop the (bold) title segment,
            # the date segment, and location-ish segments — what's left is the
            # employer name.
            segments = [seg.strip().strip("*").strip() for seg in s.split("|")]
            for idx, seg in enumerate(segments):
                if idx == 0 or not seg:
                    continue
                if _ANCHOR_DATE_RE.search(seg) or _ANCHOR_LOCATIONISH_RE.search(seg):
                    continue
                if len(seg) > 60:
                    continue
                _add(seg, f"employer name '{seg}'")

    for m in _ANCHOR_DEGREE_RE.finditer(master_md):
        _add(m.group(0), "degree name")
    for m in _ANCHOR_SCHOOL_RE.finditer(master_md):
        # Require a real name, not a bare keyword ("university" alone).
        if len(m.group(0).split()) >= 2:
            _add(m.group(0), "education institution")

    return anchors


@dataclass
class DoctorReport:
    passed: bool
    score: int                              # 0–100
    ats_coverage_pct: float                 # % of top JD keywords found in resume
    banned_found: List[str] = field(default_factory=list)
    weak_bullets: List[str] = field(default_factory=list)   # missing verb or metric
    integrity_issues: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)         # human-readable summary
    llm_verdict: Optional[str] = None      # Haiku 2-sentence hiring signal

    def summary(self) -> str:
        lines = [f"Doctor Score: {self.score}/100 | ATS Coverage: {self.ats_coverage_pct:.0%} | {'PASS ✅' if self.passed else 'FAIL ❌'}"]
        for issue in self.issues:
            lines.append(f"  ⚠ {issue}")
        if self.llm_verdict:
            lines.append(f"  Verdict: {self.llm_verdict}")
        return "\n".join(lines)


class ResumeDoctor:
    PASS_THRESHOLD = 65

    def check(self, tailored_md: str, master_md: str, jd_text: str) -> DoctorReport:
        issues: List[str] = []

        # ── 1. Banned word scan ───────────────────────────────────────────────
        text_lower = tailored_md.lower()
        banned_found = [w for w in BANNED_WORDS if w in text_lower]
        if banned_found:
            issues.append(f"Banned words found: {', '.join(banned_found)}")

        # ── 2. Bullet quality ─────────────────────────────────────────────────
        bullets = self._extract_bullets(tailored_md)
        weak: List[str] = []
        for b in bullets:
            first_word = b.split()[0].lower().rstrip(".,;") if b.split() else ""
            has_verb   = first_word in ACTION_VERBS
            has_metric = bool(_METRIC_RE.search(b))
            if not has_verb or not has_metric:
                weak.append(b[:80])
        if weak:
            issues.append(f"{len(weak)}/{len(bullets)} bullets missing action verb or metric")

        # ── 3. ATS keyword coverage ───────────────────────────────────────────
        top_keywords = self._extract_jd_keywords(jd_text)
        hits = sum(1 for kw in top_keywords if kw.lower() in text_lower)
        coverage = hits / len(top_keywords) if top_keywords else 1.0
        if coverage < 0.5:
            issues.append(f"Low ATS coverage: only {coverage:.0%} of JD keywords in resume")

        # ── 4. Integrity check — anchors come from THIS user's master resume ──
        integrity_issues: List[str] = []
        for pattern, desc in _derive_anchors(master_md):
            if not re.search(pattern, text_lower, re.IGNORECASE):
                integrity_issues.append(f"Missing or altered: {desc}")
        if integrity_issues:
            issues.extend(integrity_issues)

        # ── Score ─────────────────────────────────────────────────────────────
        score = self._compute_score(banned_found, weak, bullets, coverage, integrity_issues)
        passed = score >= self.PASS_THRESHOLD

        # ── Haiku LLM verdict (only on passing resumes — no point verdicting failures) ──
        verdict = None
        if passed:
            verdict = self._llm_verdict(tailored_md, jd_text)

        report = DoctorReport(
            passed=passed,
            score=score,
            ats_coverage_pct=coverage,
            banned_found=banned_found,
            weak_bullets=weak,
            integrity_issues=integrity_issues,
            issues=issues,
            llm_verdict=verdict,
        )
        log.info("ResumeDoctor: %s", report.summary())
        return report

    def _llm_verdict(self, tailored_md: str, jd_text: str) -> Optional[str]:
        """Ask claude-haiku for a blunt 2-sentence hiring signal. Returns None on failure."""
        from app.config import settings
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=settings.anthropic_api_key)
        except Exception:
            return None

        prompt = f"""You are a cynical technical recruiter doing a 30-second resume screen.

JOB DESCRIPTION (first 1500 chars):
{jd_text[:1500]}

TAILORED RESUME:
{tailored_md[:3000]}

Write exactly 2 sentences:
1. Would this resume pass an ATS keyword screen and a quick human review for this role? (yes/borderline/no + one reason)
2. The single biggest risk that could get it rejected.

Be blunt. No fluff."""

        try:
            resp = client.messages.create(
                model=settings.doctor_model,
                max_tokens=120,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            log.warning("Doctor LLM verdict failed: %s", e)
            return None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _extract_bullets(self, md: str) -> List[str]:
        bullets = []
        in_section = False
        for line in md.splitlines():
            s = line.strip()
            if s.startswith("## ") or s.startswith("# "):
                header = s.upper()
                in_section = any(k in header for k in
                                 ("EXPERIENCE", "EMPLOYMENT", "PROJECT", "WORK"))
            if in_section and (s.startswith("- ") or s.startswith("* ")):
                cleaned = re.sub(r"\*+", "", s[2:]).strip()
                if len(cleaned) > 20:
                    bullets.append(cleaned)
        return bullets

    def _extract_jd_keywords(self, jd: str, top_n: int = 25) -> List[str]:
        """Pull the most-repeated meaningful tokens from the JD."""
        stop = {
            "the","and","for","with","you","our","are","this","that","will",
            "have","from","your","not","be","we","as","an","is","in","of",
            "to","a","at","or","by","on","it","we're","who","all","able",
            "their","they","but","can","has","been","more","than","into",
            "within","across","each","its","about","what","such","any",
        }
        words = re.findall(r"[a-zA-Z][a-zA-Z+#\-\.]{2,}", jd.lower())
        freq: dict[str, int] = {}
        for w in words:
            if w not in stop and len(w) > 2:
                freq[w] = freq.get(w, 0) + 1
        # Boost multi-word tech terms present in JD
        tech_terms = [
            "python","pytorch","tensorflow","fastapi","kubernetes","docker",
            "langchain","llm","rag","faiss","mlflow","vertex ai","bigquery",
            "kafka","airflow","postgresql","mongodb","openai","claude","gcp",
            "aws","scikit-learn","pyspark","spark","transformer","embedding",
            "fine-tuning","inference","recommendation","retrieval","multi-agent",
        ]
        boosted = {t for t in tech_terms if t in jd.lower()}
        ranked = sorted(freq.items(), key=lambda x: -x[1])
        top = [w for w, _ in ranked[:top_n]]
        # Always include boosted tech terms
        for t in boosted:
            if t not in top:
                top.append(t)
        return top[:top_n + len(boosted)]

    def _compute_score(
        self,
        banned: List[str],
        weak: List[str],
        all_bullets: List[str],
        coverage: float,
        integrity: List[str],
    ) -> int:
        # ATS coverage: 40 pts
        ats_pts = int(coverage * 40)

        # Bullet quality: 30 pts
        total = len(all_bullets) or 1
        good  = total - len(weak)
        bullet_pts = int((good / total) * 30)

        # No banned words: 20 pts (-5 per word found, floor 0)
        banned_pts = max(0, 20 - len(banned) * 5)

        # Integrity: 10 pts (-5 per missing anchor)
        integrity_pts = max(0, 10 - len(integrity) * 5)

        return min(100, ats_pts + bullet_pts + banned_pts + integrity_pts)
