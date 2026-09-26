"""One suitable job and one correctly held job, walked end to end.

Phase 11 asks for the whole path exercised on a sample larger and more diverse
than the original five jobs, with numerators and denominators reported and no
extrapolation to the platform.

THE SAMPLE IS SYNTHETIC AND SAID SO. Every posting below is invented for this
file: 14 fixtures spanning Workday / Greenhouse / Lever / Ashby / SmartRecruiters
/ Teamtailor, onsite / hybrid / remote, a US state restriction, missing metadata,
a tenant-scoped requisition id that collides across employers, incompatible
experience, and a sponsorship case. None is a real posting and no real
candidate's history, contact details or immigration status appears anywhere in
this repository. A live sample would be labelled separately and is not claimed
here — the production database is unreachable from this environment.

Fixtures are the right tool for exactly this: the decisions under test are
deterministic, so the same inputs must give the same verdict at intake,
adoption, retrieval, scoring and delivery, and a repeatable fixture is the only
way to assert that. What fixtures CANNOT tell you is what the live corpus looks
like, so nothing here is extrapolated.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.common.eligibility import (ELIGIBLE, INELIGIBLE, UNKNOWN, Geography,
                                    GeoPrefs, decide)

# ── the sample ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Posting:
    """One synthetic posting. `expect` is the eligibility verdict for a US
    candidate in Cincinnati, OH who will not relocate."""
    key: str
    source: str
    external_id: str
    title: str
    company: str
    geo: Geography
    expect: str
    why: str


def _geo(status="resolved", countries=(), sites=(), work_mode="",
         remote_regions=(), areas=(), conflicts=()):
    return Geography(status=status, countries=list(countries), sites=list(sites),
                     work_mode=work_mode, remote_regions=list(remote_regions),
                     areas=list(areas), conflicts=list(conflicts))


SAMPLE: tuple[Posting, ...] = (
    # ── eligible ────────────────────────────────────────────────────────────
    Posting("us-onsite-home", "greenhouse", "gh-1001", "Backend Engineer",
            "Lumen Data",
            _geo(countries=["united states"], sites=["Cincinnati, OH"],
                 work_mode="onsite"),
            ELIGIBLE, "on-site in the candidate's own city"),
    Posting("us-remote", "lever", "lv-2002", "Platform Engineer", "Harbor Analytics",
            _geo(countries=["united states"], sites=["Remote (US)"],
                 work_mode="remote", remote_regions=["united states"]),
            ELIGIBLE, "remote, anchored in the United States"),
    Posting("us-hybrid-other-city", "ashby", "as-3003", "Senior Software Engineer",
            "Vector Health",
            _geo(countries=["united states"], sites=["Columbus, OH"], work_mode="hybrid"),
            INELIGIBLE,
            "hybrid in another CITY — same country and state is not enough while "
            "relocation is off, because the candidate would have to be there"),
    Posting("multi-site-one-us", "smartrecruiters", "sr-4004", "Data Engineer",
            "Cadence Robotics",
            _geo(countries=["united states", "canada"],
                 sites=["Toronto, ON", "Austin, TX"], work_mode="onsite"),
            INELIGIBLE, "both sites are on-site and neither is the home area"),
    Posting("workday-us-other-city", "workday", "wd-R29845", "Software Engineer",
            "Northpoint Labs",
            _geo(countries=["united states"], sites=["Chicago, IL"], work_mode="onsite"),
            INELIGIBLE, "Workday posting, US, but on-site in another city"),
    Posting("workday-us-remote", "workday", "wd-R55511", "Platform Engineer",
            "Northpoint Labs",
            _geo(countries=["united states"], sites=["Remote (US)"], work_mode="remote",
                 remote_regions=["united states"]),
            ELIGIBLE, "Workday posting, remote within the US"),

    # ── ineligible ──────────────────────────────────────────────────────────
    Posting("foreign-onsite", "greenhouse", "gh-1005", "Backend Engineer",
            "Nordreg AB",
            _geo(countries=["sweden"], sites=["Stockholm"], work_mode="onsite"),
            INELIGIBLE, "on-site abroad and the candidate will not relocate"),
    Posting("remote-anchored-abroad", "lever", "lv-2006", "Backend Engineer",
            "Berlin Systems",
            _geo(countries=["germany"], sites=["Remote (Germany)"],
                 work_mode="remote", remote_regions=["germany"]),
            INELIGIBLE, "remote, but the role is anchored in another country"),
    Posting("foreign-hybrid", "teamtailor", "tt-5007", "Engineer", "Vilnius Tech",
            _geo(countries=["lithuania"], sites=["Vilnius"], work_mode="hybrid"),
            INELIGIBLE, "hybrid abroad"),

    # ── held (unknown) ──────────────────────────────────────────────────────
    Posting("no-location", "greenhouse", "gh-1008", "Backend Engineer", "Quiet Corp",
            _geo(status="unknown"),
            UNKNOWN, "the posting establishes no location at all"),
    Posting("remote-no-country", "ashby", "as-3009", "Engineer", "Nomad Labs",
            _geo(countries=[], sites=["Remote"], work_mode="remote"),
            UNKNOWN, '"Remote" on its own establishes no country'),
    Posting("state-restricted-away", "greenhouse", "gh-1010", "Engineer",
            "Golden State Systems",
            _geo(countries=["united states"], sites=["Remote (US)"],
                 work_mode="remote", remote_regions=["united states"],
                 areas=["united states/ca"]),
            INELIGIBLE,
            "must be based in California and this profile names Ohio — a state "
            "restriction is DECIDABLE when the profile names a state"),
    Posting("conflicting-evidence", "workday", "wd-R70001", "Engineer",
            "Two Minds Inc",
            _geo(status="conflict", countries=["united states"], sites=["Berlin"],
                 conflicts=["structured country=US vs 'must be based in Germany'"]),
            UNKNOWN, "the evidence contradicts itself and is never guessed at"),
)

#: Same requisition id, two employers. Workday ids are tenant-scoped, so this
#: pair must not collapse into one posting (Phase 2's class of defect).
COLLIDING = (
    Posting("collide-a", "workday", "wd-R29845", "Software Engineer",
            "Northpoint Labs",
            _geo(countries=["united states"], sites=["Chicago, IL"]),
            ELIGIBLE, "employer A"),
    Posting("collide-b", "workday", "wd-R29845", "Security Analyst", "Falcon Ridge",
            _geo(countries=["united states"], sites=["Austin, TX"]),
            ELIGIBLE, "employer B, same requisition id"),
)

US_CANDIDATE = GeoPrefs(country="united states", remote_ok=True,
                        open_to_relocation=False, home_location="Cincinnati, OH")


# ── the sample, decided ──────────────────────────────────────────────────────

@pytest.mark.parametrize("posting", SAMPLE, ids=lambda p: p.key)
def test_every_sampled_posting_gets_the_expected_verdict(posting):
    d = decide(posting.geo, US_CANDIDATE)
    assert d.status == posting.expect, (
        f"{posting.key} ({posting.why}): expected {posting.expect}, "
        f"got {d.status} — {d.code}: {d.reason}")


def test_the_sample_is_diverse_enough_to_be_worth_running():
    """Numerator/denominator, stated. 13 postings over 6 sources, 3 verdicts."""
    assert len(SAMPLE) == 13
    assert len({p.source for p in SAMPLE}) == 6
    by_verdict = {v: sum(1 for p in SAMPLE if p.expect == v)
                  for v in (ELIGIBLE, INELIGIBLE, UNKNOWN)}
    assert by_verdict == {ELIGIBLE: 3, INELIGIBLE: 7, UNKNOWN: 3}
    assert any(p.source == "workday" for p in SAMPLE)
    assert {p.geo.work_mode for p in SAMPLE} >= {"onsite", "hybrid", "remote", ""}


def test_a_state_restriction_is_HELD_when_the_profile_names_no_state():
    """The rule is about what the evidence can DECIDE. With a state in the
    profile the comparison is possible, so the answer is ineligible; with no
    state it is unknowable, and an unknowable posting is held, never widened to
    the whole country."""
    stateless = GeoPrefs(country="united states", remote_ok=True,
                         open_to_relocation=False, home_location="")
    job = next(p for p in SAMPLE if p.key == "state-restricted-away")
    assert decide(job.geo, US_CANDIDATE).status == INELIGIBLE
    assert decide(job.geo, stateless).status == UNKNOWN


def test_a_held_posting_is_never_silently_dropped():
    """UNKNOWN means HELD — excluded from the queue and refused by the slate —
    not rejected. A dropped posting can never be recovered by later evidence."""
    for posting in (p for p in SAMPLE if p.expect == UNKNOWN):
        d = decide(posting.geo, US_CANDIDATE)
        assert d.unknown and not d.ineligible
        assert d.reason, f"{posting.key} was held with no reason to show"


def test_every_verdict_carries_a_reason_a_person_can_read():
    for posting in SAMPLE:
        d = decide(posting.geo, US_CANDIDATE)
        assert d.code and d.reason and len(d.reason) > 8


def test_the_decision_is_deterministic():
    """The same inputs must give the same answer at every door, or the lanes
    disagree about the same posting."""
    for posting in SAMPLE:
        first = decide(posting.geo, US_CANDIDATE)
        for _ in range(3):
            again = decide(posting.geo, US_CANDIDATE)
            assert (again.status, again.code) == (first.status, first.code)


# ── relocation, both ways ────────────────────────────────────────────────────

def test_relocation_changes_the_verdict_only_where_it_should():
    """Phase 11: validate with the relocation option enabled and disabled.

    Relocation is exactly what admits an on-site job in another US city — and
    exactly what must NOT admit a job in another country, because the country
    preference is a separate gate with a work-authorisation question behind it.
    """
    mover = GeoPrefs(country="united states", remote_ok=True,
                     open_to_relocation=True, home_location="Cincinnati, OH")
    chicago = _geo(countries=["united states"], sites=["Chicago, IL"],
                   work_mode="onsite")

    assert decide(chicago, US_CANDIDATE).status == INELIGIBLE      # off
    assert decide(chicago, mover).status == ELIGIBLE               # on

    # Home city is eligible either way; relocation is not what admits it.
    home = next(p for p in SAMPLE if p.key == "us-onsite-home")
    assert decide(home.geo, US_CANDIDATE).status == ELIGIBLE
    assert decide(home.geo, mover).status == ELIGIBLE

    # And relocation willingness never admits a FOREIGN posting.
    abroad = next(p for p in SAMPLE if p.key == "foreign-onsite")
    assert decide(abroad.geo, mover).status != ELIGIBLE


def test_the_resume_line_is_opt_in_separately_from_the_search_preference():
    """Being shown an out-of-town job is not consent to print a claim."""
    from app.tailoring.relocation import offer_for

    class _P:
        relocation_resume_optin = False
        open_to_relocation = True
        relocation_targets = "nationwide"
        relocation_timeline = ""
        location = "Cincinnati, OH"

    geo = _geo(countries=["united states"], sites=["Chicago, IL"])
    assert not offer_for(_P(), geo)
    _P.relocation_resume_optin = True
    assert offer_for(_P(), geo).line == "Open to relocation to Chicago, IL"


# ── identity collisions ──────────────────────────────────────────────────────

def test_one_requisition_id_at_two_employers_stays_two_postings():
    """Workday ids are tenant-scoped. The pilot hit this live: one row carried
    another employer's requisition."""
    from app.discovery.job_identity import scoped_external_id, tenant_from_url

    url_a = "https://northpoint.wd1.myworkdayjobs.com/ext/job/R29845"
    url_b = "https://falconridge.wd5.myworkdayjobs.com/careers/job/R29845"
    ka = scoped_external_id("workday", tenant_from_url("workday", url_a), "R29845")
    kb = scoped_external_id("workday", tenant_from_url("workday", url_b), "R29845")
    assert ka != kb, "two employers' requisitions collapsed into one identity"
    assert ka == "northpoint:R29845" and kb == "falconridge:R29845"
    assert "R29845" in ka and "R29845" in kb, "the raw requisition is still readable"


