"""Students: internships & co-ops, and F-1 / citizen work authorization.

Production, 2026-09-28 (counts only, 19 profiles): the two users who chose
"Looking for: Both" got NO internships — "both" counted as full-time-only
unless a second checkbox was also ticked — and all four F-1 OPT/CPT students
had "Requires visa sponsorship" unticked, so the filter and the scorer treated
them as never needing sponsorship while their visa status said OPT. The status
dropdown was read by nothing that matched jobs, and "CPT" was not a status the
work-authorization reader knew. Synthetic profiles and postings only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.api import server
from app.common.tenant_prefs import internships_only, needs_sponsorship, wants_internships
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile
from app.intelligence.work_auth import assess_profile
from app.matching.filters.rule_filter import RuleFilter


class _Prof:
    def __init__(self, **kw):
        self.years_experience = 0
        self.salary_min = self.salary_max = 0
        self.salary_currency = "USD"
        self.requires_sponsorship = False
        self.preferred_country = "United States"
        self.key_skills = "python, sql"
        self.degree = "B.S. Computer Science"
        self.job_type_preference = "full_time"
        self.include_internships_in_discovery = False
        self.work_authorization = ""
        self.visa_status = ""
        self.work_auth_status = ""
        self.target_roles = "Software Engineer"
        for k, v in kw.items():
            setattr(self, k, v)


def _job(title, desc="Build services in Python for our platform."):
    return Job(title=title, company="Co", location="Austin, TX", remote=False,
               description=desc, source=JobSource.GREENHOUSE, external_id="x", url="u")


INTERN = "Software Engineer Intern"
COOP = "Software Developer Co-op (Fall 2027)"
FULL = "Software Engineer"


# ── "Looking for" ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pref,legacy_switch,intern_ok,full_ok", [
    ("full_time", False, False, True),
    ("internship", False, True, False),
    ("both", False, True, True),          # THE bug: Both gave no internships
    ("full_time", True, True, True),      # the older "Also search internships" switch
])
def test_looking_for_decides_which_jobs_pass(pref, legacy_switch, intern_ok, full_ok):
    f = RuleFilter(_Prof(job_type_preference=pref, include_internships_in_discovery=legacy_switch))
    assert f.filter(_job(INTERN)).passed is intern_ok
    assert f.filter(_job(COOP)).passed is intern_ok, "a co-op is an internship-type role"
    assert f.filter(_job(FULL)).passed is full_ok


def test_the_preference_helpers():
    assert wants_internships(_Prof(job_type_preference="both"))
    assert not internships_only(_Prof(job_type_preference="both"))
    assert internships_only(_Prof(job_type_preference="internship"))
    assert not wants_internships(_Prof())


def test_internship_searches_lead_and_stay_anchored_to_the_users_role():
    roles = ["Software Engineer", "Data Analyst", "Backend Engineer", "ML Engineer"]
    only = server._internship_keywords(roles, _Prof(job_type_preference="internship"))
    # Sources read only the first few keywords: internships-only leads with them.
    assert only[:3] == ["Software Engineer intern", "Data Analyst intern", "Backend Engineer intern"]
    both = server._internship_keywords(roles, _Prof(job_type_preference="both"))
    assert both[:4] == ["Software Engineer", "Software Engineer intern",
                        "Data Analyst", "Data Analyst intern"]
    for kws in (only, both):
        # A bare term matches every title at the role gate (Marketing Internship…).
        assert "internship" not in [k.lower() for k in kws]
        assert "co-op" not in [k.lower() for k in kws]
        assert "Software Engineer co-op" in kws
    assert server._internship_keywords(roles, _Prof()) == roles


# ── work authorization ───────────────────────────────────────────────────────

@pytest.mark.parametrize("status,box,needs", [
    ("OPT", False, True),               # the production case: box never ticked
    ("STEM OPT", False, True),
    ("CPT", False, True),               # was not a status the reader knew at all
    ("Student Visa", False, True),
    ("Needs Sponsorship", False, True),
    ("H1B", False, True),
    ("Citizen", True, False),           # a citizen never needs sponsorship
    ("Green Card", True, False),
    ("TN", False, False),               # the checkbox decides for these
    ("TN", True, True),
    ("", True, True),
    ("", False, False),
    ("Other", False, False),
])
def test_the_visa_status_decides_sponsorship(status, box, needs):
    assert needs_sponsorship(_Prof(work_auth_status=status, requires_sponsorship=box)) is needs


def test_cpt_is_framed_as_the_internship_authorization():
    fr = assess_profile(_Prof(work_auth_status="CPT"))
    assert fr.basis == "F-1 CPT" and fr.needs_future_sponsorship
    assert fr.review_flag, "the future-sponsorship question stays the student's to answer"
    assert assess_profile(_Prof(work_authorization="F-1 CPT")).basis == "F-1 CPT"


def test_a_citizen_outside_the_us_is_not_called_a_us_citizen():
    fr = assess_profile(_Prof(work_auth_status="Citizen", preferred_country="Canada"))
    assert "U.S." not in fr.basis and "Canada" in fr.basis
    assert assess_profile(_Prof(work_auth_status="Citizen")).basis == "U.S. Citizen"


NO_SPONSOR = ("Build services in Python. We are unable to sponsor visas for this role; "
              "candidates must be authorized to work without sponsorship.")
CITIZENS_ONLY = "Build services in Python. Must be a U.S. citizen due to contract requirements."


def test_an_opt_student_does_not_see_jobs_that_refuse_sponsorship():
    f = RuleFilter(_Prof(work_auth_status="OPT", job_type_preference="both"))
    r = f.filter(_job(FULL, NO_SPONSOR))
    assert not r.passed and "Sponsorship" in r.reason
    # ...while a citizen keeps them.
    assert RuleFilter(_Prof(work_auth_status="Citizen")).filter(_job(FULL, NO_SPONSOR)).passed


@pytest.mark.parametrize("status,passes", [
    ("Citizen", True), ("Green Card", False), ("", True),
])
def test_citizens_only_roles_read_the_dropdown_too(status, passes):
    """Checked only for users who need no sponsorship (a sponsorship-needing
    student is handled by the refusal check); an unset status never blocks."""
    assert RuleFilter(_Prof(work_auth_status=status)).filter(_job(FULL, CITIZENS_ONLY)).passed is passes


def test_the_scorer_is_told_what_the_status_says():
    from app.matching.reranker import _prescore_system_prompt, _profile_system_prompt
    p = _Prof(work_auth_status="OPT")
    assert "WILL need visa sponsorship" in _profile_system_prompt(p)
    assert "needs visa sponsorship" in _prescore_system_prompt(p)
    assert "does NOT need sponsorship" in _profile_system_prompt(_Prof(work_auth_status="Citizen"))


def test_form_answers_stay_the_students_to_give():
    """The audited rule: a dated status never gets its future-sponsorship
    question answered for the user — from the dropdown either."""
    for status in ("OPT", "CPT", "STEM OPT"):
        assert server._sponsorship_answer_for_pack(_Prof(work_auth_status=status)) is None


# ── changing "Looking for" re-points the board ───────────────────────────────

_U = "student-realign"


@pytest.fixture
def student_pool():
    def _wipe():
        with get_session() as s:
            s.exec(delete(Application).where(Application.user_id == _U))
            s.exec(delete(Job).where(Job.user_id == _U))
            s.exec(delete(UserProfile).where(UserProfile.user_id == _U))
            s.commit()
    _wipe()
    now = datetime.utcnow()
    ids = {}
    with get_session() as s:
        s.add(UserProfile(user_id=_U, job_type_preference="full_time",
                          include_internships_in_discovery=False))
        for key, title, score, reason in (
            ("intern_stamped", INTERN, 10.0,
             "Rule filtered: Internship filtered: user did not opt into internships"),
            ("coop_stamped", COOP, 10.0,
             "Pre-screened (Tier-1 fit 10): Internship filtered: user did not opt into internships"),
            ("old_intern", "Data Intern", 10.0,
             "Rule filtered: Internship filtered: user did not opt into internships"),
            ("full_board", FULL, 82.0, "Strong match."),
            ("full_opened", "Backend Engineer", 80.0, "Strong match."),
        ):
            seen = now - timedelta(days=30 if key == "old_intern" else 1)
            j = Job(user_id=_U, source=JobSource.GREENHOUSE, external_id=f"{_U}-{key}",
                    company=f"Co {key}", title=title, url=f"https://x/{key}",
                    description="Build services in Python.", rerank_score=score,
                    rerank_reasoning=reason, prescore=score, scored_at=now,
                    first_seen=seen, discovered_at=seen, posted_at=seen)
            s.add(j)
            s.flush()
            ids[key] = j.id
        s.add(Application(user_id=_U, job_id=ids["full_board"], status=ApplicationStatus.SHORTLISTED))
        s.add(Application(user_id=_U, job_id=ids["full_opened"], status=ApplicationStatus.SHORTLISTED,
                          viewed_at=now))
        s.commit()
    yield ids
    _wipe()


def _set_pref(pref: str) -> None:
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == _U)).first()
        p.job_type_preference = pref
        s.add(p)
        s.commit()


def test_switching_to_internships_only_reopens_them_and_clears_unopened_full_time(student_pool):
    from app.strategy.realign import realign_job_type
    ids = student_pool
    _set_pref("internship")
    stats = realign_job_type(_U)
    with get_session() as s:
        job = {k: s.get(Job, v) for k, v in ids.items()}
        apps = {a.job_id: a for a in s.exec(select(Application).where(Application.user_id == _U)).all()}
    # Fresh internships rejected under the old choice go back in the queue...
    assert job["intern_stamped"].rerank_score is None and job["intern_stamped"].prescore is None
    assert job["coop_stamped"].rerank_score is None
    # ...but not one past the scoring window (it would only be expired again).
    assert job["old_intern"].rerank_score == 10.0
    # An unopened full-time job leaves the board; one the student opened stays.
    assert apps[ids["full_board"]].status == ApplicationStatus.SKIPPED
    assert "[job-type-realign]" in apps[ids["full_board"]].notes
    assert apps[ids["full_opened"]].status == ApplicationStatus.SHORTLISTED
    assert stats == {"reopened": 2, "unshortlisted": 1, "restored": 0}

    # Switching back to Both returns what WE removed, and only that.
    _set_pref("both")
    stats = realign_job_type(_U)
    with get_session() as s:
        remaining = {a.job_id for a in s.exec(select(Application).where(Application.user_id == _U)).all()}
    assert ids["full_board"] not in remaining, "our removal is undone (the backstop re-offers it)"
    assert ids["full_opened"] in remaining
    assert stats["restored"] == 1


def test_the_profile_save_realigns_only_when_looking_for_changes(student_pool, monkeypatch):
    calls = []
    import app.strategy.realign as realign
    monkeypatch.setattr(realign, "realign_job_type", lambda uid: calls.append(uid) or {})
    monkeypatch.setattr(server, "_get_user_id", lambda request: _U)
    from app.config import Settings
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: True))
    server.update_profile(request=None, update=server.ProfileUpdate(key_skills="python"))
    assert calls == []
    server.update_profile(request=None, update=server.ProfileUpdate(job_type_preference="internship"))
    assert calls == [_U]
