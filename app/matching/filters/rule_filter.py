import re
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from app.common.geo import detect_country, norm_country
from app.db.models import Job
from app.matching.filters.constants import STAFF_TITLES, find_refusal

log = logging.getLogger(__name__)

# Salary targeting: $80k–$150k/yr
_SALARY_TOO_HIGH_MIN = 150_000   # reject if advertised minimum >= this (e.g. "$160k-$200k")
_SALARY_TOO_LOW_MAX  = 80_000    # reject if advertised maximum <= this (e.g. "$50k-$75k")

# Pre-compile the range pattern once. $, £ and € are all accepted so non-US
# users get salary filtering too — a user's profile band is in their local
# currency, and jobs in their country advertise in that same currency.
_SALARY_RANGE_RE = re.compile(
    r'([$£€₹])([\d,]+)\s*(k)?\s*[-–to]+\s*[$£€₹]([\d,]+)\s*(k)?',
    re.IGNORECASE,
)
_SALARY_SINGLE_RE = re.compile(r'([$£€₹])([\d,]+)\s*(k)?')
# Currency implied by the salary symbol. The gate below only fires when this
# matches the USER's salary_currency — comparing an Indian user's ₹15,00,000
# floor against a "$120k" posting rejected every cross-currency job as
# "salary too low". Mismatched/unknown currency → skip the gate (keep the job).
_SYMBOL_CCY = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR"}
_CCY_SYMBOL = {v: k for k, v in _SYMBOL_CCY.items()}
_SALARY_CONTEXT_RE = re.compile(
    r'(?:salary|base pay|compensation|annual pay|pay range|total pay)'
)


def _country_mention_re(country: str) -> str:
    """Regex matching an explicit mention of `country` in a location string.

    Used to rescue a remote posting that lists several regions ("Remote —
    US / EU"): the first detected country may be foreign while the user's own
    is also named, and that job is still open to them.
    """
    c = (country or "").strip().lower()
    aliases = {
        "united states": r"\b(u\.?s\.?a?\.?|united states|usa|us[- ]based|americas?)\b",
        "united kingdom": r"\b(u\.?k\.?|united kingdom|england|britain)\b",
        "canada": r"\bcanada\b",
        "india": r"\bindia\b",
        "germany": r"\bgermany|deutschland\b",
    }
    return aliases.get(c, r"\b" + re.escape(c) + r"\b")


def _extract_salary_range(text: str) -> Optional[Tuple[float, float, str]]:
    """Return (min, max, currency) from an explicit salary range like
    $80k–$120k, £60k–£80k, or ₹20,00,000–₹30,00,000.

    Only uses values that form an actual range to avoid picking up
    bonus, equity, or signing-bonus figures as salary anchors.
    Falls back to a single amount near a salary keyword.
    """
    # Primary: explicit range pattern $X–$Y
    for m in _SALARY_RANGE_RE.finditer(text):
        try:
            lo = float(m.group(2).replace(',', '')) * (1000 if m.group(3) else 1)
            hi = float(m.group(4).replace(',', '')) * (1000 if m.group(5) else 1)
            if lo >= 30_000 and hi >= 30_000:
                return lo, hi, _SYMBOL_CCY.get(m.group(1), "USD")
        except ValueError:
            pass

    # Fallback: single salary figure within 80 chars of a salary keyword
    for kw in _SALARY_CONTEXT_RE.finditer(text):
        window = text[max(0, kw.start() - 10): kw.start() + 80]
        for m in _SALARY_SINGLE_RE.finditer(window):
            try:
                raw = float(m.group(2).replace(',', '')) * (1000 if m.group(3) else 1)
                if raw >= 30_000:
                    return raw, raw, _SYMBOL_CCY.get(m.group(1), "USD")
            except ValueError:
                pass

    return None


@dataclass
class FilterResult:
    passed: bool
    reason: str
    score_override: Optional[int] = None


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_INTERNSHIP_SIGNALS = (
    "intern", "internship", "co-op", "co op", "coop", "summer analyst",
    "industrial placement", "working student", "praktikum",
)


