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


def test_a_silent_posting_is_not_a_blocker_for_a_student():
    """The rubric says silent = high; the model wrote 10 next to 'silent'."""
    from app.intelligence.work_auth import reconcile_work_auth_factor as fix
    out = fix({"score": 10, "note": "F-1 OPT; posting silent on sponsorship but US role"},
              _opt(), False)
    assert out["score"] >= 75
    # ...unless the note names a real restriction, or the posting refuses.
    assert fix({"score": 10, "note": "silent on visas; requires US citizenship"},
               _opt(), False)["score"] == 10
    assert fix({"score": 10, "note": "posting silent on sponsorship"}, _opt(), True)["score"] == 10
    # Unknown posting text is never taken as "silent".
    assert fix({"score": 10, "note": "posting silent on sponsorship"}, _opt(), None)["score"] == 10


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
