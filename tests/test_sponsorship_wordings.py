"""A posting that refuses sponsorship is recognised however it is worded.

Production, 2026-11-24: a posting saying "will not provide sponsorship for
employment visas or participate in STEM OPT" was scored 88 and reached the
board of someone on F-1 OPT. The scorer had even written "explicitly states no
visa sponsorship" — but only the deterministic reader (`find_refusal`) may cap
a score or drop a job, and it did not know that sentence.

It was not one sentence. Every line naming sponsorship or OPT beside a negation
was pulled from one user's pool (4,000 postings): the reader knew 27 of 50
distinct refusals. 92% of what the product delivered that month went to people
who need sponsorship, so each miss was a paid score on a job that says no, and
a board entry whenever the scorer did not hold itself back.

An employer has unlimited wordings and five things to say, so the reader now
matches the five (app/common/sponsorship_text.py): the verb "sponsor" negated;
sponsorship not offered; sponsorship not available; work "without …
sponsorship"; people who need it not wanted. This file is the corpus — the
refusals, and the look-alikes that must stay eligible, because a false hit
here drops a job the person could take before anyone scores it.

Wording as it appears in real postings; employer names replaced.
"""
from __future__ import annotations

import pytest

from app.common.sponsorship_text import find_refusal

REFUSALS = [
    "(No visa sponsorship)",
    "No sponsorship no agencies",
    "No relocation or visa sponsorship",
    "This job is not eligible for employment sponsorship",
    "This position is not eligible for visa sponsorship",
    "This role is not eligible for immigration sponsorship",
    "Ineligible for visa sponsorship",
    "This position is not eligible for employer-sponsored work authorization, either at the time of hire or thereafter",
    # sponsorship is not available
    "Visa sponsorship is not available",
    "New visa sponsorship is not available",
    "Visa sponsorship for this role is currently not available",
    "Visa sponsorship and visa transfers are not available",
    "Current and future sponsorship are not available for this position",
    "Please note that sponsorship of new applicants for employment authorization, or any other "
    "immigration-related support, is not available for this position",
    # the verb, negated
    "At this time, Acme cannot transfer nor sponsor a work visa for this position",
    "At this time, Acme will not sponsor a new applicant for employment authorization, or offer any "
    "immigration related support for this position",
    "Acme is unable to sponsor or assist with U.S. work visas",
    "Acme is not currently sponsoring candidates requiring work authorization support for this position",
    "Please note that at this time, Acme is not sponsoring U.S. work visas",
    "We do not sponsor or take over sponsorship of employment visas (now or in the future) for this role",
    "We’re unable to sponsor or take over sponsorship of employment visas at this time",
    "Acme does not sponsor US F1 or H-1B work visas",
    "Acme does not sponsor a new applicant for employment authorization or immigration related support "
    "for this position",
    "We will not sponsor individuals for employment visas, now or in the future, for this job opening",
    "Visa Sponsorship: Employer will not sponsor applicants for employment visa status",
    # sponsorship is not offered
    "Acme will not provide sponsorship at this time",
    "Acme will not provide sponsorship for employment visas or participate in STEM OPT for this position",
    "Note: Acme will not be providing visa sponsorship for this position now or in the future",
    "Acme does not provide sponsorship for employment-based work visas for this position",
    "We do not provide new work visa sponsorship",
    "We don't offer visa sponsorship",
    "We do not offer relocation assistance or visa sponsorship",
    "Acme does not offer support or sponsorship of job applicants for employment-based visas or any "
    "other work authorization for this role now or in the future",
    "We are unable to provide visa sponsorship or support visa transfers",
    "We’re unable to provide visa sponsorship at this time, but we welcome applications from all "
    "qualified candidates who are legally authorized to work in these countries",
    # work without sponsorship
    "Applicants must be authorized to work directly for any employer in the United States without visa sponsorship",
    "Legally authorized to work for any company in the United States without sponsorship",
    "Duration: approximately a 12-month consulting role (must be able to work on Acme's W2 without sponsorship)",
    "This position requires candidates to be authorized to work in the US on an ongoing basis without "
    "current or future employer sponsorship",
    "without the need for sponsorship now or in the future",
    "Therefore, in order to be considered for this position, you must have the ability to work in the "
    "United States without a need for current or future visa sponsorship",
    "Applicants must be legally authorized to work in the United States for any employer without "
    "requiring current or future visa sponsorship (for example, employment-based visas such as H-1B, "
    "F-1/OPT, or similar)",
    "This role is based in the US and requires current authorization to work in the United States "
    "without the need for employer sponsorship",
    # people who need it are not wanted
    "Applicants for employment in the US must have work authorization that does not now or in the "
    "future require sponsorship of a visa for employment authorization in the United States",
    "You must have the right to work in the United States and not require Acme support or sponsorship "
    "for immigration-related employment",
    "You should not apply for this role if you will require Acme to assist with immigration support or "
    "sponsorship now or in the future",
    "We are not accepting candidates who require sponsorship for this role at this time",
    "Candidates who require Acme sponsorship for an employment visa, now or in the future, will not be "
    "considered for this role",
    # OPT / CPT / H-1B named instead of the word
    "Please note: we are unable to support H1-B or STEM OPT candidates at this time",
    "We are unable to sponsor or transfer H-1B or other employment-based visas, and candidates "
    "currently on F-1, OPT, STEM OPT status are not eligible for this role",
    "The employer also does not provide employer participation or documentation required for STEM OPT "
    "extensions for this position",
    "Candidates on OPT or CPT are not eligible for this role",
    "No OPT/CPT",
    # citizens or residents only
    "US citizenship or green card only - we do not offer sponsorship",
    "US citizens and green card holders only",
]

