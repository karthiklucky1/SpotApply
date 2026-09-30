"""What the fill pack tells the extension about the applicant (2026-09-30).

The extension audit traced three wrong answers to what the pack did or did not
carry:

- the extension hardcoded "United States" into every country dropdown because
  the pack had no residence at all, so a Toronto profile was filed as a US
  resident — the pack now carries ``residence_country``, only when the user's
  own location STATES it;
- "N years of <skill>?" was answered from total tenure because nothing better
  was sent — the pack now carries ``skill_months``, the months of paid work the
  résumé dates for each skill (tailoring/inventory.py);
- a degree-SUBJECT question read an extractor field that is inferred, not
  stated — ``degree_fields`` carries only what the profile records.

And one money promise: choosing "Original resume" never starts a tailoring
generation — the résumé route honoured it, the fill pack did not.
Synthetic profiles only.
"""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from app.api import server
from app.common.geo import stated_country


@pytest.mark.parametrize("location,want", [
    ("Toronto, ON, Canada", "Canada"),
    ("Cincinnati, OH", "United States"),
    ("Remote, USA", "United States"),
    ("London, UK", "United Kingdom"),
    ("Bangalore, India", "India"),
    ("Dublin", ""),            # a city name is a guess, not a statement
    ("Toronto, ON", ""),
    ("", ""),
])
def test_residence_is_what_the_location_states(location, want):
    assert server._residence_country(SimpleNamespace(location=location)) == want
    assert server._residence_country(None) == ""


def test_stated_country_never_uses_the_city_tier():
    from app.common.geo import detect_country
    assert detect_country("Dublin") == "ireland"          # the scorer's best guess…
    assert stated_country("Dublin") == ""                 # …is not a fact about a person


def test_residence_is_not_the_job_country():
    """preferred_country is where the user wants WORK; it must not leak in."""
    prof = SimpleNamespace(location="", preferred_country="United States")
    assert server._residence_country(prof) == ""


def test_degree_fields_come_from_the_profile_only():
    prof = SimpleNamespace(education_json=json.dumps([
        {"degree": "B.A.", "field": "Economics"},
        {"degree": "M.S.", "field": "Computer Science"},
        {"degree": "M.S.", "field": "computer science"},       # duplicate, other case
        {"degree": "Certificate"},
    ]))
    assert server._profile_degree_fields(prof) == ["Economics", "Computer Science"]
    assert server._profile_degree_fields(SimpleNamespace(education_json="not json")) == []
    assert server._profile_degree_fields(None) == []


def test_skill_months_are_the_dated_months_only(monkeypatch):
    from app.common import ttl_cache
    import app.matching.pipeline as pipeline
    ttl_cache.invalidate("skill-months:")
    resume = ("## Experience\n**Python Developer** | Beta | Jan 2018 - Dec 2018\n"
              "- Maintained internal tooling.\n"
              "**Engineer** | Acme | Jan 2020 - Dec 2025\n"
              "- Mentioned Kubernetes once in a six-year role.\n")
    monkeypatch.setattr(pipeline, "_load_resume_file", lambda user_id=None: resume)
    months = server._skill_months_for_pack("fp-user", "Python, Kubernetes")
    assert months == {"python": 12}, "a skill merely mentioned in a long role is not its length"
    ttl_cache.invalidate("skill-months:")


def test_skill_months_fail_quietly(monkeypatch):
    from app.common import ttl_cache
    import app.matching.pipeline as pipeline
    ttl_cache.invalidate("skill-months:")

    def _boom(user_id=None):
        raise RuntimeError("storage down")
    monkeypatch.setattr(pipeline, "_load_resume_file", _boom)
    assert server._skill_months_for_pack("fp-user", "Python") == {}
    ttl_cache.invalidate("skill-months:")


def test_the_pack_carries_the_new_facts():
    src = inspect.getsource(server.get_fill_pack)
    for key in ('"residence_country"', '"degree_fields"', '"skill_months"'):
        assert key in src, key


def test_original_resume_never_starts_a_tailoring_generation():
    """The auto-tailor thread in get_fill_pack must sit behind the preference."""
    src = inspect.getsource(server.get_fill_pack)
    guard = src.index('resume_source == "original"')
    spend = src.index("_check_tailor_limit(uid")   # the call, not the comment above it
    assert guard < spend
    # and it is the SAME resolver the résumé route uses
    assert "_autofill_resume_source(session, uid)" in src


def test_original_resume_skips_the_thread(monkeypatch):
    """Behaviour, not just order: with "original" chosen no generation starts."""
    started = []
    monkeypatch.setattr(server, "_check_tailor_limit",
                        lambda uid: started.append(uid) or (True, "", None))
    monkeypatch.setattr(server, "_tailor_and_settle", lambda *a, **k: started.append("tailor"))
    monkeypatch.setattr(server, "_autofill_resume_source", lambda session, uid: "original")

    from sqlmodel import delete
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus, Job, JobSource
    with get_session() as s:
        s.exec(delete(Application).where(Application.notes == "fp-test"))
        s.exec(delete(Job).where(Job.external_id == "fp-orig"))
        j = Job(user_id="local", source=JobSource.GREENHOUSE, external_id="fp-orig", company="Co",
                title="Engineer", url="https://x/fp", description="d")
        s.add(j)
        s.flush()
        a = Application(user_id="local", job_id=j.id, status=ApplicationStatus.SHORTLISTED,
                        notes="fp-test")
        s.add(a)
        s.commit()
        app_id = a.id
    try:
        from fastapi.testclient import TestClient
        r = TestClient(server.app).get(f"/api/fill-pack/{app_id}")
        assert r.status_code == 200, r.text
        assert started == [], "choosing the original resume started a paid generation"
        body = r.json()
        assert "residence_country" in body and "skill_months" in body
    finally:
        with get_session() as s:
            s.exec(delete(Application).where(Application.notes == "fp-test"))
            s.exec(delete(Job).where(Job.external_id == "fp-orig"))
            s.commit()


# ── authorized today: the status AND its end date ───────────────────────────
# Found checking a real account (2026-09-30): "F-1 OPT" with an EAD end date
# already passed. The pack sent only the status text, and the extension read it
# as "authorized in the US" — a Yes on employer forms the user must answer.

def _auth_profile(**kw):
    base = dict(work_authorization="", work_auth_status="", visa_status="", ead_end_date="",
                stem_opt=False, requires_sponsorship=False, preferred_country="United States")
    base.update(kw)
    return SimpleNamespace(**base)


def test_an_expired_dated_status_is_left_for_the_user():
    p = _auth_profile(work_authorization="F-1 OPT", work_auth_status="OPT", ead_end_date="2020-06-18")
    assert server._authorized_now_for_pack(p) is None


def test_a_current_dated_status_is_authorized():
    from datetime import date, timedelta
    p = _auth_profile(work_authorization="F-1 OPT", work_auth_status="OPT",
                      ead_end_date=(date.today() + timedelta(days=300)).isoformat())
    assert server._authorized_now_for_pack(p) is True


def test_an_undated_status_answers_from_the_status():
    assert server._authorized_now_for_pack(_auth_profile(work_authorization="US Citizen")) is True
    assert server._authorized_now_for_pack(_auth_profile()) is None
    assert server._authorized_now_for_pack(None) is None


def test_the_pack_carries_the_verdict():
    assert '"authorized_now"' in inspect.getsource(server.get_fill_pack)
