"""Does this link open the EMPLOYER's application, or a job board's page?

WHY THIS EXISTS (2026-11-02). A user's board held Deepgram's "Research
Engineer, ML Systems" as a jobicy.com link — a job board's listing page with no
form on it — while the shared pool had held Deepgram's own Ashby posting of
the same role, open, for 24 days. She clicked Auto-Fill, the tab opened on
jobicy.com, and nothing filled. Her All Jobs was half aggregator pages (Adzuna,
Jooble, Reed, learn4good, jobleads, …) that ask you to register before they
show the employer's form. Platform-wide, 92 of ~500 board deliveries in 14 days
(18%) pointed at a page like that.

Three places had their own idea of "aggregator", each partial: SerpAPI's
apply-link ranker (`sources/serpapi.py`, a substring list), the ghost filter's
redirect list (`filters/ghost_detector.py`, a likelihood signal — deliberately
left separate: changing it moves ghost stamps), and `source_quality`, which
judges the SOURCE — a SerpAPI row can carry a direct Greenhouse link, and a
Jobicy row never can. Where a job can be applied to is a property of its URL,
so this module judges the URL's HOST and nothing else.

Matching is by host LABELS, never a substring: "greenhouse.io" must not match
"greenhouse.io.example", and an employer called "Reed" must not become a job
board. A brand that runs one site per country (adzuna.co.uk, indeed.ca) is
listed in ``_ANY_TLD_BRANDS`` and matched only as the registrable name.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

#: Job boards, aggregators and middlemen whose page is not the employer's
#: application form. Each entry is a registrable domain; its subdomains match
#: too (uk.linkedin.com, remoteworld.mysmartpros.com). The second group was
#: read off production's board deliveries 2026-10-19..11-02.
AGGREGATOR_DOMAINS: frozenset[str] = frozenset({
    # Sign-in / sign-up walls and "apply with your X profile" flows
    "jobright.ai", "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "dice.com", "monster.com", "careerbuilder.com", "simplyhired.com", "snagajob.com",
    "wellfound.com", "otta.com", "theladders.com", "earnbetter.com", "jobleads.com",
    "jobgether.com", "upwork.com", "haystackapp.io", "workatastartup.com",
    # Re-listers and redirect aggregators
    "adzuna.com", "jooble.org", "lensa.com", "talent.com", "neuvoo.com", "bebee.com",
    "jobrapido.com", "jora.com", "jobsora.com", "jobtensor.com", "careerjet.com",
    "joblist.com", "jobted.com", "jobisit.com", "whatjobs.com", "learn4good.com",
    "themuse.com", "builtin.com", "startup.jobs", "himalayas.app", "findwork.dev",
    "jobicy.com", "remotive.com", "remoteok.com", "remoteok.io", "weworkremotely.com",
    "arbeitnow.com", "reed.co.uk", "trabajo.org", "jobserve.com", "digitalhire.com",
    "jobilize.com", "rekru.ru", "mysmartpros.com", "careersingovernment.com",
    "womenforhire.com",
    # Read off one user's All Jobs pool, 2026-11-02 (hosts no ATS detector knows)
    "builtinla.com", "builtinseattle.com", "builtinnyc.com", "builtinboston.com",
    "builtinchicago.org", "builtinaustin.com", "builtinsf.com", "builtincolorado.com",
    "builtinlondon.uk", "hiring.cafe", "hiringcafe.com", "simplify.jobs", "levels.fyi",
    "tealhq.com", "totaljobs.com", "jobmesh.io", "jobmesh.co.uk", "jobalert.world",
    "recruit.net", "jobstore.com", "jobclerk.com", "s1jobs.com", "fish4.co.uk",
    "jobs24.co.uk", "iitjobs.com", "aijobs.com", "freelancermap.com", "clearancejobs.com",
    "jobteaser.com", "expertini.com", "devitjobs.com", "techengineerjobs.ai",
    "internentry.com", "interninsider.me", "startuphakk.com", "tallo.com", "remote.co",
    "icanapply.uk", "huntukvisasponsors.com", "civiljobs.co.uk", "geckojobs.co.uk",
    "revopscareers.com",
})

#: Brands with one site per country. Matched only as the name right before the
#: public suffix: adzuna.co.uk, indeed.ca, de.indeed.com — never "indeedtech.com".
_ANY_TLD_BRANDS = ("adzuna", "jooble", "arbeitnow", "glassdoor", "indeed", "linkedin",
                   "careerjet", "jobrapido", "talent", "simplyhired", "jobijoba",
                   "efinancialcareers")
_BRAND_RE = re.compile(
    r"(?:^|\.)(?:" + "|".join(_ANY_TLD_BRANDS) + r")\.(?:com?\.)?[a-z]{2,3}$")


def _host(url_or_host: str) -> str:
    s = (url_or_host or "").strip().lower()
    if "://" in s:
        try:
            s = urlparse(s).hostname or ""
        except ValueError:
            return ""
    else:
        s = s.split("/", 1)[0].split(":", 1)[0]
    return s.rstrip(".")


def aggregator_host(url_or_host: str) -> str | None:
    """The job board this link opens on (its host, e.g. "jobicy.com"), or None
    when the link is the employer's own page or an ATS form."""
    host = _host(url_or_host)
    if not host:
        return None
    labels = host.split(".")
    if any(".".join(labels[i:]) in AGGREGATOR_DOMAINS for i in range(len(labels) - 1)) \
            or _BRAND_RE.search(host):
        return host.removeprefix("www.")
    return None


def is_aggregator_url(url_or_host: str) -> bool:
    """True when the link opens a job board's page, not the employer's form."""
    return aggregator_host(url_or_host) is not None


def url_regex() -> str:
    """The same decision as `aggregator_host`, as ONE case-insensitive regex
    over a full URL, for SQL (`column.regexp_match(url_regex(), flags="i")`:
    Postgres `~*`, SQLAlchemy's registered REGEXP on SQLite). Plain groups
    only — no `(?:` — so both engines read it the same way."""
    domains = "|".join(re.escape(d) for d in sorted(AGGREGATOR_DOMAINS))
    brands = "|".join(_ANY_TLD_BRANDS)
    host_end = r"([:/?#]|$)"
    return (r"^https?://([^/?#]*\.)?((" + domains + r")|((" + brands +
            r")\.(com?\.)?[a-z]{2,3}))" + host_end)