NOT_REFUSALS = [
    # The employer sponsors.
    "We offer visa sponsorship for this role",
    "Visa sponsorship is available for exceptional candidates",
    "We will sponsor H-1B transfers and do not require prior US experience",
    "Acme participates in E-Verify and supports STEM OPT extensions",
    "We do not offer relocation, but we do support STEM OPT and H-1B transfers",
    "We don't provide lunch, but we do provide visa sponsorship",
    "Visa sponsorship is available for this role, however relocation assistance is not provided",
    "We offer visa sponsorship, though remote work is not available",
    "If you are not a US citizen, we sponsor visas for this role",
    # A hedge: this employer sponsors, case by case.
    "However, we aren't able to successfully sponsor visas for every role and every candidate",
    "We can't guarantee sponsorship for every role, but if we make you an offer we'll make every "
    "reasonable effort, with help from our immigration law firm",
    "Sponsorship eligibility is assessed on an individual basis and is not guaranteed for any role or candidate",
    # The opposite of a refusal, or a question.
    "There is no sponsorship requirement for this role",
    "We consider candidates with or without sponsorship",
    "We hire without regard to visa status or sponsorship needs",
    "Please ask your recruiter whether or not we sponsor visas for this role",
    "Not sure if you need sponsorship? Talk to us",
    # Other things that are sponsored, opted into, or reserved.
    "Company-sponsored 401k + matching (US only)",
    "No clear data mapping and no named business sponsor means no prioritization",
    "Executive sponsor engagement is not required for this project",
    "This is not a sponsored post",
    "If you would prefer not to have your application processed using this tool, you may opt out by "
    "selecting 'opt out' in the application form",
    "If an interview will be recorded, you will be notified in advance and may opt out at any time "
    "with no impact on your candidacy",
    "Candidates who prefer not to have AI notetaking used may request to opt out without impact to your candidacy",
    "We note that phone numbers and SMS opt-in data are not shared, sold, or rented to third parties",
    "You may opt in to SMS updates; we will not share your number",
    "consider the candidate for alternative positions unaffected by such restrictions, or, as an "
    "alternative, opt not to proceed with the candidate's application",
    # Citizenship named without limiting who may apply.
    "Citizenship status is used only to verify employment eligibility through E-Verify",
    "We are an equal opportunity employer and do not discriminate on the basis of citizenship or national origin",
    "Must be authorized to work in the United States",
]


@pytest.mark.parametrize("sentence", REFUSALS)
def test_a_refusal_is_read_as_one(sentence):
    hit = find_refusal(sentence)
    assert hit is not None, "a posting that refuses sponsorship was read as silent"
    assert hit.sentence, "the refusal must be quotable"
    # The rule filter passes lowercased text; the answer cannot depend on case.
    assert find_refusal(sentence.lower()) is not None


@pytest.mark.parametrize("sentence", NOT_REFUSALS)
def test_a_look_alike_stays_eligible(sentence):
    hit = find_refusal(sentence)
    assert hit is None, f"read as a refusal ({hit.phrase!r}) — this job would be dropped unscored"
    assert find_refusal(sentence.lower()) is None


def test_the_refusal_is_found_inside_a_whole_posting():
    """Sentence by sentence: the refusal counts wherever it sits, and a posting
    that sponsors is not refused by an unrelated "not" elsewhere."""
    refuses = ("About the role\nBuild APIs in Python.\n\nRequirements\n• 3+ years of experience\n"
               "• Strong SQL\n\nAcme will not provide sponsorship for employment visas or participate "
               "in STEM OPT for this position. In lieu of a degree, a comparable combination of "
               "education and experience may be considered.")
    hit = find_refusal(refuses)
    assert hit is not None and "will not provide sponsorship" in hit.sentence.lower()

    sponsors = ("About the role\nBuild APIs in Python. This is not a remote position.\n\n"
                "We offer visa sponsorship and relocation support. Benefits are not available to "
                "contractors.")
    assert find_refusal(sponsors) is None


def test_the_score_is_capped_for_someone_who_needs_sponsorship():
    """End to end through the final check: the 88 becomes the blocker cap for
    an F-1 OPT user and stays 88 for a citizen."""
    from app.db.models import UserProfile
    from app.matching import reranker as rr
    posting = ("Software Engineer - AI Agents. Python, LangChain, REST APIs.\n"
               "Acme will not provide sponsorship for employment visas or participate in STEM OPT "
               "for this position.")
    base = dict(user_id="spw-user", years_experience=4, key_skills="Python, LangChain",
                target_roles="Software Engineer", preferred_country="United States")
    opt = UserProfile(**base, work_auth_status="OPT", work_authorization="F-1 OPT")
    citizen = UserProfile(**base, work_auth_status="Citizen", work_authorization="US Citizen")
    said = (88.0, "Strong AI/GenAI match", [], {"work_auth": {"score": 15, "note": "no sponsorship"}})

    score, _r, concerns, _b = rr.reconcile_with_profile(said, opt, posting, title="Software Engineer")
    assert score == rr.BLOCKER_OVERALL_CAP
    assert concerns[0].startswith("Posting rules this out")
    assert rr.reconcile_with_profile(said, citizen, posting, title="Software Engineer")[0] == 88.0
