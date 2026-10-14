"""What a fit report says must agree with what we already know.

Live test on the owner's account, 2026-10-09 (an F-1 OPT user whose profile
says they will need H-1B sponsorship; ids and names below are synthetic):

1. A report read "Work auth 100% · F-1 OPT, no sponsorship needed" (scored
   2026-08-07, before the visa status decided sponsorship); a fresh one read
   "Work auth 10% · F-1 OPT; posting silent on sponsorship" although the
   rubric scores a silent posting HIGH.
2. "candidate shows 0 years explicitly" sat next to "3 years backend
   engineering": zero years of ONE skill, written as the candidate's years.
3. A July report cited experience the current (August) resume no longer has.
4. One Miratech role, posted once per city with identical text, was on the
   board twice (New York 72, Miami 78).
5. A Stripe card read "na · On-site": the ATS's placeholder shown as a place.
6. The referral and public profile links started with http:// (Railway's TLS
   proxy makes request.base_url say http).

Every row this file writes carries the ``mc_`` prefix and is deleted by it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import (Application, ApplicationStatus, FunnelEvent, Job, JobSource,
                           UserProfile)

_P = "mc_"
UID = "mc_user"


def _opt(**kw):
    base = dict(work_authorization="F-1 OPT", requires_sponsorship=True, visa_status="",
                work_auth_status="OPT", ead_end_date="", preferred_country="United States")
    base.update(kw)
    return SimpleNamespace(**base)


def _citizen():
    return SimpleNamespace(work_authorization="", requires_sponsorship=False, visa_status="",
                           work_auth_status="Citizen", ead_end_date="",
                           preferred_country="United States")


@pytest.fixture
def clean():
    def _wipe():
        with get_session() as s:
            jids = [r for r in s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
            if jids:
                s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
                s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(Application).where(Application.user_id.like(f"{_P}%")))
            s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
            s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_P}%")))
            s.commit()
    _wipe()
    yield
    _wipe()


# ── 1. Work authorization: the profile decides ───────────────────────────────

def test_an_opt_student_is_never_reported_as_needing_no_sponsorship():
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    out = fix({"score": 100, "note": "F-1 OPT, no sponsorship needed"}, _opt(), False)
    assert "no sponsorship needed" not in out["note"].lower()
    assert "will need sponsorship" in out["note"] and "F-1 OPT" in out["note"]
    assert out["score"] <= 85, "a silent posting is a good fit for OPT, not a perfect one"
    # The July wording, with no posting text to judge refusal from.
    out = fix({"score": 100, "note": "Candidate is OPT, US-based, no sponsorship needed. "
                                     "Fully authorized to work in United States."}, _opt(), None)
    assert "no sponsorship" not in out["note"].lower() and "fully" not in out["note"].lower()
    # Other ways the model says it.
    for note in ("Candidate does not need sponsorship.", "doesn't require visa sponsorship",
                 "sponsorship not required"):
        assert "will need sponsorship" in fix({"score": 100, "note": note}, _opt(), False)["note"]


def test_a_refusing_posting_caps_the_factor_for_a_student():
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    out = fix({"score": 100, "note": "no sponsorship needed"}, _opt(), True)
    assert out["score"] <= 15 and "posting refuses" in out["note"]
    assert fix({"score": 90, "note": "sponsorship possible"}, _opt(), True)["score"] <= 15


_SILENT_POSTING = "Build backend services in Python and Go. 3+ years of API work."


def test_a_silent_posting_is_not_a_blocker_for_a_student():
    """The rubric says silent = high; the model wrote 10 next to 'silent'."""
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    out = fix({"score": 10, "note": "F-1 OPT; posting silent on sponsorship but US role"},
              _opt(), False, _SILENT_POSTING)
    assert out["score"] >= 75
    # ...unless the note names a real restriction, or the posting refuses.
    assert fix({"score": 10, "note": "silent on visas; requires US citizenship"},
               _opt(), False, _SILENT_POSTING)["score"] == 10
    assert fix({"score": 10, "note": "posting silent on sponsorship"}, _opt(), True,
               _SILENT_POSTING)["score"] == 10
    # Unknown posting text is never taken as "silent".
    assert fix({"score": 10, "note": "posting silent on sponsorship"}, _opt(), None)["score"] == 10
    assert fix({"score": 10, "note": "posting silent on sponsorship"}, _opt(), False)["score"] == 10


# Review 2026-10-09: four postings `find_refusal` reads as silent, each with a
# note the old rule lifted from a blocker to 85 "posting silent on it".
_RESTRICTED = {
    "tssci": ("Build backend services in Java. Requirements: Active TS/SCI clearance with "
              "full-scope polygraph. 3+ years Java.",
              "F-1 OPT; sponsorship not mentioned; TS/SCI with poly needed"),
    "usc": ("Build backend services. Applicants must be U.S. citizens due to federal "
            "contract requirements.",
            "Posting silent on visas; federal contract role"),
    "secret": ("Must be able to obtain and maintain a Secret clearance. Python, AWS.",
               "Silent on sponsorship; Secret eligibility required"),
    "dod": ("U.S. Citizenship is required for this position.",
            "F-1 OPT; posting does not mention sponsorship, U.S. person role"),
}


@pytest.mark.parametrize("case", sorted(_RESTRICTED))
def test_a_clearance_or_citizenship_role_is_never_lifted_as_silent(case):
    from app.matching.reranker import reconcile_with_profile
    description, note = _RESTRICTED[case]
    factor = {"score": 5, "note": note}
    out = reconcile_with_profile((60.0, "x", [], {"work_auth": dict(factor)}), _opt(), description)
    assert out[3]["work_auth"] == factor, "the model's blocker stands, in its own words"


#: Review round 3: requirements the first guard missed. The "United States"
#: spelling, and negations that describe the APPLICANT ("who are not U.S.
#: citizens") rather than lift the requirement ("is not required").
#: Review round 4: requirements worded with "status" (the round-3 exception
#: for the EEO phrase dropped them), a clearance negation beside a citizenship
#: requirement (a negated clause cancelled the whole sentence), and EEO-like
#: words ("based on", "protected") inside a real requirement.
_STATED_IN_THE_POSTING = [
    "Must be a United States citizen.",
    "Only United States citizens may apply.",
    "Applicants who are not U.S. citizens will not be considered.",
    "We cannot hire anyone who is not a U.S. citizen.",
    "Candidates who do not hold U.S. citizenship are not eligible for this role.",
    "Non-citizens are not eligible.",
    "Active Secret required.",
    "U.S. citizenship is not required, but you must hold an active clearance.",
    "U.S. citizenship status is required.",
    "Requires U.S. citizenship status.",
    "Must have U.S. citizen status due to contract requirements.",
    "US Citizen status required.",
    "Only United States citizens may apply, no clearance needed.",
    "U.S. Citizens only, clearance not required.",
    "U.S. citizenship is required, but no security clearance is needed.",
    "Requires U.S. citizenship (no clearance required).",
    "Active Secret clearance not required at start, U.S. citizenship required.",
    "U.S. citizenship is required based on federal contract requirements.",
    "U.S. citizenship required to access protected program data.",
    "Green card holders only.",
    "Must be a lawful permanent resident.",
]


@pytest.mark.parametrize("description", [_RESTRICTED[c][0] for c in sorted(_RESTRICTED)]
                         + [_SILENT_POSTING + " " + t for t in _STATED_IN_THE_POSTING])
def test_the_posting_text_alone_stops_the_lift(description):
    """A note that names nothing is not enough when the POSTING states it."""
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    out = fix({"score": 5, "note": "posting silent on sponsorship"}, _opt(), False, description)
    assert out["score"] == 5 and "model_note" not in out


@pytest.mark.parametrize("note", [
    "posting silent on sponsorship; TS/SCI role", "silent; Secret level access",
    "not mentioned; polygraph", "silent on visas; U.S. persons only (ITAR)",
    "silent; federal agency", "silent; government contractor", "silent; defense program",
    "silent; export control applies"])
def test_a_note_naming_a_clearance_or_government_restriction_is_kept(note):
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    assert fix({"score": 5, "note": note}, _opt(), False, _SILENT_POSTING)["score"] == 5


#: E-Verify and EEO lines a silent posting routinely carries (review round 3:
#: each made rule (c) read the posting as a citizenship requirement). These are
#: the ONLY text removed before the conservative "any mention" rule.
_E_VERIFY = "We participate in E-Verify, a program of U.S. Citizenship and Immigration Services."
_EEO_US = "We hire without regard to race, national origin, or U.S. citizenship status."
_EEO_FULL = ("All qualified applicants will receive consideration for employment without "
             "regard to race, color, religion, sex, national origin, disability, protected "
             "veteran status, citizenship status, or any other characteristic protected by law.")


@pytest.mark.parametrize("text", [
    "We do not discriminate on the basis of race, national origin or citizenship status.",
    _E_VERIFY, _EEO_US, _EEO_FULL,
    "E-Verify is run by United States Citizenship and Immigration Services (USCIS).",
    "E-Verify is a program of Citizenship and Immigration Services.",
    "We use E-Verify (U.S. Citizenship & Immigration Services).",
    "Acme is an equal opportunity employer and hires regardless of citizenship.",
    "Decisions are based on merit, not on citizenship status or national origin.",
    "No employee is treated differently because of a protected trait such as citizenship.",
    "We sponsor visas for the right candidate."])
def test_boilerplate_is_not_a_restriction(text):
    from app.intelligence.work_auth import posting_restriction
    assert posting_restriction(_SILENT_POSTING + " " + text) is None


def test_a_silent_posting_with_e_verify_boilerplate_is_still_silent():
    """The live MC1 case: E-Verify is what STEM OPT asks of an employer, not a
    citizenship requirement."""
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    factor = {"score": 10, "note": "F-1 OPT; posting silent on sponsorship but US role"}
    for extra in (_E_VERIFY, _EEO_US, _EEO_FULL, _E_VERIFY + " " + _EEO_US):
        out = fix(dict(factor), _opt(), False, _SILENT_POSTING + " " + extra)
        assert out["score"] == 85 and out["model_score"] == 10, extra


#: Review round 4: negated or waived mentions. Every attempt to read negation
#: ("is not required", "no ... needed", "need not be") failed in one direction
#: or the other, so the rule is deliberately conservative: ANY mention of
#: citizenship, a clearance, U.S.-person status, a green card or permanent
#: residence means the posting is not silent. The cost is that these keep the
#: model's own (low) score instead of the 85 lift; the alternative was lifting
#: citizens-only roles to 85 for a student who cannot take them.
_NEGATED_MENTIONS = [
    "No U.S. citizenship or clearance required.",
    "No clearance or U.S. citizenship needed.",
    "Neither U.S. citizenship nor a security clearance is required.",
    "There is no requirement for U.S. citizenship.",
    "You need not be a U.S. citizen.",
    "You're not required to hold a clearance.",
    "Applicants not required to be U.S. citizens.",
    "We welcome non-citizens and sponsor visas.",
    "U.S. citizenship is not required.", "You do not need to be a U.S. citizen to apply.",
    "This role does not require a security clearance.",
    "No security clearance needed.", "Clearance not required.",
    "You are not required to be a United States citizen.",
    "U.S. citizenship isn't a requirement for this role.",
    "We sponsor visas and green cards for the right candidate.",
    "Employment decisions never depend on United States citizen status.",
]


@pytest.mark.parametrize("text", _NEGATED_MENTIONS)
def test_a_negated_mention_also_stops_the_lift(text):
    from app.intelligence.work_auth import posting_restriction
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    assert posting_restriction(_SILENT_POSTING + " " + text) is not None
    factor = {"score": 10, "note": "F-1 OPT; posting silent on sponsorship but US role"}
    out = fix(dict(factor), _opt(), False, _SILENT_POSTING + " " + _E_VERIFY + " " + text)
    assert out == factor, "the model's own verdict stands; nothing is lifted"


def test_only_the_eeo_clause_loses_its_citizenship_mention():
    """The EEO exception is per sentence: a requirement beside EEO text is
    still read, and an EEO sentence that also states a requirement is kept."""
    from app.intelligence.work_auth import posting_restriction
    assert posting_restriction(_EEO_FULL + " U.S. citizenship status is required.") \
        == "U.S. citizenship status is required"
    assert posting_restriction(
        "We do not discriminate based on citizenship, but U.S. citizenship is required "
        "for this contract.") is not None


def test_a_correction_keeps_the_models_own_verdict():
    """The fix runs before the row is stored: what the model said must survive
    it, and a later read re-derives from the model's words, not the fix."""
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    model = {"score": 10, "note": "F-1 OPT; posting silent on sponsorship"}
    out = fix(dict(model), _opt(), False, _SILENT_POSTING)
    assert out["score"] == 85 and "posting silent on it" in out["note"]
    assert out["model_score"] == 10 and out["model_note"] == model["note"]
    stored = json.loads(json.dumps(out))
    assert fix(stored, _opt(), False, _SILENT_POSTING) == out, "idempotent on read"
    # The same stored row read against a posting now known to need a clearance
    # (or a stricter rule): the model's own verdict comes back, not the lift.
    back = fix(stored, _opt(), False, "Active TS/SCI clearance required.")
    assert back == model
    # A profile that no longer needs sponsorship gets the scorer's own words too.
    assert fix(stored, _citizen(), False, _SILENT_POSTING) == model
    # The "no sponsorship needed" correction keeps the original as well.
    out = fix({"score": 100, "note": "F-1 OPT, no sponsorship needed"}, _opt(), False,
              _SILENT_POSTING)
    assert out["model_note"] == "F-1 OPT, no sponsorship needed" and out["model_score"] == 100


