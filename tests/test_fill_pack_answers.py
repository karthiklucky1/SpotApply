"""What the fill pack hands the extension, from the 2026-11-03 live test.

An F-1 OPT profile with "Requires visa sponsorship" shown ticked, Open to
relocation, notice period "2 weeks" and links stored as typed
("github.com/karthiklucky1"):

* Ashby rejected the GitHub/portfolio box ("Please enter a URL") — a scheme is
  added to a profile link that looks like a web address, never invented;
* "Will you now or in the future require sponsorship?" was left blank — the
  status now answers it Yes (never No for a dated status);
* the notice-period question had nothing to fill — the pack carries the
  profile's own `availability`.

Rows are prefixed ``fpa_`` (tenant ``fpa_user``) and cleaned by that prefix.
"""
from __future__ import annotations

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile

_P = "fpa_"


@pytest.mark.parametrize("raw,want", [
    ("github.com/karthiklucky1", "https://github.com/karthiklucky1"),
    ("linkedin.com/in/amruthaluri/", "https://linkedin.com/in/amruthaluri/"),
    ("www.example.dev", "https://www.example.dev"),
    ("https://x.io/a", "https://x.io/a"),
    ("http://x.io", "http://x.io"),
    ("", ""),
    ("   ", ""),
    ("not a url", "not a url"),        # never invented
    ("mailto:a@b.co", "mailto:a@b.co"),
    ("@handle", "@handle"),
])
def test_profile_links_get_a_scheme_only_when_they_are_web_addresses(raw, want):
    from app.api.server import _with_scheme
    assert _with_scheme(raw) == want


UID = "fpa_user"


@pytest.fixture()
def client(monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from app.api import server

    class _NoThread(threading.Thread):
        def start(self):            # never run a real tailor in tests
            pass
    monkeypatch.setattr(threading, "Thread", _NoThread)
    # Its own tenant: the "local" dev tenant resolves to whichever profile row
    # comes first, which another file may have created.
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    return TestClient(server.app)


@pytest.fixture()
def app_id():
    """The live test's profile and one application, both this file's own."""
    with get_session() as s:
        s.add(UserProfile(
            user_id=UID, first_name="Test", last_name="User", email="t@example.com",
            github_url="github.com/karthiklucky1", linkedin_url="linkedin.com/in/amruthaluri/",
            portfolio_url="", availability="2 weeks", work_authorization="F-1 OPT",
            work_auth_status="OPT", ead_end_date="",
            requires_sponsorship=False))     # the tick the dashboard showed but never saved
        j = Job(user_id=UID, source=JobSource.LEVER, external_id=_P + "1", company="Loft",
                title="Ground Software Engineer", url="https://jobs.lever.co/loft/1",
                description="d")
        s.add(j)
        s.commit()
        s.refresh(j)
        a = Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, user_id=UID)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
    yield aid
    with get_session() as s:
        jids = [r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.exec(delete(UserProfile).where(UserProfile.user_id == UID))
        s.commit()


def test_the_pack_answers_what_the_live_test_left_blank(client, app_id):
    r = client.get(f"/api/fill-pack/{app_id}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["github_url"] == "https://github.com/karthiklucky1"
    assert d["linkedin_url"] == "https://linkedin.com/in/amruthaluri/"
    assert d["portfolio_url"] == ""
    assert d["availability"] == "2 weeks"
    assert d["requires_sponsorship"] is True, "OPT will need sponsorship: Yes, never blank or No"
    # No EAD end date: "authorized today?" stays the applicant's, with the reason.
    assert d["authorized_now"] is None
    assert "end date" in d["work_auth_note"]
