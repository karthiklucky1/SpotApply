import re
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from app.db.models import Job
from app.matching.filters.constants import NON_US_LOCATIONS, NO_SPONSORSHIP_PATTERNS, STAFF_TITLES

log = logging.getLogger(__name__)

# Salary targeting: $80k–$150k/yr
_SALARY_TOO_HIGH_MIN = 150_000   # reject if advertised minimum >= this (e.g. "$160k-$200k")
_SALARY_TOO_LOW_MAX  = 80_000    # reject if advertised maximum <= this (e.g. "$50k-$75k")

# Pre-compile the range pattern once
_SALARY_RANGE_RE = re.compile(
    r'\$([\d,]+)\s*(k)?\s*[-–to]+\s*\$([\d,]+)\s*(k)?'
)
_SALARY_SINGLE_RE = re.compile(r'\$([\d,]+)\s*(k)?')
_SALARY_CONTEXT_RE = re.compile(
    r'(?:salary|base pay|compensation|annual pay|pay range|total pay)'
)


def _extract_salary_range(text: str) -> Optional[Tuple[float, float]]:
    """Return (min, max) from an explicit salary range like $80k–$120k.

    Only uses values that form an actual range to avoid picking up
    bonus, equity, or signing-bonus figures as salary anchors.
    Falls back to a single dollar amount near a salary keyword.
    """
    # Primary: explicit range pattern $X–$Y
    for m in _SALARY_RANGE_RE.finditer(text):
        try:
            lo = float(m.group(1).replace(',', '')) * (1000 if m.group(2) else 1)
            hi = float(m.group(3).replace(',', '')) * (1000 if m.group(4) else 1)
            if lo >= 30_000 and hi >= 30_000:
                return lo, hi
        except ValueError:
            pass

    # Fallback: single salary figure within 80 chars of a salary keyword
    for kw in _SALARY_CONTEXT_RE.finditer(text):
        window = text[max(0, kw.start() - 10): kw.start() + 80]
        for m in _SALARY_SINGLE_RE.finditer(window):
            try:
                raw = float(m.group(1).replace(',', '')) * (1000 if m.group(2) else 1)
                if raw >= 30_000:
                    return raw, raw
            except ValueError:
                pass

    return None


@dataclass
class FilterResult:
    passed: bool
    reason: str
    score_override: Optional[int] = None

class RuleFilter:
    def __init__(self):
        pass

    def filter(self, job: Job) -> FilterResult:
        desc_low = job.description.lower()
        title_low = job.title.lower()
        loc_low = (job.location or "").lower()

        # 1. Non-US Location Filter — use word-boundary regex to avoid
        #    "india" matching "indiana" or "uk" matching "duke"
        if loc_low:
            for loc in NON_US_LOCATIONS:
                pattern = rf"\b{re.escape(loc)}\b"
                if re.search(pattern, loc_low):
                    return FilterResult(
                        passed=False,
                        reason=f"Location pre-filtered: job location '{job.location}' matches '{loc}' (outside the US)",
                        score_override=10
                    )
        else:
            # If location is empty, check title for explicit non-US tags
            for loc in NON_US_LOCATIONS:
                pattern = rf"\b{re.escape(loc)}\b"
                if re.search(pattern, title_low):
                    return FilterResult(
                        passed=False,
                        reason=f"Location pre-filtered: title '{job.title}' indicates outside the US ('{loc}')",
                        score_override=10
                    )

        # 2. Work Authorization / Sponsorship Blocker
        for pattern in NO_SPONSORSHIP_PATTERNS:
            if pattern in desc_low:
                return FilterResult(
                    passed=False,
                    reason=f"Sponsorship pre-filtered: matches '{pattern}'",
                    score_override=10
                )

        # 3. Experience Gap Filter — match "N years" then confirm "experience"
        #    within the next 60 chars to catch all common JD phrasings
        for m in re.finditer(r'(\d+)\+?\s*years?', desc_low):
            years = int(m.group(1))
            context = desc_low[m.start(): m.start() + 60]
            if 'experience' in context and years >= 5:
                return FilterResult(
                    passed=False,
                    reason=f"Experience pre-filtered: requires {years}+ years (candidate has 3)",
                    score_override=15
                )

        # 4. Hard titles block — filter every entry in STAFF_TITLES unconditionally
        for t in STAFF_TITLES:
            if title_low.startswith(t) or f" {t}" in title_low:
                return FilterResult(
                    passed=False,
                    reason=f"Title pre-filtered: '{job.title}' is a senior/staff-level role",
                    score_override=15
                )

        # 5. Salary Range Filter — only use real salary ranges, not isolated bonus figures
        sal_range = _extract_salary_range(desc_low)
        if sal_range:
            min_sal, max_sal = sal_range
            if min_sal >= _SALARY_TOO_HIGH_MIN:
                return FilterResult(
                    passed=False,
                    reason=f"Salary too high: starts at ${min_sal:,.0f} (targeting $80k–$150k)",
                    score_override=20
                )
            if max_sal <= _SALARY_TOO_LOW_MAX:
                return FilterResult(
                    passed=False,
                    reason=f"Salary too low: up to ${max_sal:,.0f} (targeting $80k–$150k)",
                    score_override=20
                )

        # 6. Hire-probability filter — block roles the candidate cannot credibly fill
        #    a) Low-level systems / GPU kernel engineering
        systems_signals = [
            "cuda kernel", "gpu kernel", "write cuda", "triton kernel",
            "systems programming", "kernel developer", "kernel engineer",
            "bare metal", "memory allocator", "compiler engineer", "llvm", "mlir",
        ]
        if any(s in desc_low for s in systems_signals):
            return FilterResult(
                passed=False,
                reason="Hire-probability: GPU/kernel/compiler systems role — not in candidate stack",
                score_override=12
            )

        #    b) C++ or Rust listed as a hard requirement (not nice-to-have)
        cpp_rust_required = [
            "c++ required", "proficiency in c++", "strong c++", "expert in c++",
            "rust required", "proficiency in rust", "strong rust", "expert in rust",
            "primary language is c++", "primary language is rust",
        ]
        if any(pat in desc_low for pat in cpp_rust_required):
            return FilterResult(
                passed=False,
                reason="Hire-probability: C++/Rust listed as required — not in candidate stack",
                score_override=12
            )

        #    c) Pure research / PhD roles
        research_signals = [
            "phd required", "phd preferred", "doctoral degree required",
            "publishing research", "publish original research",
            "first-author publication", "neurips", "icml", "iclr publication",
        ]
        if sum(1 for s in research_signals if s in desc_low) >= 2:
            return FilterResult(
                passed=False,
                reason="Hire-probability: pure research role requiring publications/PhD",
                score_override=12
            )

        return FilterResult(passed=True, reason="Passed all rule filters")