def test_a_stored_verdict_keeps_the_models_note_through_the_report(clean, monkeypatch):
    """Parse time stores both; the report shows the correction."""
    from app.api import server
    from app.matching.reranker import reconcile_with_profile
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    with get_session() as s:
        s.add(UserProfile(user_id=UID, work_authorization="F-1 OPT", work_auth_status="OPT",
                          requires_sponsorship=True))
        s.commit()
    _s, _r, _c, bd = reconcile_with_profile(
        (70.0, "Fine.", [], {"work_auth": {"score": 10, "note": "posting silent on sponsorship"}}),
        _opt(), "Build backend services in Python.")
    assert bd["work_auth"]["model_score"] == 10
    aid = _seed_scored("keep", reasoning="Fine.", breakdown=bd, scored_at=datetime.utcnow(),
                       app_created=datetime.utcnow())
    d = _client().get(f"/application/{aid}/match").json()
    assert d["breakdown"]["work_auth"]["score"] == 85
    assert "posting silent on it" in d["breakdown"]["work_auth"]["note"]


def test_a_citizen_keeps_the_scorers_own_words():
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    f = {"score": 100, "note": "no sponsorship needed"}
    assert fix(f, _citizen(), False) == f


def test_the_scorer_is_told_both_halves_of_opt():
    from app.matching.reranker import _profile_system_prompt
    p = SimpleNamespace(**vars(_opt()), years_experience=3, key_skills="python",
                        target_roles="Backend Engineer", current_title="", remote_ok=True,
                        professional_summary="")
    prompt = _profile_system_prompt(p)
    assert "is authorized to work now but WILL need visa sponsorship" in prompt
    assert "Never write that the candidate needs no sponsorship" in prompt


