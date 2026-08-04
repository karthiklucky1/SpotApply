"""Recruiter verification is a PII gate, so it must not be self-service.

Verification unlocks POST /api/recruiter/search, which returns, for every candidate
in the pool: full name, current title, skills, availability, **work authorization**,
and **whether they need sponsorship**.

The old auto-verify compared the domain of `work_email` against `company_domain` and
granted `verified = True` on a match. But both fields arrive in the same request
body, so a match proved only that the caller typed two consistent strings — never
that they control the mailbox. Anyone could register as
`me@acme-recruiting.test` / `acme-recruiting.test` and read the pool. Sponsorship
need is exactly the attribute a discriminating actor would filter on.

A domain match is now a recorded signal; promotion goes through an admin.
"""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.api import server
from app.config import settings
from app.db.init_db import get_session
from app.db.models import RecruiterProfile


def _rp(**kw):
    base = dict(user_id=None, full_name="R Ecruiter", work_email="", company_name="Acme",
                company_domain="", title="Recruiter", specialties="backend",
                verified=False, verification_notes="", h1b_filings=0,
                charges_candidates=False, banned=False)
    base.update(kw)
    return RecruiterProfile(**base)


@pytest.fixture(autouse=True)
def _no_autoverify(monkeypatch):
    """The shipped default. Individual tests opt into the legacy behaviour."""
    monkeypatch.setattr(settings, "recruiter_autoverify_on_domain_match", False,
                        raising=False)


# ── the hole ─────────────────────────────────────────────────────────────────

def test_a_matching_self_supplied_domain_does_not_grant_verification():
    """THE bug. Both sides of the comparison are attacker-controlled."""
    rp = _rp(work_email="me@acme-recruiting.test", company_domain="acme-recruiting.test")
    server._verify_recruiter(rp)
    assert rp.verified is False, (
        "a caller verified themselves by supplying an email and a domain that "
        "match each other — no proof of controlling either")
    assert "Pending manual review" in rp.verification_notes


def test_the_domain_match_is_still_recorded_as_a_signal():
    """Refusing to auto-grant must not throw away the evidence an admin needs."""
    rp = _rp(work_email="me@acme.test", company_domain="acme.test")
    server._verify_recruiter(rp)
    assert "Corporate email matches acme.test" in rp.verification_notes


def test_autoverify_can_be_turned_back_on_explicitly(monkeypatch):
    """Documented escape hatch, so the change is reversible in one env var."""
    monkeypatch.setattr(settings, "recruiter_autoverify_on_domain_match", True,
                        raising=False)
    rp = _rp(work_email="me@acme.test", company_domain="acme.test")
    server._verify_recruiter(rp)
    assert rp.verified is True


def test_it_defaults_to_off():
    assert settings.recruiter_autoverify_on_domain_match is False


# ── the checks that already worked, kept working ─────────────────────────────

@pytest.mark.parametrize("email,domain", [
    ("me@gmail.com", "gmail.com"),
    ("me@outlook.com", "outlook.com"),
    ("me@proton.me", "proton.me"),
])
def test_free_mail_never_verifies(email, domain):
    rp = _rp(work_email=email, company_domain=domain)
    server._verify_recruiter(rp)
    assert rp.verified is False
    assert "Free email" in rp.verification_notes


def test_a_mismatched_domain_never_verifies():
    rp = _rp(work_email="me@somewhere.test", company_domain="acme.test")
    server._verify_recruiter(rp)
    assert rp.verified is False
    assert "does not match" in rp.verification_notes


def test_charging_candidates_bans_and_short_circuits():
    """Charging candidates is illegal in most jurisdictions — a hard ban, and it
    must not fall through to any verification logic."""
    rp = _rp(work_email="me@acme.test", company_domain="acme.test",
             charges_candidates=True)
    server._verify_recruiter(rp)
    assert rp.banned is True and rp.verified is False


# ── the admin promotion path ─────────────────────────────────────────────────

_UID = "recruiter-under-review"


@pytest.fixture
def pending_recruiter():
    with get_session() as s:
        for r in s.exec(select(RecruiterProfile).where(
                RecruiterProfile.user_id == _UID)).all():
            s.delete(r)
        s.commit()
        s.add(_rp(user_id=_UID, work_email="me@acme.test", company_domain="acme.test"))
        s.commit()
    yield
    with get_session() as s:
        for r in s.exec(select(RecruiterProfile).where(
                RecruiterProfile.user_id == _UID)).all():
            s.delete(r)
        s.commit()


def _verified() -> bool:
    with get_session() as s:
        return s.exec(select(RecruiterProfile).where(
            RecruiterProfile.user_id == _UID)).first().verified


def test_an_admin_can_promote_by_user_id(pending_recruiter, monkeypatch):
    """Turning auto-verify off must leave a way forward, not a dead end."""
    monkeypatch.setattr(server, "_require_admin_user", lambda request: "admin@x")
    out = server.admin_verify_recruiter(request=None,
                                        body={"user_id": _UID, "note": "called them"})
    assert out["verified"] is True and _verified() is True
    assert "verified by admin@x" in out["notes"] and "called them" in out["notes"]


def test_an_admin_can_promote_by_work_email(pending_recruiter, monkeypatch):
    monkeypatch.setattr(server, "_require_admin_user", lambda request: "admin@x")
    assert server.admin_verify_recruiter(
        request=None, body={"work_email": "ME@Acme.test"})["verified"] is True


def test_an_admin_can_demote(pending_recruiter, monkeypatch):
    monkeypatch.setattr(server, "_require_admin_user", lambda request: "admin@x")
    server.admin_verify_recruiter(request=None, body={"user_id": _UID})
    server.admin_verify_recruiter(request=None,
                                  body={"user_id": _UID, "verified": False})
    assert _verified() is False


def test_a_banned_account_cannot_be_verified_by_accident(pending_recruiter, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(server, "_require_admin_user", lambda request: "admin@x")
    with get_session() as s:
        rp = s.exec(select(RecruiterProfile).where(
            RecruiterProfile.user_id == _UID)).first()
        rp.banned = True
        s.add(rp)
        s.commit()
    with pytest.raises(HTTPException) as exc:
        server.admin_verify_recruiter(request=None, body={"user_id": _UID})
    assert exc.value.status_code == 409
    assert _verified() is False


def test_promotion_requires_an_identifier(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(server, "_require_admin_user", lambda request: "admin@x")
    with pytest.raises(HTTPException) as exc:
        server.admin_verify_recruiter(request=None, body={})
    assert exc.value.status_code == 400


def test_the_promotion_route_is_admin_gated():
    """It flips a PII gate, so it must not be reachable by an ordinary user."""
    import inspect
    src = inspect.getsource(server.admin_verify_recruiter)
    assert "_require_admin_user(" in src
