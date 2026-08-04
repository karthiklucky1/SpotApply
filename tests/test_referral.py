"""Unit tests for generate_referral_drafts extensions."""
import pytest
from unittest.mock import patch, MagicMock
from sqlmodel import select
from app.db.init_db import get_session
from app.db.models import Job, JobSource, Application, ApplicationStatus, UserProfile
from app.intelligence.referral import generate_referral_drafts


def _seed():
    with get_session() as s:
        # Clean old test runs
        for j in s.exec(select(Job).where(Job.external_id.like("referral-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        # Create user profile if none exists
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
        if not prof:
            prof = UserProfile(user_id="local", first_name="Karthik", university="University of Cincinnati")
            s.add(prof)
        else:
            prof.university = "University of Cincinnati"
            s.add(prof)
        
        j = Job(source=JobSource.MANUAL, external_id="referral-test-1",
                company="GitHub", title="Software Engineer",
                url="http://x", description="React, Python")
        s.add(j); s.commit(); s.refresh(j)
        a = Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, user_id="local")
        s.add(a); s.commit(); s.refresh(a)
        return a.id, j.id


def _cleanup():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("referral-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_generate_referral_drafts_extensions():
    app_id, _ = _seed()
    try:
        mock_people = [
            {"name": "Alice Smith", "headline": "Alum of University of Cincinnati | Ex-Apple", "url": "https://linkedin.com/in/alice"}
        ]
        mock_github_repos = ["cli", "desktop"]

        import app.matching.reranker as rr
        with patch("app.intelligence.linkedin_xray.find_champions", return_value={"ok": True, "people": mock_people}), \
             patch("app.intelligence.referral.get_company_github_repos", return_value=mock_github_repos), \
             patch("app.config.settings.anthropic_api_key", "dummy_key"), \
             patch("anthropic.Anthropic") as mock_anthropic:

            # Referral drafts go through the process-wide shared client pair —
            # force a cold build under the patch (reset again in _cleanup-safe
            # finally below so the mock never leaks into other tests).
            rr._CLIENTS = None
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_message = MagicMock()
            
            mock_message.content = [MagicMock(text='''[
                {"type": "referral_request", "label": "Referral request", "channel": "LinkedIn", "body": "Referral draft"},
                {"type": "hiring_manager", "label": "Hiring-manager note", "channel": "LinkedIn", "body": "Hiring manager draft"},
                {"type": "university_alumni", "label": "University Alumni connection", "channel": "LinkedIn", "body": "Go Cincinnati!"},
                {"type": "github_outreach", "label": "GitHub outreach note", "channel": "LinkedIn", "body": "GitHub draft"}
            ]''')]
            mock_client.messages.create.return_value = mock_message

            res = generate_referral_drafts(app_id, user_id="local")
            
            draft_types = {d["type"] for d in res["drafts"]}
            assert "university_alumni" in draft_types
            assert "github_outreach" in draft_types
            
            univ_draft = next(d for d in res["drafts"] if d["type"] == "university_alumni")
            assert "Cincinnati" in univ_draft["body"]
    finally:
        import app.matching.reranker as rr
        rr._CLIENTS = None   # drop the mock-built shared clients
        _cleanup()


def test_referral_asks_follow_the_ats_mechanics():
    """Guard: the ask ladder, the req link, and no resume on first contact.

    Two mechanical facts drive this. A referrer must select a SPECIFIC LIVE JOB
    (so every draft carries the req link), and must describe their RELATIONSHIP
    to you in writing on the record (so a cold "will you refer me?" gets silence
    and must never be the lead ask). Offering a resume on first contact is the
    other measured mistake. See docs/research/hiring-machine-2026-08.md §1.8.
    """
    from app.intelligence.referral import _fallback_drafts

    drafts = _fallback_drafts(
        name="Ada Lovelace", title="Data Engineer", company="Globex", role="Analytics Engineer",
        skills="python, sql, dbt", selling="", needs_sponsorship=False,
        job_url="https://boards.example.com/globex/jobs/42",
    )
    by_type = {d["type"]: d["body"] for d in drafts}

    # The ladder exists, in descending order of yes.
    for t in ("referral_request", "referral_intro_call", "referral_who_owns"):
        assert t in by_type, f"missing ask: {t}"

    # The lead ask is forward-the-req, which needs no relationship claim.
    assert "forward the req" in by_type["referral_request"].lower()

    # Every referral ask carries the specific requisition link.
    for t in ("referral_request", "referral_intro_call", "referral_who_owns"):
        assert "https://boards.example.com/globex/jobs/42" in by_type[t], f"{t} lost the req link"

    # No resume offered or mentioned on first contact, in any draft.
    for t, body in by_type.items():
        assert "resume" not in body.lower(), f"{t} mentions a resume on first contact"

    # A low-cost ask with concrete windows, not an open-ended one.
    assert "15 minutes" in by_type["referral_intro_call"]
    assert "Wednesday" in by_type["referral_intro_call"]
