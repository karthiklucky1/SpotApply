"""Sponsorship intelligence — legal, public-data driven.

Assesses whether a company is likely to sponsor a work visa (H-1B), and whether
it is *cap-exempt* (universities / non-profit research / affiliated hospitals),
which can sponsor H-1Bs year-round with NO lottery.

Every verdict carries its own provenance — ``source``, ``as_of`` and
``confidence`` — plus the ``signals`` it was derived from, so the UI can show
*why* and *how old the evidence is* rather than an unexplained badge. We state
facts with dates and let the user conclude; nothing here is a legal opinion.

## Where the real data comes from

Ingest the public registries with ``app.intelligence.h1b_data`` (the admin page
at ``/admin/h1b`` uploads to the same code path)::

    python -m app.intelligence.h1b_data <uscis_datahubexport.csv>
    python -m app.intelligence.h1b_data <uk_register.csv> "united kingdom"

Until a registry is loaded for a country, ``assess`` says so explicitly instead
of implying the employer was looked up and not found — ``registry_loaded``
distinguishes "no record for this employer" from "no dataset at all".

The ``KNOWN_SPONSORS`` / ``KNOWN_NON_SPONSORS`` sets below are a LAST-RESORT
fallback for when no registry is loaded. They are ~80 hand-typed company-name
substrings, not evidence, and they are reported as ``source="curated"`` with
``confidence="low"`` so nothing downstream can mistake them for a public
record. Real data always wins (see the registry branch in ``assess``).

An earlier docstring here promised a ``settings.h1b_employer_csv`` setting and
a ``load_employer_hub`` function. Neither ever existed — the loader is
``h1b_data.ingest_csv``. Do not re-add that claim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from app.common.sponsorship_text import find_refusal

log = logging.getLogger(__name__)


class SponsorshipLikelihood(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


# ── Public-record-informed seed lists ────────────────────────────────────────
# Consistent top H-1B sponsors per public USCIS/DOL disclosure data (big tech,
# the major consultancies/IT services, and banks that file in volume every year).
KNOWN_SPONSORS = {
    # Big tech / product
    "google", "alphabet", "meta", "facebook", "amazon", "apple", "microsoft",
    "netflix", "nvidia", "intel", "qualcomm", "oracle", "salesforce", "adobe",
    "ibm", "cisco", "vmware", "uber", "lyft", "airbnb", "stripe", "block",
    "paypal", "linkedin", "snap", "pinterest", "doordash", "instacart",
    "databricks", "snowflake", "palantir", "twilio", "workday", "servicenow",
    "atlassian", "dropbox", "tesla", "spacex", "bloomberg", "intuit", "ebay",
    "walmart", "capital one", "visa", "mastercard",
    # Major consultancies / IT services (highest-volume sponsors)
    "deloitte", "accenture", "cognizant", "infosys", "tata consultancy", "tcs",
    "wipro", "capgemini", "hcl", "tech mahindra", "ernst & young", "pwc",
    "pricewaterhousecoopers", "kpmg", "mckinsey", "boston consulting",
    "ltimindtree", "mphasis", "persistent systems", "epam",
    # Banks / finance
    "jpmorgan", "jp morgan", "goldman sachs", "morgan stanley", "citigroup",
    "citi", "bank of america", "wells fargo", "american express", "blackrock",
    "two sigma", "citadel", "jane street", "de shaw",
}

# Defense / government-linked employers that typically require US persons.
KNOWN_NON_SPONSORS = {
    "lockheed", "raytheon", "rtx", "boeing", "northrop", "general dynamics",
    "l3harris", "leidos", "booz allen", "saic", "draper", "mitre", "anduril",
    "palantir usg", "caci", "peraton",
}

# Cap-exempt signals: institution of higher ed, non-profit research, hospitals.
CAP_EXEMPT_NAME_SIGNALS = (
    "university", "college", "institute of technology", "polytechnic",
    "school of medicine", "medical center", "medical school", "health system",
    "hospital", "research institute", "research center", "national laboratory",
    "national lab", "cancer center", "children's hospital", "state university",
)
CAP_EXEMPT_DESC_SIGNALS = (
    "cap-exempt", "cap exempt", "h-1b cap-exempt", "h1b cap exempt",
    "institution of higher education", "non-profit research", "nonprofit research",
)

# The refusal phrases live in app/common/sponsorship_text.py, shared with the
# rule filter. Re-exported here so existing importers keep working.
from app.common.sponsorship_text import (  # noqa: E402,F401
    NO_SPONSORSHIP_HARD as NO_SPONSORSHIP_PATTERNS,
)


@dataclass
class SponsorshipAssessment:
    likelihood: SponsorshipLikelihood
    cap_exempt: bool
    reason: str
    badge: str                 # short UI label
    explicitly_refuses: bool = False
    # ── Provenance. Present on every verdict so the card can show its working.
    # source:     "uscis" | "register" | "posting" | "curated" | "none"
    # as_of:      human-readable vintage of the evidence ("FY2024", "").
    # confidence: how much weight the EVIDENCE carries — deliberately separate
    #             from `likelihood`, which is about whether they sponsor.
    #             A curated name-list guess is "low" even when it says HIGH.
    source: str = "none"
    as_of: str = ""
    confidence: str = "low"
    # Every signal that fed the verdict, including ones the headline had to
    # resolve against each other. A posting from a cap-exempt university that
    # also says "we do not sponsor" has two true signals; suppressing one and
    # showing the other as certain is how a badge becomes a lie.
    signals: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def contradictory(self) -> bool:
        """True when the evidence disagrees with itself."""
        kinds = {s.get("kind") for s in self.signals}
        return "refusal" in kinds and bool(kinds & {"uscis", "register", "cap_exempt"})

    @property
    def tone(self) -> str:
        """UI colour hint: good | bad | mixed | unknown.

        MEDIUM counts as good. It is only ever returned for "Has sponsored" —
        an employer with a real USCIS approval on record — and it used to fall
        through to "unknown", so 2,072 jobs a day were shown as "Sponsorship
        not stated" when we held a public filing proving the opposite. Every
        consumer branches on 'good'/'bad' and treats the rest as unknown, so
        that single missing case suppressed the badge, the card chip and the
        drawer verdict at once.
        """
        if self.contradictory:
            return "mixed"
        if self.explicitly_refuses or self.likelihood == SponsorshipLikelihood.LOW:
            return "bad"
        if self.cap_exempt or self.likelihood in (
                SponsorshipLikelihood.HIGH, SponsorshipLikelihood.MEDIUM):
            return "good"
        return "unknown"


def _signal(kind: str, statement: str, source: str = "", as_of: str = "") -> Dict[str, Any]:
    return {"kind": kind, "statement": statement, "source": source, "as_of": as_of}


def _norm(s) -> str:
    return (s or "").lower().strip()


def _is_cap_exempt(name: str, desc: str, url: str) -> bool:
    if any(sig in name for sig in CAP_EXEMPT_NAME_SIGNALS):
        return True
    if any(sig in desc for sig in CAP_EXEMPT_DESC_SIGNALS):
        return True
    # .edu domains are institutions of higher education. Match on a real label
    # boundary: a bare `".edu" in host` also matched jobs.educationcorp.com and
    # careers.edulastic.io, handing a "no-lottery sponsor" badge to companies
    # with no cap-exempt status at all.
    host = url.split("//")[-1].split("/")[0].split(":")[0].rstrip(".")
    if host == "edu" or host.endswith(".edu"):
        return True
    return False


def assess(company: str = "", description: str = "", url: str = "",
           location: str = "") -> SponsorshipAssessment:
    """Return a legal, explainable sponsorship assessment for a posting.

    Explicit refusal detection is universal; everything else (cap-exempt,
    USCIS records, the curated sponsor lists) is US/H-1B-specific and only
    applies when the posting isn't clearly located in another country.
    """
    name, desc, u = _norm(company), _norm(description), _norm(url)

    # Sentence-scoped and negation-aware — see app/common/sponsorship_text.py.
    refusal = find_refusal(desc)
    explicitly_refuses = refusal is not None
    signals: List[Dict[str, Any]] = []
    if refusal:
        signals.append(_signal(
            "refusal",
            f"The posting says: “{refusal.sentence}”",
            source="posting",
        ))

    # Non-US posting → skip all H-1B-specific intelligence.
    try:
        from app.common.geo import detect_country
        job_country = detect_country(location or "")
    except Exception:
        job_country = ""
    if job_country and job_country != "united states":
        # Country-specific licensed-sponsor register (UK Register of Licensed
        # Sponsors, Canada LMIA employers, ...) — if it's loaded, use it. This
        # runs even when the posting refuses, so a refusal that contradicts an
        # official register is visible instead of silently winning.
        reg_rec = None
        reg_loaded = False
        extra = ""
        try:
            from app.intelligence.h1b_data import (
                lookup as _reg_lookup, has_country_data as _reg_has,
            )
            reg_rec = _reg_lookup(company, country=job_country)
            reg_loaded = _reg_has(job_country)
        except Exception:
            pass

        if reg_rec:
            extra = f" ({reg_rec['detail']})" if reg_rec.get("detail") else ""
            if reg_rec.get("approvals"):
                extra += f" — {reg_rec['approvals']} approved position(s) on record"
            signals.append(_signal(
                "register",
                f"Listed on {job_country.title()}'s official sponsor register{extra}.",
                source="register", as_of=str(reg_rec.get("year") or "") or "",
            ))

        if explicitly_refuses:
            reason = ("This posting explicitly states it will not sponsor a work visa / "
                      "requires an existing right to work.")
            if reg_rec:
                reason += (f" Note the employer IS on {job_country.title()}'s licensed-sponsor "
                           "register — the refusal may apply to this role only.")
            return SponsorshipAssessment(
                SponsorshipLikelihood.LOW, False, reason,
                "Conflicting signals" if reg_rec else "No sponsorship",
                explicitly_refuses=True,
                source="posting", as_of="", confidence="high", signals=signals,
            )
        if reg_rec:
            return SponsorshipAssessment(
                SponsorshipLikelihood.HIGH, False,
                f"Listed on {job_country.title()}'s official sponsor register"
                f"{extra} — authorised to sponsor work visas.",
                "Licensed sponsor",
                source="register", as_of=str(reg_rec.get("year") or "") or "",
                confidence="high", signals=signals,
            )
        if reg_loaded:
            return SponsorshipAssessment(
                SponsorshipLikelihood.LOW, False,
                f"Not found on {job_country.title()}'s sponsor register. Registers "
                "list legal entity names, so double-check under the company's "
                "registered name before ruling it out.",
                "Not on register",
                source="register", confidence="medium", signals=signals,
            )
        return SponsorshipAssessment(
            SponsorshipLikelihood.UNKNOWN, False,
            f"Posting is located in {job_country.title()} — no sponsor register is "
            f"loaded for {job_country.title()}, so we have nothing to check the "
            "employer against. Check the employer's policy before applying.",
            "Check visa policy",
            source="none", confidence="low", signals=signals,
        )

    # ── United States ────────────────────────────────────────────────────────
    cap_exempt = _is_cap_exempt(name, desc, u)
    if cap_exempt:
        signals.append(_signal(
            "cap_exempt",
            "Employer looks cap-exempt (university / non-profit research / hospital), "
            "which can sponsor H-1B year-round with no lottery.",
            source="posting",
        ))

    # Public record. Looked up even when the posting refuses — the two facts
    # coexist and the user is entitled to both.
    rec = None
    registry_loaded = False
    try:
        from app.intelligence.h1b_data import (
            lookup as _h1b_lookup, has_country_data as _h1b_has,
        )
        rec = _h1b_lookup(company)
        registry_loaded = _h1b_has("united states")
    except Exception:
        pass

    rec_year = ""
    if rec and (rec["approvals"] + rec["denials"]) >= 1:
        rec_year = f"FY{rec['year']}" if rec.get("year") else ""
        signals.append(_signal(
            "uscis",
            f"USCIS record: {rec['approvals']} H-1B approval(s), "
            f"{int(rec['rate'] * 100)}% approval rate.",
            source="uscis", as_of=rec_year,
        ))

    # A refusal in the posting is about THIS role and outranks the employer's
    # overall record for the purpose of applying — but it no longer erases it.
    if explicitly_refuses:
        reason = "This posting explicitly states it will not sponsor a work visa."
        badge = "No sponsorship"
        if rec or cap_exempt:
            badge = "Conflicting signals"
            if rec:
                reason += (f" The employer does have a public USCIS record"
                           f"{f' ({rec_year})' if rec_year else ''} of "
                           f"{rec['approvals']} approval(s), so this refusal may apply "
                           "to this role only.")
            elif cap_exempt:
                reason += (" The employer also looks cap-exempt, which normally means "
                           "it can sponsor year-round — worth confirming directly.")
        return SponsorshipAssessment(
            SponsorshipLikelihood.LOW, cap_exempt, reason, badge,
            explicitly_refuses=True,
            source="posting", as_of="", confidence="high", signals=signals,
        )

    if cap_exempt:
        return SponsorshipAssessment(
            SponsorshipLikelihood.HIGH, True,
            "Cap-exempt employer (university / non-profit research / hospital) — "
            "can sponsor H-1B year-round with no lottery.",
            "No-lottery sponsor",
            source="posting", confidence="medium", signals=signals,
        )

    if rec and (rec["approvals"] + rec["denials"]) >= 1:
        rate = rec["rate"]
        if rec["approvals"] >= 5 and rate >= 0.5:
            return SponsorshipAssessment(
                SponsorshipLikelihood.HIGH, False,
                f"USCIS record: {rec['approvals']} H-1B approvals"
                f"{f' ({rec_year})' if rec_year else ''}, {int(rate*100)}% approval rate.",
                "Sponsors H-1B",
                source="uscis", as_of=rec_year, confidence="high", signals=signals,
            )
        if rec["approvals"] >= 1:
            return SponsorshipAssessment(
                SponsorshipLikelihood.MEDIUM, False,
                f"USCIS record: {rec['approvals']} H-1B approval(s)"
                f"{f' ({rec_year})' if rec_year else ''} — has sponsored before.",
                "Has sponsored",
                source="uscis", as_of=rec_year, confidence="high", signals=signals,
            )

    if any(b in name for b in KNOWN_NON_SPONSORS):
        return SponsorshipAssessment(
            SponsorshipLikelihood.LOW, False,
            "Defense / government-linked employer — these roles usually require "
            "US persons. Not checked against a public filing record.",
            "Rarely sponsors",
            source="curated", confidence="low", signals=signals,
        )

    if any(b in name for b in KNOWN_SPONSORS):
        # A name-list hit is a GUESS, not a record. Say so, and never claim a
        # public record we did not read — the old copy asserted "files
        # regularly per public USCIS/DOL records" for employers we had never
        # looked up.
        return SponsorshipAssessment(
            SponsorshipLikelihood.HIGH, False,
            "Widely known H-1B sponsor. This is a name match against a curated "
            "list, not a public filing record — load USCIS data for the real "
            "numbers.",
            "Likely sponsors",
            source="curated", confidence="low", signals=signals,
        )

    # Nothing found. Distinguish "we looked and this employer is absent" from
    # "we have no dataset to look in" — the old copy said the same thing for
    # both, so an empty registry read as a negative finding about the employer.
    if registry_loaded:
        return SponsorshipAssessment(
            SponsorshipLikelihood.UNKNOWN, False,
            "No USCIS H-1B filing found for this employer name. Records list legal "
            "entity names, so check under the registered name before ruling it out.",
            "No filing found",
            source="uscis", confidence="medium", signals=signals,
        )
    return SponsorshipAssessment(
        SponsorshipLikelihood.UNKNOWN, False,
        "No sponsorship dataset is loaded, so this employer has not been checked "
        "against public records.",
        "Not checked",
        source="none", confidence="low", signals=signals,
    )