class _OpenAIText:
    def __init__(self, text):
        self.text = text
        self.chat = self
        self.completions = self

    def create(self, **kw):
        usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5,
                               "prompt_tokens_details": None})()
        msg = type("M", (), {"content": self.text})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()],
                              "usage": usage, "model": "gpt-4o-mini"})()


def test_a_fresh_verdict_is_stored_already_reconciled(monkeypatch):
    """The fix runs where the verdict is parsed, so new rows never carry it."""
    import app.analytics.spend as spend
    import app.matching.reranker as rr
    from app.config import settings
    monkeypatch.setattr(settings, "dual_score_enabled", False)
    verdict = {"score": 72, "reason": "Strong backend and API skills",
               "concerns": ["Requires 1+ year building production integrations; "
                            "candidate shows 0 years explicitly"],
               "breakdown": {"skills": {"score": 75, "note": "Python strong"},
                             "experience": {"score": 65, "note": "3 years backend engineering"},
                             "location": {"score": 100, "note": "US remote"},
                             "work_auth": {"score": 100, "note": "F-1 OPT, no sponsorship needed"}}}
    rk = rr.Reranker.__new__(rr.Reranker)
    rk._profile, rk._feedback, rk._user_id = _opt(), "", f"{_P}rk"
    rk._anthropic_client, rk._openai_client = None, _OpenAIText(json.dumps(verdict))
    rk._active_backend = "openai"
    rk._pre_filter_job = lambda job: None
    job = Job(title="Software Engineer, Integrations", company="H3", location="US, Remote",
              remote=True, description="Build integrations in Python.",
              source=JobSource.ASHBY, external_id=f"{_P}rk1", url="https://x/rk1")
    try:
        score, reason, concerns, breakdown = rk.score("resume", job)
    finally:
        spend._BUFFER.clear()
    assert score == 72, "the overall score is the model's; only the notes are held to facts"
    assert "no sponsorship needed" not in breakdown["work_auth"]["note"]
    assert not any("0 years" in c for c in concerns)
    assert "resume shows none" in concerns[0]