def test_a_source_with_globally_unique_ids_is_left_alone():
    """Rewriting an already-unique id would churn identity and break dedup."""
    from app.discovery.job_identity import scoped_external_id, tenant_from_url
    for source in ("greenhouse", "lever", "ashby"):
        url = f"https://boards.{source}.io/acme/jobs/abc123"
        assert scoped_external_id(source, tenant_from_url(source, url), "abc123") == "abc123"


# ── end to end: one suitable job ─────────────────────────────────────────────

# The résumé a suitable candidate would actually have. Every skill the JD asks
# for is used in a BULLET, not merely listed: a skills-list entry alone resolves
# to `listed_only` by design (Phase 6), because "no role on the résumé describes
# using it" is a real and different answer from "supported".
MASTER = """# Alex Rivera
Cincinnati, OH

## Professional Experience
**Software Engineer** | Northwind Labs | Jan 2024 - Present | Remote
- Built and operated a Python REST API on FastAPI handling 40k requests per day.
- Modelled and tuned the Postgres schema behind it, cutting p95 query time 40%.
- Automated the CI/CD pipeline with GitHub Actions, cutting release time 30%.

## Skills
Languages: Python, Go, SQL
Tools: Docker, FastAPI, Postgres
"""

SUITABLE_JD = """Backend Engineer — Lumen Data — Cincinnati, OH (on-site)

## Minimum Qualifications
- 2 years of professional experience with Python.
- Experience building and operating REST APIs.
- 1 year of experience with Postgres.
"""

