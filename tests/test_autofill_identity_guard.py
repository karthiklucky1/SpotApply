"""CRITICAL #1: autofill must never fill a non-founder's form with founder PII.

By default (autofill_multi_user_enabled=False) only the founder/local user may
autofill. When multi-user is enabled, each user's form is filled from THEIR OWN
profile, and an incomplete profile fails closed (app/autofill/agent.py).
"""
import contextvars

import app.autofill.agent as af
import app.autofill.answer_pack as ap
from app.config import settings


class _FakeProfile:
    def __init__(self, first_name="", last_name="", email="", phone="",
                 location="", linkedin_url="", github_url=""):
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.phone = phone
        self.location = location
        self.linkedin_url = linkedin_url
        self.github_url = github_url


def test_is_founder(monkeypatch):
    monkeypatch.setattr(settings, "founder_user_id", "founder-1")
    assert af._is_autofill_founder(None) is True
    assert af._is_autofill_founder("local") is True
    assert af._is_autofill_founder("founder-1") is True
    assert af._is_autofill_founder("user-B") is False


def test_founder_allowed_and_uses_globals(monkeypatch):
    monkeypatch.setattr(settings, "founder_user_id", "founder-1")

    def run():
        assert af._set_fill_owner("founder-1") is True
        assert af._autofill_identity.get() is None      # founder → existing behavior
    contextvars.copy_context().run(run)


def test_nonfounder_refused_when_multiuser_off(monkeypatch):
    monkeypatch.setattr(settings, "founder_user_id", "founder-1")
    monkeypatch.setattr(settings, "autofill_multi_user_enabled", False)
    result = contextvars.copy_context().run(lambda: af._set_fill_owner("user-B"))
    assert result is False


def test_nonfounder_complete_profile_uses_own_identity(monkeypatch):
    monkeypatch.setattr(settings, "autofill_multi_user_enabled", True)
    monkeypatch.setattr(settings, "founder_user_id", "founder-1")
    prof = _FakeProfile(first_name="Bob", last_name="Lee", email="bob@x.com",
                        phone="555-000", location="NYC", github_url="gh/bob",
                        linkedin_url="li/bob")
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: prof)

    def run():
        assert af._set_fill_owner("user-B") is True
        pf = af._personal_fields()
        assert pf["first_name"] == "Bob"
        assert pf["email"] == "bob@x.com"
        assert pf["phone"] == "555-000"
    contextvars.copy_context().run(run)


def test_nonfounder_incomplete_profile_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "autofill_multi_user_enabled", True)
    monkeypatch.setattr(settings, "founder_user_id", "founder-1")
    monkeypatch.setattr(ap, "_get_or_create_profile",
                        lambda user_id=None: _FakeProfile(first_name="", email=""))

    def run():
        assert af._set_fill_owner("user-B") is False    # refuse, don't leak
    contextvars.copy_context().run(run)


def test_personal_fields_returns_owner_identity_when_set():
    def run():
        af._autofill_identity.set({"first_name": "Zoe", "last_name": "", "email": "zoe@x.com",
                                   "phone": "", "location": "", "github": "", "linkedin": ""})
        assert af._personal_fields()["first_name"] == "Zoe"
    contextvars.copy_context().run(run)