# ── 2. "0 years" ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Requires 1+ year building production integrations; candidate shows 0 years explicitly",
     "Requires 1+ year building production integrations; resume shows none explicitly"),
    ("requires Go; candidate has 0 years of Go experience", "requires Go; resume shows no Go"),
    ("candidate has ~3 years experience", "candidate has ~3 years experience"),
    ("Requires 0-2 years", "Requires 0-2 years"),
])
def test_a_skill_gap_is_never_written_as_zero_years(text, expected):
    from app.matching.reranker import tidy_years_claims
    assert tidy_years_claims(text) == expected


# ── the report endpoint applies both to rows already stored ─────────────────

def _client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _seed_scored(ext, *, reasoning, breakdown, scored_at, app_created, location="Remote, US",
                 description="Build backend services in Python."):
    with get_session() as s:
        j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + ext, company="Accel",
                title="Senior Software Engineer", location=location, remote=False,
                url=f"https://x/{ext}", description=description, rerank_score=70,
                rerank_reasoning=reasoning, rerank_breakdown=json.dumps(breakdown),
                scored_at=scored_at, first_seen=app_created)
        s.add(j)
        s.commit()
        s.refresh(j)
        a = Application(job_id=j.id, user_id=UID, status=ApplicationStatus.SHORTLISTED,
                        created_at=app_created)
        s.add(a)
        s.commit()
        s.refresh(a)
        return a.id


def test_the_report_holds_stored_notes_to_the_profile(clean, monkeypatch):
    from app.api import server
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    with get_session() as s:
        s.add(UserProfile(user_id=UID, work_authorization="F-1 OPT", work_auth_status="OPT",
                          requires_sponsorship=True,
                          resume_uploaded_at=datetime.utcnow() - timedelta(days=50)))
        s.commit()
    aid = _seed_scored(
        "stored", reasoning="Strong overlap.\nConcerns: candidate shows 0 years explicitly; needs VLMs",
        breakdown={"work_auth": {"score": 100, "note": "F-1 OPT, no sponsorship needed"},
                   "experience": {"score": 60, "note": "3 years, role asks for more"}},
        scored_at=None, app_created=datetime.utcnow() - timedelta(days=60), location="na")
    d = _client().get(f"/application/{aid}/match").json()
    assert "no sponsorship needed" not in d["breakdown"]["work_auth"]["note"]
    assert d["breakdown"]["work_auth"]["score"] <= 85
    assert "0 years" not in d["reason"] and "needs VLMs" in d["reason"]
    assert d["location"] == "", "a placeholder is no location"
    # Scored (delivered) 60 days ago, resume uploaded 50 days ago.
    assert d["from_previous_resume"] is True and d["resume_uploaded_at"]