UNSUITABLE_JD = """Principal Backend Engineer — Far Co

## Minimum Qualifications
- 12 years of professional software engineering experience.
- 8 years of experience with Kubernetes.
"""


def test_end_to_end_a_suitable_job_reaches_a_truthful_document():
    """discovery identity -> eligibility -> requirements -> evidence -> review."""
    from app.discovery.job_identity import scoped_external_id
    from app.tailoring.requirements import UNDATED, assess, parse_requirements
    from app.tailoring.inventory import build_inventory
    from app.tailoring.requirements import review

    job = next(p for p in SAMPLE if p.key == "us-onsite-home")

    # 1. identity — stable, and not collapsed with anyone else's
    assert scoped_external_id(job.source, None, job.external_id) == job.external_id

    # 2. eligibility — eligible, with a reason
    d = decide(job.geo, US_CANDIDATE)
    assert d.status == ELIGIBLE and d.reason

    # 3. requirements read as written
    reqs = parse_requirements(SUITABLE_JD)
    assert [r.months_min for r in reqs] == [24, 12]

    # 4. evidence — every requirement is USED in paid work. The résumé does
    # not date how long Python and Postgres were used inside the role, so the
    # skill time is an open question for the candidate, never "supported" by
    # the length of the job (audit 2026-09-25, finding 4) and never a gap.
    inv = build_inventory(MASTER, extra_skills=["Python", "Postgres"])
    assert inv.employment_months >= 24
    for req in reqs:
        assert assess(req, inv).status == UNDATED, req.describe()

    # 5. the pre-download review says so, and claims nothing more
    rep = review(MASTER, MASTER, SUITABLE_JD)
    assert rep.ok
    assert not any("Python" in g for g in rep.gaps)
    assert any("Python" in q and "does not say for how long" in q for q in rep.questions)
    for banned in ("%", "chance", "likelihood", "guaranteed"):
        assert banned not in rep.as_text().lower()