def classify_job_type(title: str, description: str = "") -> str:
    """Return 'internship' or 'full_time' from the title/description text.
    Title is weighted strongly; description only confirms when the title is
    ambiguous (so a full-time JD that merely mentions an internship program
    isn't misclassified)."""
    t = (title or "").lower()
    if any(s in t for s in _INTERNSHIP_SIGNALS):
        return "internship"
    d = (description or "").lower()[:600]   # only the opening lines
    if any(s in d for s in ("intern position", "internship position", "this internship",
                            "summer intern", "co-op position", "is an internship")):
        return "internship"
    return "full_time"


# ── Job kinds (audit 2026-09-25, finding 10) ─────────────────────────────────
# "Engineer" in a title does not make a posting a job. Gig work training AI
# models ("AI Trainer — Software Engineers", data annotation, RLHF raters) is
# piece-rate freelance work sold through aggregators; staffing agencies and
# fixed-term contracts are real jobs of a different kind. Classified
# explicitly so the gate can act on the kind, and so the kind is recorded
# rather than guessed downstream. Order matters: internship first (it has its
# own preference), then gig, then contract, then agency.
# Unambiguous on their own: the job IS rating/labelling for a model.
_GIG_AI_TRAINING_RE = re.compile(
    r"\b(?:ai|llm|model)\s+(?:trainer|training\s+(?:specialist|contributor|expert))\b"
    r"|\bdata\s+annotat\w*|\bannotator\b|\brlhf\s+(?:rater|annotator|contributor|specialist)\b"
    r"|\bevaluat\w+\s+(?:ai|model|chatbot)[-\s]generated\b|\bdataannotation\.tech\b",
    re.I)
# Ambiguous alone ("train AI models" is also what an ML engineer does); counts
# only beside gig-work terms.
_GIG_TRAIN_RE = re.compile(r"\b(?:help\s+)?train\s+(?:ai|llm|generative\s+ai)\s+models?\b", re.I)
_GIG_TERMS_RE = re.compile(
    r"\b(?:freelance|independent\s+contractor|contributor|flexible\s+hours|per\s+hour|"
    r"project[-\s]based|work\s+from\s+anywhere\s+on\s+your\s+own\s+schedule|"
    r"outlier|remotasks|dataannotation|alignerr|mercor)\b", re.I)
_CONTRACT_RE = re.compile(
    r"\b(?:contract(?:\s+position|\s+role|\s*-\s*to\s*-\s*hire|\s+to\s+hire)|fixed[-\s]term|"
    r"temporary\s+(?:position|role|assignment)|\d+\s*(?:-|to)?\s*\d*\s*months?\s+contract|c2c|corp[-\s]to[-\s]corp|w2\s+contract)\b",
    re.I)
_AGENCY_RE = re.compile(
    r"\b(?:our\s+client|on\s+behalf\s+of\s+(?:our|a)\s+client|staffing\s+(?:agency|firm)|"
    r"recruitment\s+agency|we\s+are\s+(?:a\s+)?(?:staffing|recruiting)\s+(?:firm|agency))\b",
    re.I)


def classify_job_kind(title: str, description: str = "") -> str:
    """'internship' | 'gig_ai_training' | 'contract' | 'agency' | 'permanent'.

    Deterministic, text-only. 'permanent' is the default and means "nothing
    says otherwise", not a verified employment type."""
    if classify_job_type(title, description) == "internship":
        return "internship"
    head = f"{title or ''}\n{(description or '')[:3000]}"
    if _GIG_AI_TRAINING_RE.search(head) or (_GIG_TRAIN_RE.search(head) and _GIG_TERMS_RE.search(head)):
        return "gig_ai_training"
    if _CONTRACT_RE.search(head):
        return "contract"
    if _AGENCY_RE.search(head):
        return "agency"
    return "permanent"