def test_a_report_scored_after_the_upload_is_current(clean, monkeypatch):
    from app.api import server
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    now = datetime.utcnow()
    with get_session() as s:
        s.add(UserProfile(user_id=UID, resume_uploaded_at=now - timedelta(days=5)))
        s.commit()
    aid = _seed_scored("current", reasoning="Fine.", breakdown={},
                       scored_at=now - timedelta(days=1), app_created=now - timedelta(days=1))
    d = _client().get(f"/application/{aid}/match").json()
    assert d["from_previous_resume"] is False and d["resume_uploaded_at"] is None


def test_no_upload_time_means_no_claim(clean, monkeypatch):
    from app.api import server
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    with get_session() as s:
        s.add(UserProfile(user_id=UID))
        s.commit()
    aid = _seed_scored("unknown", reasoning="Fine.", breakdown={}, scored_at=None,
                       app_created=datetime.utcnow() - timedelta(days=90))
    assert _client().get(f"/application/{aid}/match").json()["from_previous_resume"] is False


# ── 3. A new resume re-judges the board, bounded ─────────────────────────────

def _board_job(ext, *, age_days, status=ApplicationStatus.SHORTLISTED, score=80.0):
    with get_session() as s:
        seen = datetime.utcnow() - timedelta(days=age_days)
        j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + ext, company=f"Co{ext}",
                title="Backend Engineer", location="Austin, TX", url=f"https://x/{ext}",
                description="d", rerank_score=score, rerank_reasoning="old resume says so",
                rerank_breakdown="{}", scored_at=seen, first_seen=seen)
        s.add(j)
        s.commit()
        s.refresh(j)
        s.add(Application(job_id=j.id, user_id=UID, status=status))
        s.commit()
        return j.id


def test_a_new_resume_rescores_only_the_recent_untouched_board(clean, monkeypatch):
    from app.config import settings
    from app.strategy.realign import rescore_board_for_new_resume
    monkeypatch.setattr(settings, "realign_rescore_days", 2)
    monkeypatch.setattr(settings, "realign_max_rescore", 500)
    fresh = _board_job("fresh", age_days=0)
    old = _board_job("old", age_days=4)
    tailored = _board_job("tail", age_days=0, status=ApplicationStatus.TAILORED)
    skipped = _board_job("skip", age_days=0, status=ApplicationStatus.SKIPPED)
    stats = rescore_board_for_new_resume(UID)
    assert stats["rescore"] == 1 and stats["kept_score"] == 1
    with get_session() as s:
        assert s.get(Job, fresh).rerank_score is None, "back in the queue for the new resume"
        assert s.get(Job, fresh).rerank_reasoning is None
        for jid in (old, tailored, skipped):
            assert s.get(Job, jid).rerank_score == 80.0


def test_the_rescore_is_capped(clean, monkeypatch):
    from app.config import settings
    from app.strategy.realign import rescore_board_for_new_resume
    monkeypatch.setattr(settings, "realign_max_rescore", 2)
    for i in range(4):
        _board_job(f"cap{i}", age_days=0)
    stats = rescore_board_for_new_resume(UID)
    assert stats["rescore"] == 2 and stats["capped"] == 2


# ── 3b. A job waiting for its re-score is not a 0 on the slate ───────────────
# Review 2026-10-09: the re-score clears rerank_score on today's board, the
# slate read NULL as 0, and from the upload until the re-score landed any
# qualifying job "beat" a 92 by the margin and replaced it; the 92 came back
# re-scored to find its application SKIPPED, and was never delivered again.

@pytest.fixture
def two_slot_slate(monkeypatch):
    import app.common.plan_limits as pl
    from app.config import settings
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 2)
    monkeypatch.setattr(settings, "realign_rescore_days", 2)
    monkeypatch.setattr(settings, "realign_max_rescore", 500)


def _slate_job(ext, score, *, age_days=0.0, title="Backend Engineer"):
    with get_session() as s:
        seen = datetime.utcnow() - timedelta(days=age_days)
        j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + ext,
                company=f"Slate{ext}", title=f"{title} {ext}", location="Austin, TX",
                url=f"https://x/{ext}", description=f"d{ext}", rerank_score=score,
                rerank_reasoning="scored against the old resume", rerank_breakdown="{}",
                scored_at=seen, first_seen=seen)
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _status_of(jid):
    with get_session() as s:
        return s.exec(select(Application.status).where(Application.job_id == jid)).first()


def _verdict_lands(jid, score):
    with get_session() as s:
        j = s.get(Job, jid)
        j.rerank_score = score
        s.add(j)
        s.commit()


def _strict_json(text):
    def _no_constants(name):
        raise ValueError(f"not JSON: {name}")
    return json.loads(text, parse_constant=_no_constants)