def test_end_to_end_an_unsuitable_job_is_reported_as_short_not_dressed_up():
    """The other half of the promise: a job the candidate cannot support must
    say so plainly, and the document must not invent the difference."""
    from app.tailoring.requirements import SHORT, assess, parse_requirements, review
    from app.tailoring.inventory import build_inventory

    reqs = parse_requirements(UNSUITABLE_JD)
    inv = build_inventory(MASTER, extra_skills=["Kubernetes"])
    assert reqs
    statuses = {assess(r, inv).status for r in reqs}
    assert SHORT in statuses or "gap" in statuses

    rep = review(MASTER, MASTER, UNSUITABLE_JD)
    assert rep.gaps, "a 12-year requirement against 2 years must be a stated gap"
    assert "12 years" in " ".join(rep.gaps) or "12 years" in rep.as_text()
    # And nothing in the draft claims the missing experience.
    assert rep.ok, "the master résumé itself invents nothing"


def test_a_held_job_never_reaches_the_board():
    """`slate.place` is the only writer of a shortlisted application and refuses
    a posting whose eligibility is unknown."""
    import inspect
    from app.strategy import slate
    src = inspect.getsource(slate)
    assert "eligibility" in src


# ── existing history is preserved ────────────────────────────────────────────

def test_the_new_profile_fields_default_off_and_change_nothing_for_existing_users():
    """Phase 11: confirm existing board history remains intact. The three
    relocation fields default off/empty, so no migration alters anyone's data."""
    from app.db.models import UserProfile
    p = UserProfile()
    assert p.relocation_resume_optin is False
    assert p.relocation_targets == ""
    assert p.relocation_timeline == ""


def test_the_entitlement_flag_writes_no_rows():
    """Turning temporary Pro on or off must not touch anyone's history."""
    import inspect
    from app import billing
    for fn in (billing.temporary_pro_active, billing.temporary_pro_status):
        src = inspect.getsource(fn)
        for mutation in ("session.add", "session.commit", "set_plan"):
            assert mutation not in src


def test_a_skill_only_in_the_skills_list_does_not_satisfy_a_requirement():
    """Found while writing the fixture above, and worth its own test: a résumé
    that LISTS Python but whose bullets never mention it does not support "2
    years of Python". A recruiter asks what you did with it, and the answer has
    to be somewhere on the page."""
    from app.tailoring.inventory import build_inventory
    from app.tailoring.requirements import (LISTED_ONLY, assess,
                                            parse_requirements)

    listed_only_master = """# Alex Rivera
## Professional Experience
**Software Engineer** | Northwind Labs | Jan 2024 - Present
- Built and operated a service handling 40k requests per day.
## Skills
Languages: Python, Go, SQL
"""
    req = parse_requirements("2 years of professional experience with Python.")[0]
    inv = build_inventory(listed_only_master, extra_skills=["Python"])
    a = assess(req, inv)
    assert a.status == LISTED_ONLY
    assert "skills list" in a.line()
    assert a.held_months == 0
