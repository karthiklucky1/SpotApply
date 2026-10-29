"""The cheap gates drop only jobs the user truly cannot take (2026-10-28).

Founder: "have you checked with jobs … check in all directions". One F-1 OPT
AI-developer profile's queue was replayed against the production gates: 839
jobs had been drained before the authoritative scorer ever saw them, and most
of the misses were the GATES, not the model's judgement of fit:

  * the years rule read "6–8 years" as 8, "BS with 8+, MS with 6+" as 8, and
    "gtreasury has more than 40 years of experience" as a requirement;
  * "Member of Technical Staff" counted as staff-level;
  * "working alongside compiler engineers", "proficiency in C++ and/or Python"
    and "showcase your work at ICLR, ICML and NeurIPS" blocked a Python dev;
  * the Tier-1 model scored 120 verified-eligible US jobs exactly 30 on
    "Job location is not in the United States" (Los Angeles, San Mateo, …)
    and 62 more on "no sponsorship mentioned";
  * the door gate called "Applied AI Engineer" Wrong Door (tests/
    test_pipeline_door_filter.py).

And in the other direction: ITAR "(i) U.S. citizen or national, (ii) … lawful
permanent resident" lists, a refusal wrapped across lines, and German sites
read as Delaware ("Heusweiler, SAARLAND, DE") let jobs the user cannot hold
through. Every sentence below is from a real posting in that replay.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.common.geo import detect_country, detect_us_state, foreign_places_named
from app.common.sponsorship_text import find_refusal
from app.db.init_db import get_session
from app.db.models import FunnelEvent, Job, JobSource, UserProfile
from app.discovery.base import GeoEvidence, RawJob
from app.matching import reranker as rr
from app.matching.filters.rule_filter import RuleFilter
from app.tailoring.requirements import minimum_years_required


def _profile(**kw):
    base = dict(user_id="pfa-user", years_experience=4, degree="Master of Engineering",
                key_skills="Python, FastAPI, LangChain, RAG, PostgreSQL, Docker, React.js, AWS",
                target_roles="AI Engineer, Backend Engineer, AI Full Stack Developer",
                current_title="AI Full Stack Developer", work_auth_status="OPT",
                work_authorization="F-1 OPT", preferred_country="United States",
                location="Cincinnati, OH", open_to_relocation=True, remote_ok=True,
                job_type_preference="full_time")
    base.update(kw)
    return UserProfile(**base)


def _job(title="Software Engineer", desc="Build APIs in Python.", **kw):
    return Job(source=JobSource.GREENHOUSE, external_id=kw.pop("external_id", "pfa-x"),
               company=kw.pop("company", "Acme"), title=title, url="https://x.co/j",
               location=kw.pop("location", "Chicago, IL"), description=desc, **kw)


# ── years: read as written ──────────────────────────────────────────────────

@pytest.mark.parametrize("text,years", [
    ("6–8 years of professional software engineering experience.", 6),
    ("Recommended Qualifications:\n6-10 years of relevant experience in apps development", 6),
    ("8 - 12years related work experience", 8),
    ("Ten (10) to twelve (12) years of professional software engineering experience", 10),
    ("Required experience: PhD with 2+ years, MS with 6+ years, or BS with 8+ years of "
     "relevant experience in computer science", 6),
    ("Typically requires a minimum of 8 years of related experience; or 6 years and an\n"
     "advanced degree.", 6),
    ("Bachelor's degree and 4+ years of experience in DevOps or DevSecOps, or 8+ years of "
     "experience in DevOps or DevSecOps in lieu of a degree", 4),
    ("Bachelor’s degree in a STEM discipline with 8 years of related experience, 6 years with "
     "masters, 4 years with PhD", 6),
    ("Basic Qualifications\n8+ years of professional software engineering experience; Java, Go", 8),
    ("10+ years of software engineering experience, including 5+ years of experience in a "
     "strategic technical role", 10),
    ("8+ years of experience in full-stack development, preferably in high-growth product "
     "environments.", 8),
    ("Bachelor's degree or higher (4 years of additional experience may substitute if no "
     "degree)\n12+ years of experience working in government or industry", 12),
    ("10+ years of experience in data architecture, data engineering, or analytics platform "
     "design, with at least 3–5 years in a pre-sales capacity", 10),
    ("<h3>Minimum Qualifications</h3><ul><li>7+ years of experience with Go</li></ul>", 7),
    # Random-pool cases (2026-10-28): ladders across lines, the nearest number owns a no-degree phrase.
    ("8+ years aerospace manufacturing experience\r5+ years experience with advanced degree", 5),
    ("Master’s degree with 3 years of experience, or a Bachelor's degree with 5 years of experience, "
     "or an Associates degree with 7 years of experience; a High School diploma or equivalent with 9 "
     "years of experience may be considered in lieu of a completed degree.", 3),
    ("4–6 years in analytics or data analysis, with regulated industry experience preferred.", 4),
    ("Minimum eight years of progressively responsible experience administering systems", 8),
])
def test_the_requirement_is_the_floor_the_posting_states(text, years):
    got = minimum_years_required(text, degree_level=2)
    assert got is not None and got[0] == years, got


@pytest.mark.parametrize("text", [
    "With over 50 years of experience in aviation technology, Curtiss-Wright's sensors lead.",
    "gtreasury has more than 40 years of experience supporting the world's largest firms.",
    "Named to the Inc. 5000 list for the last 12 years. Experience matters.",
    "Preferred Qualifications\n5-8 years of work experience in related field",
    "<h3>Preferred Qualifications</h3><ul><li>7+ years of experience with Go</li></ul>",
    "Candidates with 5+ years of Python experience preferred.",
    "Up to 5 years of experience",
    "Bachelor's degree required.\n10 years of direct relevant technical experience may be substituted for education",
    "Associate's Degree and 7 years of directly relevant experience may be considered in lieu of a Bachelor’s degree.",
    "Bachelor's degree or additional 4 years equivalent experience",
    "1-3 years’ BA or BS in Business, Accounting, Communication or related field experience preferred",
    "Experience with Kafka is a plus. We offer a 3 year vesting schedule.",
    # A benefit, from a posting the real scorer rated 78.
    "6-Week Paid Sabbatical after 10 years of service (and every 5 years thereafter). "
    "Experience-driven culture.",
])
def test_history_preferences_and_ceilings_are_not_requirements(text):
    assert minimum_years_required(text, degree_level=2) is None


def test_the_rule_filter_keeps_a_range_and_drops_a_real_gap():
    rf = RuleFilter(profile=_profile())
    keep = _job("Senior Full-Stack Engineer (Java / Spring Boot)",
                "6–8 years of professional software engineering experience.")
    drop = _job("Senior Software Engineer",
                "10+ years of professional software development experience across the full SDLC")
    assert rf.filter(keep).passed
    res = rf.filter(drop)
    assert not res.passed and res.reason.startswith("Experience pre-filtered: requires 10+ years")


# ── titles and hire-probability ─────────────────────────────────────────────

def test_member_of_technical_staff_is_not_staff_level():
    rf = RuleFilter(profile=_profile())
    assert rf.filter(_job("Member of Technical Staff (AI Software Engineer, Agent Engine)")).passed
    assert not rf.filter(_job("Staff Software Engineer, Applied AI")).passed


@pytest.mark.parametrize("title,desc", [
    ("AI Software Development Engineer", "Working alongside hardware architects, compiler "
     "engineers, and AI researchers, you will use performance models."),
    ("Sr. Software Engineer, Command & Control", "Proficiency in backend or systems programming "
     "languages such as Python and Rust."),
    ("3D Computer Vision Engineer", "Experience with good software development practices and "
     "proficiency in C++ and/or Python."),
    ("Forward Deployed AI Engineer", "Work on cutting-edge AI research and showcase your work at "
     "top conferences such as ICLR, ICML, and NeurIPS."),
    ("ML Engineer - Vision", "<h3>Preferred Qualifications</h3><ul><li>Proficiency in C++ for "
     "model integration.</li></ul>"),
    ("Senior ML Engineer - Email Security", "Systems programming:\n familiarity with Go or Rust, "
     "particularly for parsers."),
])
def test_a_mention_is_not_a_requirement(title, desc):
    assert RuleFilter(profile=_profile()).filter(_job(title, desc)).passed


@pytest.mark.parametrize("title,desc", [
    ("Compiler Engineer for AI DSLs", "As a compiler engineer, you will advance our compiler stack."),
    ("Rust Backend Architect", "Requirements:\nStrong hands-on proficiency in Rust."),
    ("Senior Software Engineer - IVI", "Strong C++ skills in resource-constrained, real-time, or "
     "embedded environments; solid Python."),
])
def test_a_stated_requirement_outside_the_stack_still_blocks(title, desc):
    res = RuleFilter(profile=_profile()).filter(_job(title, desc))
    assert not res.passed and res.reason.startswith("Hire-probability")


# ── refusals the detector used to miss ──────────────────────────────────────

@pytest.mark.parametrize("text", [
    "ITAR REQUIREMENTS: To conform to U.S. Government export regulations, applicant must be a "
    "(i) U.S. citizen or national, (ii) U.S. lawful, permanent resident (aka green card holder), "
    "(iii) Refugee under 8 U.S.C. 1157, or (iv) Asylee under 8 U.S.C. 1158.",
    "**Work Authorization: This role \ndoes not\n qualify for employer-sponsored work "
    "authorization. Nationwide does not participate in the STEM OPT Extension program.**",
    "The company will not support the STEM OPT I-983 Training Plan endorsement for this position.",
    "This position is <strong>not</strong> eligible for visa sponsorship.",
])
def test_refusals_in_lists_wraps_and_markup_are_caught(text):
    assert find_refusal(text) is not None
    assert find_refusal(text.lower()) is not None        # RuleFilter passes lowercased text


def test_a_wrapped_bullet_list_does_not_merge_into_a_refusal():
    text = "visa sponsorship\nis available for this role.\nno sponsorship restrictions apply"
    assert find_refusal(text) is None


# ── an ISO country code is not a US state ───────────────────────────────────

@pytest.mark.parametrize("site,country", [
    ("Heusweiler, SAARLAND, DE", "germany"),
    ("Zwickau, SAXONY, DE", "germany"),
    ("San Ġiljan, Europe, MT", "malta"),
    ("Wilmington, DE", "united states"),
    ("Dover, DE", "united states"),
    ("Kansas City, Missouri, MO", "united states"),
    ("Austin, TX, US", "united states"),
])
def test_city_region_code_addresses(site, country):
    assert detect_country(site) == country
    if country != "united states":
        assert detect_us_state(site) == ""


@pytest.mark.parametrize("site,state", [
    # From a random 300 of the shared pool: 1 in 25 was held "no location" for these.
    ("WI, Minocqua", "wi"), ("TX - Fort Worth", "tx"), ("OH-Twinsburg-Cardinal Health at-Home", "oh"),
    ("Dallas TX", "tx"), ("Tempe AZ", "az"), ("Orlando - FL", "fl"),
    ("Store 2723882 Charleston SC", "sc"), ("JD: 01246 Riverside, California (Galleria)", "ca"),
    ("Portland, Maine", "me"),
])
def test_ats_location_formats_read_as_us(site, state):
    assert detect_country(site) == "united states" and detect_us_state(site) == state


@pytest.mark.parametrize("site", [
    "Mexicali, Baja California", "Angers, Maine-et-Loire", "IN - Bangalore", "Bangalore IN",
    "Seneffe (BEL)", "ESP - MD - LAS ROZAS", "Midland, Georgia",
])
def test_foreign_or_ambiguous_places_are_not_us(site):
    assert detect_country(site) != "united states"


def test_a_structured_country_owns_its_code_in_the_sites():
    from app.discovery.geo_verify import derive
    raw = RawJob(source="smartrecruiters", external_id="pfa-geo", company="Acme",
                 title="Backend Engineer", location="Darmstadt, DE", remote=False,
                 url="https://x.co/j", description="Build APIs.",
                 geo=GeoEvidence(sites=["Darmstadt, DE"], country="DE"))
    assert derive(raw).countries == ["germany"]


# ── Tier-1 claims the gates already settled ─────────────────────────────────

def _t1(reason, *, eligibility="eligible", desc="Python, FastAPI and LangChain.", score=30.0,
        profile=None):
    job = _job("AI Engineer", desc, eligibility=eligibility, location="Chicago")
    return rr._overrule_cleared_blocker(score, reason, job, profile or _profile())


@pytest.mark.parametrize("reason", [
    "Job located outside United States; remote not permitted.",
    "Job based in Seattle, no remote option.",
    "Job requires on-site work in a different region than candidate's location.",
    "Job requires onsite presence and no sponsorship is mentioned.",
    "Location not stated for remote; visa sponsorship unclear.",
])
def test_a_settled_location_or_silent_sponsorship_claim_advances_at_the_gate(reason):
    score, why = _t1(reason)
    assert score == 40.0 and "overruled" in why


def test_claims_that_stand():
    # About fit, not place.
    assert _t1("Job is for a Senior Product Manager, not a technical role.")[0] == 30.0
    assert _t1("Job requires 5 years experience and is location specific.")[0] == 30.0
    # No verified location verdict to overrule with.
    assert _t1("Job located outside United States.", eligibility=None)[0] == 30.0
    # The posting's own words limit sponsorship.
    assert _t1("Explicit no-sponsorship stated in the posting.",
               desc="We are unable to sponsor visas for this role.")[0] == 30.0
    # A foreign place the posting itself names.
    assert _t1("Job located in Germany; not open to US remote applicants.",
               desc="Although this supports US work, this role is located in Wiesbaden, Germany.")[0] == 30.0
    # Already advancing: untouched.
    assert _t1("Job based in Seattle, no remote option.", score=55.0)[0] == 55.0


def test_a_sponsorship_claim_is_irrelevant_to_a_citizen():
    citizen = _profile(work_auth_status="Citizen", work_authorization="US Citizen")
    assert _t1("No sponsorship available.", desc="No sponsorship available.",
               profile=citizen)[0] == 40.0
    assert _t1("Requires US citizenship and clearance.", desc="Active TS/SCI clearance required.",
               profile=citizen)[0] == 30.0


def test_foreign_places_named():
    assert foreign_places_named("Job based in MEA region, not United States", "United States") == {"emea"}
    assert "germany" in foreign_places_named("located in Wiesbaden, Germany", "United States")
    assert foreign_places_named("Job is not remote and located in Chicago", "United States") == set()


def test_the_tier_one_prompt_no_longer_asks_the_model_to_judge_place():
    sp = rr._prescore_system_prompt(_profile())
    blocker = next(line for line in sp.split("\n- ") if line.startswith("0-30"))
    assert "country" not in blocker and "remote" not in blocker.lower()
    assert "Location and remote policy are NOT part of this score" in sp
    assert "does not mention sponsorship is NOT a refusal" in sp
    assert "years alone is never below 40" in sp
    prompt = rr._build_prescore_prompt("r", _job("AI Engineer", eligibility="eligible"))
    assert "Location check: passed for this candidate" in prompt
    assert "Remote:" not in prompt


# ── the final score answers to the posting's stated years and level ─────────
# From the same profile's 60-job board: every case below was shortlisted at
# 72-92 although the posting itself said otherwise.

def _final(desc, title="Software Engineer", score=72.0, **kw):
    return rr.reconcile_with_profile((score, "fit", ["gap"], {}), _profile(**kw), desc, title=title)


@pytest.mark.parametrize("title,desc", [
    ("Software Engineer, Senior", "Required Qualifications\nMinimum of 7 years of professional "
     "software development experience.\nAdvanced proficiency in PHP."),
    ("Senior Software Engineer, Portal Application", "6+ years of overall software development "
     "experience, preferably secure, scalable web applications with NodeJS, TypeScript, React."),
    ("Distinguished AI Engineer", "Requires a bachelor's degree in Computer Science and a minimum of "
     "15 years of experience in software engineering, or any combination of education and experience."),
])
def test_a_stated_gap_of_two_years_is_a_stretch_not_a_match(title, desc):
    score, _r, concerns, _b = _final(desc, title)
    assert score == rr.STRETCH_OVERALL_CAP
    assert concerns[0].startswith("Posting asks") and "about 4" in concerns[0]


@pytest.mark.parametrize("title,desc", [
    ("Junior AI Engineer - Magna International", "0–2 years of professional software development, "
     "data, automation, or AI experience. Programming experience with Python."),
    ("Associate Software Engineer", "Proficiency in at least one programming language, such as C# or Python."),
    ("Software Engineer, Cloud", "As a Software Engineer I, Cloud, you'll work on full-stack development."),
])
def test_an_entry_role_is_below_four_years(title, desc):
    score, _r, concerns, _b = _final(desc, title, score=92.0)
    assert score == rr.STRETCH_OVERALL_CAP and concerns[0].startswith("Entry-level role")


def test_what_is_left_alone():
    # "Senior …, 4+ years" asks for 4: no gap (the model had written "typically 7+").
    assert _final("You have 4+ years of experience designing backend systems in Python.",
                  "Senior Software Engineer, Backend (AI Platform)")[0] == 72.0
    # One year short is a judgement for the scorer, not a cap.
    assert _final("5+ years of experience in software engineering.")[0] == 72.0
    # A senior associate, an associate principal and an "I/II" opening are not entry roles.
    assert _final("Consulting delivery.", "AI Consulting Engineer - Senior Associate")[0] == 72.0
    assert _final("Lead .NET work.", "Associate Principal Engineer - Senior .NET")[0] == 72.0
    assert _final("Build APIs.", "Software Engineer I/II")[0] == 72.0
    assert _final("0-3 years of experience.", "Software Engineer", score=82.0)[0] == 82.0
    # A minimum a 4-year engineer meets is never "below level".
    assert _final("2+ years of professional software engineering experience.", score=82.0)[0] == 82.0
    assert _final("1-2 years of experience with Python and REST APIs.", score=82.0)[0] == 82.0
    # An entry role is not "below" a candidate who is starting out.
    assert _final("0–2 years of experience.", "Junior AI Engineer", score=88.0,
                  years_experience=1)[0] == 88.0


def test_the_final_scorer_reads_years_as_stated_and_alternatives_as_alternatives():
    sp = rr._profile_system_prompt(_profile())
    assert "Judge years ONLY against a number the posting STATES" in sp
    assert '"Python or Go"' in sp and "met by any one of them" in sp
    assert "PRIMARY language or platform" in sp


def test_the_board_holds_strong_matches_only():
    from app.config import settings
    assert settings.shortlist_score_threshold == 75 > rr.STRETCH_OVERALL_CAP


def test_the_right_door_card_is_gone():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "app" / "templates"
    for name in ("dashboard.html", "_board_macros.html"):
        text = (root / name).read_text()
        assert "Right-Door Check" not in text and "autopsy-placeholder" not in text


# ── the one-time re-open of jobs these gates had drained ────────────────────

UID = "pfa_user"
_P = "pfa_"


def _clean():
    from app.db.models import Application
    from app.matching.pipeline import _PREFILTER_FIX_MARKER
    with get_session() as s:
        s.exec(delete(Application).where(Application.user_id == UID))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.exec(delete(FunnelEvent).where(FunnelEvent.stage == _PREFILTER_FIX_MARKER,
                                         FunnelEvent.reason == f"user={UID}"))
        s.commit()


@pytest.fixture()
def stamped():
    _clean()
    now = datetime.utcnow()
    rows = {
        "door": ("Applied AI Engineer", "Python, FastAPI, LangChain. Benefits: dental insurance.",
                 "Wrong Door: This position requires a **enterprise** background", now, "eligible"),
        "range": ("Senior Full-Stack Engineer", "6–8 years of professional software engineering experience.",
                  "Pre-screened (Tier-1 fit 15): Experience pre-filtered: requires 8+ years", now, "eligible"),
        "gap": ("Senior Software Engineer", "10+ years of professional software development experience.",
                "Pre-screened (Tier-1 fit 15): Experience pre-filtered: requires 10+ years", now, "eligible"),
        "place": ("AI Engineer", "Python and LangChain.",
                  "Pre-screened (Tier-1 fit 30): Job located outside United States; remote not permitted.",
                  now, "eligible"),
        "fit": ("Product Manager, AI", "Own the roadmap.",
                "Pre-screened (Tier-1 fit 30): Job is for a Product Manager, not a technical role.",
                now, "eligible"),
        "old": ("Applied AI Engineer", "Python.", "Wrong Door: enterprise", now - timedelta(days=20),
                "eligible"),
    }
    ids = {}
    with get_session() as s:
        for key, (title, desc, reason, seen, elig) in rows.items():
            j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + key, company="Acme",
                    title=title, url=f"https://x/{key}", description=desc, location="Chicago, IL",
                    eligibility=elig, rerank_score=15.0, rerank_reasoning=reason, prescore=15.0,
                    scored_at=seen, first_seen=seen)
            s.add(j)
            s.commit()
            s.refresh(j)
            ids[key] = j.id
    yield ids
    _clean()


def test_recent_jobs_the_new_gates_keep_are_reopened_once(stamped):
    from app.matching.pipeline import _reopen_misjudged_prefilter_stamps
    prof = _profile(user_id=UID)
    assert _reopen_misjudged_prefilter_stamps(UID, prof) == 3
    with get_session() as s:
        got = {k: s.get(Job, i) for k, i in stamped.items()}
        for key in ("door", "range", "place"):
            assert got[key].rerank_score is None and got[key].prescore is None, key
            assert got[key].scored_at is None
        for key in ("gap", "fit", "old"):
            assert got[key].rerank_score == 15.0, key
        marker = s.exec(select(FunnelEvent).where(FunnelEvent.reason == f"user={UID}")).all()
        assert len(marker) == 1
    assert _reopen_misjudged_prefilter_stamps(UID, prof) == 0      # once per user


def test_the_current_shortlist_is_held_to_its_postings(stamped):
    """Jobs already on the board kept scores written before the caps existed."""
    from app.db.models import Application, ApplicationStatus
    from app.matching.pipeline import _recheck_shortlisted_against_posting
    seeds = {
        "junior": ("Junior AI Engineer", "0–2 years of professional software development experience.", 92.0),
        "gap": ("Software Engineer, Senior", "Minimum of 7 years of professional software development experience.", 72.0),
        "refusal": ("AI Software Engineer", "U.S. Citizenship with eligibility for DoD Secret clearance.", 72.0),
        "fits": ("Senior Software Engineer, Backend (AI Platform)",
                 "You have 4+ years of experience designing backend systems in Python.", 72.0),
    }
    ids = {}
    with get_session() as s:
        for key, (title, desc, score) in seeds.items():
            j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + "sl_" + key,
                    company="Acme", title=title, url=f"https://x/sl/{key}", description=desc,
                    location="Chicago, IL", rerank_score=score,
                    rerank_reasoning="Strong fit.\nConcerns: something", first_seen=datetime.utcnow())
            s.add(j)
            s.commit()
            s.refresh(j)
            s.add(Application(job_id=j.id, user_id=UID, status=ApplicationStatus.SHORTLISTED))
            ids[key] = j.id
        s.commit()
    try:
        with get_session() as s:
            assert _recheck_shortlisted_against_posting(s, UID, _profile(user_id=UID)) == 3
            s.commit()
        with get_session() as s:
            got = {k: s.get(Job, i) for k, i in ids.items()}
            assert got["junior"].rerank_score == rr.STRETCH_OVERALL_CAP
            assert "Concerns: Entry-level role" in got["junior"].rerank_reasoning
            assert got["gap"].rerank_score == rr.STRETCH_OVERALL_CAP
            assert got["refusal"].rerank_score == rr.BLOCKER_OVERALL_CAP
            assert got["fits"].rerank_score == 72.0
            apps = s.exec(select(Application).where(Application.user_id == UID)).all()
            assert {a.status for a in apps} == {ApplicationStatus.SHORTLISTED}   # nothing removed
    finally:
        with get_session() as s:
            s.exec(delete(Application).where(Application.user_id == UID))
            s.commit()


def test_no_profile_reopens_nothing(stamped):
    from app.matching.pipeline import _reopen_misjudged_prefilter_stamps
    assert _reopen_misjudged_prefilter_stamps(UID, None) == 0