# ── Citizenship / clearance (confirmed status only) ─────────────────────────
# `NO_SPONSORSHIP_HARD` only blocks users who NEED sponsorship. A permanent
# resident needs none and still cannot take a citizens-only or clearance role,
# which went through to paid scoring. Only an explicit citizenship or
# clearance requirement counts; "citizen OR permanent resident" does not.
_CITIZEN_ONLY_RE = re.compile(
    r"\b(?:must\s+be\s+(?:a\s+)?u\.?s\.?\s+citizen(?!\s+or\b)|u\.?s\.?\s+citizenship\s+(?:is\s+)?required"
    r"|(?:active\s+)?(?:secret|top\s+secret|ts/sci|security)\s+clearance\s+(?:is\s+)?required"
    r"|must\s+(?:hold|possess|have)\s+an?\s+active\s+(?:secret|top\s+secret|ts/sci|security)\s+clearance)",
    re.I)


def requires_citizenship(description: str) -> Optional[str]:
    """The sentence stating a citizenship/clearance requirement, or None."""
    text = description or ""
    m = _CITIZEN_ONLY_RE.search(text)
    if not m:
        return None
    lo = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
    hi_c = [i for i in (text.find(".", m.end()), text.find("\n", m.end())) if i != -1]
    sentence = " ".join(text[lo:(min(hi_c) if hi_c else len(text))].split())
    # "not required", "no clearance required" and similar negations.
    if re.search(r"\b(?:not|no|nor)\b[^.]{0,20}(?:citizen|clearance)", sentence, re.I) and \
            not re.search(r"\bmust\b", sentence, re.I):
        return None
    return sentence[:200]


def _is_confirmed_citizen(profile) -> Optional[bool]:
    """True / False from the saved status; None when the profile says nothing."""
    blob = " ".join(str(getattr(profile, f, "") or "") for f in
                    ("work_authorization", "visa_status")).lower()
    if not blob.strip():
        return None
    return "citizen" in blob and "non-citizen" not in blob and "not a citizen" not in blob


# ── Required degree level (confirmed degree only) ───────────────────────────
_DEGREE_LEVELS = (("phd", 3), ("doctor", 3), ("master", 2), ("m.s", 2), ("msc", 2), ("mba", 2),
                  ("m.eng", 2), ("bachelor", 1), ("b.s", 1), ("bsc", 1), ("b.tech", 1),
                  ("b.e.", 1), ("b.a", 1), ("associate", 0))
_DEGREE_REQ_RE = re.compile(
    r"\b(?P<deg>ph\.?d|doctora(?:te|l)|master'?s?|bachelor'?s?)\b[^.\n]{0,60}?\b(?:is\s+)?required\b"
    r"|\brequires?\s+(?:a|an)\s+(?P<deg2>ph\.?d|doctora(?:te|l)|master'?s?|bachelor'?s?)\b",
    re.I)


def _degree_level(text: str) -> Optional[int]:
    t = (text or "").lower()
    best = None
    for key, lvl in _DEGREE_LEVELS:
        if key in t:
            best = lvl if best is None else max(best, lvl)
    return best


def required_degree_level(description: str) -> Optional[Tuple[int, str]]:
    """(level, sentence) for an explicit REQUIRED degree, else None. A sentence
    that also accepts equivalent experience is not a requirement."""
    text = description or ""
    for m in _DEGREE_REQ_RE.finditer(text):
        lo = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
        hi_c = [i for i in (text.find(".", m.end()), text.find("\n", m.end())) if i != -1]
        sentence = " ".join(text[lo:(min(hi_c) if hi_c else len(text))].split())
        if re.search(r"\bor\s+(?:equivalent|comparable|related)\b|\bequivalent\s+(?:practical\s+)?experience\b"
                     r"|\bpreferred\b|\bnice\s+to\s+have\b", sentence, re.I):
            continue
        lvl = _degree_level(m.group("deg") or m.group("deg2") or "")
        if lvl is not None:
            return lvl, sentence[:200]
    return None