def test_a_new_resume_does_not_open_the_board_to_weaker_jobs(clean, two_slot_slate):
    from app.config import settings
    from app.matching.finals_budget import challenger_gate
    from app.strategy import slate
    from app.strategy.realign import rescore_board_for_new_resume
    a, b = _slate_job("rsa", 92.0), _slate_job("rsb", 90.0)
    assert _place(a, 92.0).created and _place(b, 90.0).created
    assert _place(_slate_job("rsc", 65.0), 65.0).outcome == "below_cutoff"
    # The resume upload clears both scores; a challenger arrives first.
    assert rescore_board_for_new_resume(UID)["rescore"] == 2
    weak = _slate_job("rsd", 65.0)
    p = _place(weak, 65.0)
    assert not p.created and p.outcome == "below_cutoff", p
    assert p.displaced_id is None
    strong = _slate_job("rse", 95.0)
    assert not _place(strong, 95.0).created, "nothing real to measure it against yet"
    assert _status_of(a) == _status_of(b) == ApplicationStatus.SHORTLISTED
    # The Tier-1 gate stays at the bar (as it was) so the re-score itself is
    # never priced out; it is not a 0 cutoff, it is no cutoff.
    assert slate.cutoff(UID) is None
    assert challenger_gate(UID) == int(settings.shortlist_score_threshold)
    # Every placement event this wrote is strict JSON (the floor is finite).
    with get_session() as s:
        for meta in s.exec(select(FunnelEvent.metadata_json).where(
                FunnelEvent.job_id.in_([weak, strong]), FunnelEvent.stage == "placement")).all():
            _strict_json(meta)
    # The new verdicts land: real scores are compared again, and the strong
    # job the re-shortlist backstop offers again takes the weakest slot.
    _verdict_lands(a, 91.0)
    _verdict_lands(b, 70.0)
    assert slate.cutoff(UID) == 70.0
    p = _place(strong, 95.0)
    assert p.outcome == "replaced" and p.cutoff == 70.0
    assert _status_of(b) == ApplicationStatus.SKIPPED
    assert _status_of(a) == ApplicationStatus.SHORTLISTED
    assert _place(weak, 65.0).outcome == "below_cutoff"


def test_a_pending_entry_is_never_the_one_replaced(clean, two_slot_slate):
    """Mixed slate: only the entry holding a real verdict is measured."""
    from app.strategy import slate
    from app.strategy.realign import rescore_board_for_new_resume
    fresh = _slate_job("mxa", 92.0)                  # re-judged (first seen today)
    older = _slate_job("mxb", 70.0, age_days=4)      # outside the window: keeps its 70
    assert _place(fresh, 92.0).created and _place(older, 70.0).created
    assert rescore_board_for_new_resume(UID)["rescore"] == 1
    assert slate.cutoff(UID) is None, "a re-score in flight keeps the gate at the bar"
    assert _place(_slate_job("mxc", 72.0), 72.0).outcome == "below_cutoff"
    p = _place(_slate_job("mxd", 80.0), 80.0)
    assert p.outcome == "replaced" and p.cutoff == 70.0
    assert _status_of(older) == ApplicationStatus.SKIPPED
    assert _status_of(fresh) == ApplicationStatus.SHORTLISTED


def test_a_role_change_rescore_is_not_a_zero_either(clean, two_slot_slate):
    """realign_pool_to_roles clears the same scores the same way."""
    from app.strategy.realign import realign_pool_to_roles
    a, b = _slate_job("rra", 92.0), _slate_job("rrb", 90.0)
    assert _place(a, 92.0).created and _place(b, 90.0).created
    stats = realign_pool_to_roles(UID, ["Backend Engineer", "Platform Engineer"],
                                  old_roles=["Data Engineer"])
    assert stats["rescore"] == 2
    p = _place(_slate_job("rrc", 65.0), 65.0)
    assert not p.created and p.outcome == "below_cutoff", p
    assert _status_of(a) == _status_of(b) == ApplicationStatus.SHORTLISTED


# ── 3c. ...nor under the per-company cap ─────────────────────────────────────
# Review 2026-10-09 (round 3): place() runs the company cap even while the
# slate has room, and pipeline._displace_weaker_shortlisted read the cleared
# score as 0 too, so after an upload a 65 at the same company evicted a
# pending 92 ("65 vs 0") and the 92 came back re-scored to "exists".

@pytest.fixture
def three_per_company(monkeypatch):
    import app.common.plan_limits as pl
    from app.config import settings
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 35)    # the slate has room
    monkeypatch.setattr(settings, "company_cap", 3)
    monkeypatch.setattr(settings, "company_cap_displace_enabled", True)
    monkeypatch.setattr(settings, "company_cap_displace_margin", 5)
    monkeypatch.setattr(settings, "realign_rescore_days", 2)
    monkeypatch.setattr(settings, "realign_max_rescore", 500)


def _cap_job(ext, score):
    """Same company, own title / city / text: the duplicate rule never fires."""
    with get_session() as s:
        j = Job(user_id=UID, source=JobSource.GREENHOUSE, external_id=_P + ext,
                company="mc_BigCo", title=f"Backend Engineer {ext}",
                location=f"City{ext}, TX", url=f"https://x/{ext}", description=f"d{ext}",
                rerank_score=score, rerank_reasoning="old resume", rerank_breakdown="{}",
                scored_at=datetime.utcnow(), first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _fill_the_company_cap():
    held = [_cap_job(ext, sc) for ext, sc in (("cca", 92.0), ("ccb", 90.0), ("ccc", 88.0))]
    for jid, sc in zip(held, (92.0, 90.0, 88.0)):
        assert _place(jid, sc).created
    assert _place(_cap_job("ccd", 65.0), 65.0).outcome == "company_cap"
    return held


def _assert_the_pending_holders_stay(held):
    p = _place(_cap_job("cce", 65.0), 65.0)
    assert not p.created and p.outcome == "company_cap", p
    strong = _cap_job("ccf", 99.0)
    assert _place(strong, 99.0).outcome == "company_cap", "nothing real to measure it against"
    assert {_status_of(j) for j in held} == {ApplicationStatus.SHORTLISTED}
    with get_session() as s:
        notes = s.exec(select(Application.notes).where(Application.job_id.in_(held))).all()
    assert not any("Displaced" in (n or "") for n in notes)
    return strong


def test_a_new_resume_does_not_open_the_company_cap(clean, three_per_company):
    from app.strategy.realign import rescore_board_for_new_resume
    held = _fill_the_company_cap()
    assert rescore_board_for_new_resume(UID)["rescore"] == 3
    strong = _assert_the_pending_holders_stay(held)
    # The verdicts land: real scores are compared again, and the strong job the
    # re-shortlist backstop offers again displaces the weakest holder.
    for jid, sc in zip(held, (91.0, 90.0, 70.0)):
        _verdict_lands(jid, sc)
    assert _place(strong, 99.0).outcome == "placed"
    assert _status_of(held[2]) == ApplicationStatus.SKIPPED
    assert _status_of(held[0]) == _status_of(held[1]) == ApplicationStatus.SHORTLISTED


def test_a_role_change_does_not_open_the_company_cap(clean, three_per_company):
    from app.strategy.realign import realign_pool_to_roles
    held = _fill_the_company_cap()
    stats = realign_pool_to_roles(UID, ["Backend Engineer", "Platform Engineer"],
                                  old_roles=["Data Engineer"])
    assert stats["rescore"] == 3
    _assert_the_pending_holders_stay(held)


def test_only_a_holder_with_a_verdict_is_displaced(clean, three_per_company):
    """Mixed: two holders await their re-score, one keeps a real 70."""
    held = _fill_the_company_cap()
    with get_session() as s:
        for jid, sc in zip(held, (None, None, 70.0)):
            j = s.get(Job, jid)
            j.rerank_score = sc
            s.add(j)
        s.commit()
    assert _place(_cap_job("ccg", 72.0), 72.0).outcome == "company_cap"
    assert _place(_cap_job("cch", 80.0), 80.0).outcome == "placed"
    assert _status_of(held[2]) == ApplicationStatus.SKIPPED
    assert _status_of(held[0]) == _status_of(held[1]) == ApplicationStatus.SHORTLISTED


def test_only_a_changed_file_counts_as_a_new_resume(clean):
    from app.api import server
    assert server._stamp_resume_upload(UID, "a" * 64) is True
    with get_session() as s:
        first = s.exec(select(UserProfile).where(UserProfile.user_id == UID)).first().resume_uploaded_at
    assert first is not None
    assert server._stamp_resume_upload(UID, "a" * 64) is False, "the same file again"
    assert server._stamp_resume_upload(UID, "b" * 64) is True


# ── 4. One role, one card ────────────────────────────────────────────────────

_TEXT_HASH = "86e47fb6cc90c5c20e2b45f513b6b23e98b60aa9859e856375348a9847e5b0aa"


def _role_job(ext, *, location, remote, chash, company="Miratech",
              title="Full Stack Java Engineer — Workforce Management (WFM)"):
    with get_session() as s:
        j = Job(user_id=UID, source=JobSource.SMARTRECRUITERS, external_id=_P + ext,
                company=company, title=title, location=location, remote=remote,
                url=f"https://x/{ext}", description="d", content_hash=chash,
                rerank_score=78, first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _place(jid, score=78.0):
    from app.strategy import slate
    with get_session() as s:
        p = slate.place(s, s.get(Job, jid), score, user_id=UID)
        s.commit()
        return p


@pytest.fixture
def roomy_slate(monkeypatch):
    import app.common.plan_limits as pl
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 50)


def test_one_role_posted_per_city_reaches_the_board_once(clean, roomy_slate):
    """The production pair: same source, same text, two cities, both remote."""
    miami = _role_job("mia", location="Miami, FL, United States", remote=True, chash=_TEXT_HASH)
    ny = _role_job("nyc", location="New York, NY, United States", remote=True, chash=_TEXT_HASH)
    assert _place(miami).created
    p = _place(ny, 72.0)
    assert not p.created and p.outcome == "duplicate"


def test_identical_text_is_one_role_even_on_site(clean, roomy_slate):
    a = _role_job("txa", location="Seattle, WA", remote=False, chash=_TEXT_HASH)
    b = _role_job("txb", location="Austin, TX", remote=False, chash=_TEXT_HASH)
    assert _place(a).created
    assert _place(b).outcome == "duplicate"


def test_two_onsite_requisitions_with_their_own_text_stay_two(clean, roomy_slate):
    a = _role_job("rqa", location="Seattle, WA", remote=False, chash="1" * 64)
    b = _role_job("rqb", location="Austin, TX", remote=False, chash="2" * 64)
    assert _place(a).created and _place(b).created


def test_empty_postings_are_not_one_text(clean, roomy_slate):
    from app.strategy.slate import _EMPTY_TEXT_HASH
    a = _role_job("ema", location="Seattle, WA", remote=False, chash=_EMPTY_TEXT_HASH)
    b = _role_job("emb", location="Austin, TX", remote=False, chash=_EMPTY_TEXT_HASH)
    assert _place(a).created and _place(b).created


def test_the_board_shows_a_role_already_placed_twice_once(clean, monkeypatch):
    """Rows placed before the rule (production has the pair) render as one card;
    the stronger one is kept."""
    from app.api import server
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    ids = {}
    for ext, loc, score in (("bmia", "Miami, FL, United States", 78.0),
                            ("bnyc", "New York, NY, United States", 72.0)):
        jid = _role_job(ext, location=loc, remote=True, chash=_TEXT_HASH)
        with get_session() as s:
            j = s.get(Job, jid)
            j.rerank_score, j.blended_score = score, score
            s.add(j)
            a = Application(job_id=jid, user_id=UID, status=ApplicationStatus.SHORTLISTED)
            s.add(a)
            s.commit()
            s.refresh(a)
            ids[ext] = a.id
    html = _client().get("/dashboard").text
    assert f'data-app-id="{ids["bmia"]}"' in html
    assert f'data-app-id="{ids["bnyc"]}"' not in html


# ── 5. Placeholder locations ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,shown", [
    ("na", ""), ("N/A", ""), ("none", ""), ("null", ""), ("-", ""), ("tbd", ""),
    ("N/A, N/A", ""), ("  ", ""), (None, ""),
    ("Remote, NA", "Remote, NA"),           # North America: never dropped as a part
    ("New York, NY", "New York, NY"),
])
def test_placeholder_locations_are_unknown(raw, shown):
    from app.common.geo import clean_location
    assert clean_location(raw) == shown