class RuleFilter:
    """Rule-based pre-filter.

    Per-user when given a ``profile`` (years of experience, salary band, skills,
    sponsorship need drive the thresholds); falls back to the original
    single-user defaults when no profile is supplied, so existing callers and
    local/dev runs behave exactly as before.
    """

    def __init__(self, profile=None):
        self.profile = profile
        legacy = profile is None

        # Candidate experience → drives the "requires N+ years" gap filter and
        # whether senior/staff titles are filtered out.
        # Legacy (no profile) keeps the original 3-year default so the experience
        # gate still runs. A real profile reporting 0 years (student / new grad)
        # means "unknown" → the experience gate is skipped (see filter()).
        self.cand_years = _safe_int(getattr(profile, "years_experience", None), 3 if legacy else 0)
        self.block_senior_titles = legacy or self.cand_years < 6

        # Salary band: only filter on a bound the user actually expressed. With
        # no profile we keep the original $80k–$150k targeting band.
        smin = _safe_int(getattr(profile, "salary_min", None), 0)
        smax = _safe_int(getattr(profile, "salary_max", None), 0)
        self.salary_floor = smin if smin > 0 else (_SALARY_TOO_LOW_MAX if legacy else None)
        self.salary_ceiling = smax if smax > 0 else (_SALARY_TOO_HIGH_MIN if legacy else None)
        # The currency the user's band is expressed in — the salary gate only
        # fires when the posting advertises in the SAME currency.
        self.salary_currency = (
            (getattr(profile, "salary_currency", "") or "USD") if not legacy else "USD"
        ).strip().upper()

        # Only block jobs that refuse sponsorship when the user needs it. A
        # citizen / green-card holder should NOT lose "must be US citizen" roles.
        self.requires_sponsorship = True if legacy else bool(getattr(profile, "requires_sponsorship", False))

        # Country the user wants jobs in — onsite roles in a DIFFERENT country are
        # filtered out. Legacy (no profile) keeps the original US-targeting default.
        # A profile with an EMPTY country means "not chosen yet" → no country gate
        # (assuming the US for a user in Berlin hides every job in their own city).
        self.preferred_country = norm_country(
            (getattr(profile, "preferred_country", "") or "") if not legacy else "United States"
        )

        # Skills the user actually has → don't reject roles that need them.
        self.user_skills = (getattr(profile, "key_skills", "") or "").lower()
        self.user_degree = (getattr(profile, "degree", "") or "").lower()

        # Job-type preference: "full_time" | "internship" | "both". Only enforced
        # when a profile is present (legacy single-user runs are unfiltered).
        self.job_type_pref = None if legacy else (getattr(profile, "job_type_preference", "full_time") or "full_time")
        # Internships are surfaced ONLY when the user explicitly opts in via the
        # discovery toggle (or an "internship" job-type preference). "both" or an
        # unset preference does NOT silently pull in internships.
        self.enforce_job_type = not legacy
        self.include_internships = False if legacy else bool(
            getattr(profile, "include_internships_in_discovery", False))

    def _has_skill(self, *needles: str) -> bool:
        return any(n in self.user_skills for n in needles)

    def filter(self, job: Job) -> FilterResult:
        desc_low = job.description.lower()
        title_low = job.title.lower()
        loc_low = (job.location or "").lower()

        # 0. Job-type preference (students: internship vs full-time).
        if self.enforce_job_type:
            jtype = classify_job_type(job.title, job.description)
            internships_wanted = self.include_internships or self.job_type_pref == "internship"
            # Internships only appear when the user opted in — fixes internships
            # leaking in for users who never asked for them.
            if jtype == "internship" and not internships_wanted:
                return FilterResult(
                    passed=False,
                    reason="Internship filtered: user did not opt into internships",
                    score_override=10,
                )
            # An internship-only seeker shouldn't get full-time roles.
            if self.job_type_pref == "internship" and jtype != "internship":
                return FilterResult(
                    passed=False,
                    reason="Full-time filtered: user wants internships only",
                    score_override=10,
                )

        # 0b. Job kind: AI-training gig work is not the job a tech seeker asked
        #     for (audit 2026-09-25, finding 10). Only when a profile exists and
        #     its target roles do not themselves ask for such work.
        if self.enforce_job_type:
            kind = classify_job_kind(job.title, job.description)
            wanted_roles = (getattr(self.profile, "target_roles", "") or "").lower()
            if kind == "gig_ai_training" and not any(
                    w in wanted_roles for w in ("annotat", "ai trainer", "rlhf", "data label")):
                return FilterResult(
                    passed=False,
                    reason="Job kind filtered: AI-training gig work, not an employer posting",
                    score_override=10,
                )

        # 1. Country Filter — onsite roles in a different country than the user's
        #    preferred one are dropped. Skipped entirely for remote jobs since
        #    "US/Canada Remote" or "Remote (EU)" are still valid remote roles, and
        #    ambiguous/unknown locations are KEPT rather than over-filtered.
        # If location is empty, fall back to the title for explicit country tags.
        #
        # A row that carries a LOCATION VERDICT (`Job.eligibility`, decided once
        # from the posting's shared geography — app/common/eligibility.py) is
        # not re-judged by this string comparison: the verdict read every site
        # untruncated and the user's full preferences, and this check reads
        # one display string whole. "Remote · Tiranë, Albania · Austin, TX" is
        # ELIGIBLE for a US user by the verdict and Albanian to this regex; the
        # verdict wins. INELIGIBLE never normally reaches here (it is stamped
        # out upstream) but is honoured as a backstop. NULL = a row from before
        # the verdict existed: the legacy string gate, unchanged.
        _verdict = getattr(job, "eligibility", None)
        if _verdict == "ineligible":
            return FilterResult(
                passed=False,
                reason=f"Location filtered: {getattr(job, 'eligibility_reason', None) or 'ineligible location'}",
                score_override=10,
            )
        haystack = loc_low if loc_low else title_low
        detected = detect_country(haystack)
        if _verdict:
            pass                                   # decided by the geography verdict
        elif not job.remote:
            if self.preferred_country and detected and detected != self.preferred_country:
                return FilterResult(
                    passed=False,
                    reason=(
                        f"Location pre-filtered: '{job.location or job.title}' is in "
                        f"{detected.title()}, user targets {self.preferred_country.title()}"
                    ),
                    score_override=10
                )
        elif self.preferred_country and detected and detected != self.preferred_country:
            # REMOTE, but the posting names a specific OTHER country. "Remote"
            # was skipping the country gate entirely, which is how a US-targeting
            # user got "Remote · Europe, €80,000" roles they cannot take. Only an
            # EXPLICIT foreign country drops the job: a bare "Remote", or one
            # that also mentions the user's own country ("Remote — US/EU"), is
            # kept, so genuinely open roles still come through.
            mentions_preferred = bool(
                self.preferred_country
                and detect_country(f" {self.preferred_country} ") == self.preferred_country
                and re.search(_country_mention_re(self.preferred_country), haystack)
            )
            if not mentions_preferred:
                return FilterResult(
                    passed=False,
                    reason=(
                        f"Remote pre-filtered: '{job.location or job.title}' is remote in "
                        f"{detected.title()}, user targets {self.preferred_country.title()}"
                    ),
                    score_override=10
                )

        # 2. Work Authorization / Sponsorship Blocker — only relevant when the
        #    user actually needs sponsorship. Citizens / GC holders keep these jobs.
        #    Only EXPLICIT refusals hard-block; ambiguous right-to-work boilerplate
        #    ("must be authorized to work in...") appears in postings from employers
        #    that do sponsor, so those flow through to the LLM reranker, which
        #    scores them with the posting text + the employer's public H-1B record.
        #
        #    Matched per SENTENCE, not by substring containment: "there is no
        #    sponsorship requirement for this role" contains "no sponsorship"
        #    and used to hard-block a job the user was fully eligible for —
        #    silently, before scoring, so it never reached the board.
        if self.requires_sponsorship:
            _refusal = find_refusal(desc_low)
            if _refusal:
                return FilterResult(
                    passed=False,
                    reason=(f"Sponsorship pre-filtered: matches '{_refusal.phrase}' "
                            f"in \"{_refusal.sentence[:120]}\""),
                    score_override=10
                )

        # 2b. Citizenship / clearance — against the CONFIRMED status only. A
        #     permanent resident needs no sponsorship and still cannot take a
        #     citizens-only role; an unset status never blocks.
        if not self.requires_sponsorship and self.profile is not None:
            _cit = requires_citizenship(job.description)
            if _cit and _is_confirmed_citizen(self.profile) is False:
                return FilterResult(
                    passed=False,
                    reason=f"Work authorization: requires U.S. citizenship or a clearance — \"{_cit[:120]}\"",
                    score_override=10,
                )

        # 2c. Required degree above the candidate's confirmed degree. A sentence
        #     that accepts equivalent experience, or says "preferred", is not a
        #     requirement; an unknown degree never blocks.
        if self.profile is not None and self.user_degree:
            _req = required_degree_level(job.description)
            _have = _degree_level(self.user_degree)
            if _req and _have is not None and _req[0] > _have:
                return FilterResult(
                    passed=False,
                    reason=f"Education: the posting requires a higher degree — \"{_req[1][:120]}\"",
                    score_override=12,
                )

        # 3. Experience Gap Filter — only active when resume-extracted years are
        #    known (>0). Skipped for new users/students until resume is parsed.
        #    Gap is +4 to allow stretch roles; skips "preferred" mentions.
        if self.cand_years > 0:
            _exp_cutoff = self.cand_years + (2 if self.profile is None else 4)
            _preferred_words = ("preferred", "nice to have", "plus", "ideally", "bonus")
            for m in re.finditer(r'(\d+)\+?\s*years?', desc_low):
                years = int(m.group(1))
                context = desc_low[max(0, m.start() - 20): m.start() + 80]
                if 'experience' in context and years >= _exp_cutoff:
                    if any(w in context for w in _preferred_words):
                        continue
                    return FilterResult(
                        passed=False,
                        reason=f"Experience pre-filtered: requires {years}+ years (candidate has {self.cand_years})",
                        score_override=15
                    )

        # 4. Senior/staff titles — only filtered for non-senior candidates.
        if self.block_senior_titles:
            for t in STAFF_TITLES:
                if title_low.startswith(t) or f" {t}" in title_low:
                    return FilterResult(
                        passed=False,
                        reason=f"Title pre-filtered: '{job.title}' is a senior/staff-level role",
                        score_override=15
                    )

        # 5. Salary Range Filter — only enforce a bound the user expressed, and
        #    ONLY when the posting's currency matches the user's band currency
        #    (raw cross-currency comparison mis-filtered every foreign posting).
        sal_range = _extract_salary_range(desc_low)
        if sal_range and sal_range[2] == self.salary_currency:
            min_sal, max_sal, _ccy = sal_range
            sym = _CCY_SYMBOL.get(_ccy, "$")
            if self.salary_ceiling is not None and min_sal >= self.salary_ceiling:
                return FilterResult(
                    passed=False,
                    reason=f"Salary too high: starts at {sym}{min_sal:,.0f} (target ceiling {sym}{self.salary_ceiling:,.0f})",
                    score_override=20
                )
            if self.salary_floor is not None and max_sal <= self.salary_floor:
                return FilterResult(
                    passed=False,
                    reason=f"Salary too low: up to {sym}{max_sal:,.0f} (target floor {sym}{self.salary_floor:,.0f})",
                    score_override=20
                )

        # 6. Hire-probability filter — block roles the candidate can't credibly
        #    fill, but ONLY when the required skill isn't in their stack.
        #    a) Low-level systems / GPU kernel engineering
        systems_signals = [
            "cuda kernel", "gpu kernel", "write cuda", "triton kernel",
            "systems programming", "kernel developer", "kernel engineer",
            "bare metal", "memory allocator", "compiler engineer", "llvm", "mlir",
        ]
        if not self._has_skill("cuda", "gpu", "kernel", "compiler", "llvm", "systems programming"):
            if any(s in desc_low for s in systems_signals):
                return FilterResult(
                    passed=False,
                    reason="Hire-probability: GPU/kernel/compiler systems role — not in candidate stack",
                    score_override=12
                )

        #    b) C++ or Rust listed as a hard requirement (not nice-to-have)
        if not self._has_skill("c++", "cpp", "rust"):
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

        #    c) Pure research / PhD roles — skip the block for users who hold a PhD.
        if "phd" not in self.user_degree and "doctor" not in self.user_degree:
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