def test_a_placeholder_never_renders_as_a_place_on_a_card(clean, monkeypatch):
    from app.api import server
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    jid = _role_job("na", location="na", remote=False, chash="3" * 64, company="Stripe",
                    title="Software Engineer, Internal Identity")
    with get_session() as s:
        s.add(Application(job_id=jid, user_id=UID, status=ApplicationStatus.SHORTLISTED))
        s.commit()
    html = _client().get("/dashboard").text
    assert "Internal Identity" in html
    assert "na · On-site" not in html


def test_the_scorer_is_told_a_placeholder_is_no_location():
    from app.matching.reranker import _location_lines
    assert "not stated in the posting" in _location_lines(SimpleNamespace(location="n/a", remote=False))


# ── 6. Absolute links are https ──────────────────────────────────────────────

def _request(host, *, scheme="http", headers=()):
    from starlette.requests import Request
    port = 443 if scheme == "https" else 80
    return Request({"type": "http", "scheme": scheme, "server": (host, port), "path": "/",
                    "root_path": "", "query_string": b"",
                    "headers": [(b"host", host.encode())] + [(k.encode(), v.encode())
                                                              for k, v in headers]})


def test_links_behind_the_tls_proxy_are_https(monkeypatch):
    from app.api.server import _public_base_url
    from app.config import settings
    monkeypatch.setattr(settings, "public_base_url", "")
    assert _public_base_url(_request("app.spotapply.ai")) == "https://app.spotapply.ai"
    assert _public_base_url(_request("app.spotapply.ai", headers=[("x-forwarded-proto", "https")])) \
        == "https://app.spotapply.ai"
    # Local development keeps its own scheme.
    assert _public_base_url(_request("127.0.0.1:8000")) == "http://127.0.0.1:8000"
    monkeypatch.setattr(settings, "public_base_url", "https://app.spotapply.ai/")
    assert _public_base_url(_request("10.0.0.7:8080")) == "https://app.spotapply.ai"


def test_the_referral_and_profile_links_are_https(clean, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import server
    from app.config import settings
    monkeypatch.setattr(settings, "public_base_url", "", raising=False)
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    with get_session() as s:
        s.add(UserProfile(user_id=UID, first_name="Mc", last_name="Test"))
        s.commit()
    client = TestClient(server.app, base_url="http://app.spotapply.ai")
    ref = client.get("/api/referral").json()
    assert ref["link"].startswith("https://app.spotapply.ai/login?ref="), ref["link"]
    trust = client.get("/api/trust").json()
    assert trust["share_url"].startswith("https://app.spotapply.ai/u/"), trust["share_url"]
