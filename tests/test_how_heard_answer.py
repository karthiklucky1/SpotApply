"""How did you hear about this opportunity? (owner, 2026-10-09).

A per-user profile answer, and the ONLY source the extension answers that form
question from: never a remembered answer, never a model (field_guards keeps
refusing it as a fact only the applicant holds). Empty, the default, means the
applicant answers it on each form.

The option matching runs in Node against the real content.js
(tests/test_extension_rules.py); the page-level fills run in Chromium
(extension-tests/test_extension_forms.py). Rows here carry the "hh-" prefix
and are removed by it; the shared dev profile's field is restored as found.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "app" / "templates" / "dashboard.html").read_text()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app
    return TestClient(app)


@pytest.fixture()
def restore_profile(client):
    """Put the shared dev profile's answer back exactly as it was."""
    before = client.get("/api/profile").json().get("how_heard_answer", "")
    yield
    assert client.put("/api/profile", json={"how_heard_answer": before}).status_code == 200


# ── the profile field ────────────────────────────────────────────────────────

def test_empty_unless_the_user_saves_one():
    assert UserProfile.model_fields["how_heard_answer"].default == ""
    assert UserProfile().how_heard_answer == ""


def test_the_answer_round_trips_trimmed_to_one_line(client, restore_profile):
    r = client.put("/api/profile", json={"how_heard_answer": "  Company \n\t website  "})
    assert r.status_code == 200, r.text
    assert client.get("/api/profile").json()["how_heard_answer"] == "Company website"
    # Clearing it hands the question back to the applicant.
    assert client.put("/api/profile", json={"how_heard_answer": "   "}).status_code == 200
    assert client.get("/api/profile").json()["how_heard_answer"] == ""


def test_saving_other_fields_keeps_it(client, restore_profile):
    assert client.put("/api/profile", json={"how_heard_answer": "Referral"}).status_code == 200
    assert client.put("/api/profile", json={"availability": "2 weeks"}).status_code == 200
    assert client.get("/api/profile").json()["how_heard_answer"] == "Referral"


def test_an_answer_too_long_for_a_form_is_refused_not_stored(client, restore_profile):
    from app.api.server import HOW_HEARD_MAX_LEN
    assert client.put("/api/profile", json={"how_heard_answer": "LinkedIn"}).status_code == 200
    r = client.put("/api/profile", json={"how_heard_answer": "x" * (HOW_HEARD_MAX_LEN + 1)})
    assert r.status_code == 422
    assert client.get("/api/profile").json()["how_heard_answer"] == "LinkedIn"
    ok = "y" * HOW_HEARD_MAX_LEN
    assert client.put("/api/profile", json={"how_heard_answer": ok}).status_code == 200
    assert client.get("/api/profile").json()["how_heard_answer"] == ok


def test_the_column_is_in_every_hand_written_migration_list():
    from app.api.server import _USERPROFILE_COLUMNS
    assert "how_heard_answer" in [c for c, _s, _p in _USERPROFILE_COLUMNS]
    src = (ROOT / "app" / "db" / "init_db.py").read_text()
    assert '("how_heard_answer", "VARCHAR DEFAULT \'\'")' in src


# ── the fill pack (extension) and the answer pack (manual track) ─────────────

@pytest.fixture()
def local_app(client):
    """An application of the local dev tenant, and that tenant's profile row
    (the fill pack reads user_id == "local"), restored or removed after."""
    with get_session() as s:
        s.exec(delete(Application).where(Application.notes == "hh-test"))
        s.exec(delete(Job).where(Job.external_id == "hh-job"))
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
        created = prof is None
        before = None if created else prof.how_heard_answer
        if created:
            prof = UserProfile(user_id="local")
            s.add(prof)
        j = Job(user_id="local", source=JobSource.GREENHOUSE, external_id="hh-job", company="HHCo",
                title="Engineer", url="https://boards.greenhouse.io/hhco/jobs/1", description="d")
        s.add(j)
        s.flush()
        a = Application(user_id="local", job_id=j.id, status=ApplicationStatus.SHORTLISTED,
                        notes="hh-test")
        s.add(a)
        s.commit()
        app_id = a.id

    def set_answer(value: str) -> None:
        with get_session() as s:
            p = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
            p.how_heard_answer = value
            s.add(p)
            s.commit()

    yield app_id, set_answer
    with get_session() as s:
        s.exec(delete(Application).where(Application.notes == "hh-test"))
        s.exec(delete(Job).where(Job.external_id == "hh-job"))
        p = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
        if p is not None:
            if created:
                s.delete(p)
            else:
                p.how_heard_answer = before or ""
                s.add(p)
        s.commit()


def test_the_fill_pack_carries_the_saved_answer(client, local_app, monkeypatch):
    from app.api import server
    # No paid generation from a test: the background tailor stays off.
    monkeypatch.setattr(server, "_autofill_resume_source", lambda session, uid: "original")
    app_id, set_answer = local_app
    set_answer("Company website")
    r = client.get(f"/api/fill-pack/{app_id}")
    assert r.status_code == 200, r.text
    assert r.json()["how_heard_answer"] == "Company website"
    set_answer("")
    assert client.get(f"/api/fill-pack/{app_id}").json()["how_heard_answer"] == ""


def test_the_answer_pack_reads_the_profile_and_never_memory(local_app, monkeypatch):
    import app.autofill.answer_pack as ap
    app_id, _set = local_app
    monkeypatch.setattr(ap, "_llm_essay_answer", lambda *a, **k: "")
    monkeypatch.setattr(ap, "_get_or_extract_experience_education",
                        lambda *a, **k: {"work_experience": [], "education": []})
    # Another form's remembered answer must never stand in for the profile's.
    monkeypatch.setattr(ap, "_lookup_memory", lambda label, user_id=None: "REMEMBERED")

    def field(answer: str) -> str:
        monkeypatch.setattr(ap, "_get_or_create_profile",
                            lambda user_id=None: UserProfile(user_id="local", how_heard_answer=answer))
        pack = ap.generate_answer_pack(app_id, user_id="local")
        return next(f["value"] for f in pack["standard_fields"]
                    if f["label"] == "How did you hear about us?")

    assert field("Job board") == "Job board"
    assert field("") == ""


def test_the_model_is_never_asked_even_with_an_answer_saved(local_app, monkeypatch):
    """The saved answer reaches forms through the fill pack. The essay route
    keeps refusing the question (field_guards), so no model ever writes it."""
    import app.autofill.answer_pack as ap
    app_id, _set = local_app

    def _no_llm(*a, **k):
        raise AssertionError("the model must not be asked")
    monkeypatch.setattr(ap, "_llm_essay_answer", _no_llm)
    monkeypatch.setattr(ap, "_get_or_create_profile",
                        lambda user_id=None: UserProfile(user_id="local", how_heard_answer="LinkedIn"))
    monkeypatch.setattr(ap.settings, "anthropic_api_key", "test-key")
    for q in ("How did you hear about this opportunity?", "Where did you see this job posted?"):
        assert ap.answer_question_with_source(q, app_id, user_id="local") == \
            ("", "refused:applicant_fact")


# ── the dashboard ────────────────────────────────────────────────────────────

def test_the_profile_form_offers_it_beside_the_resume_title_option():
    from app.api.server import HOW_HEARD_MAX_LEN
    start = DASH.index('id="profile-form"')
    form = DASH[start:DASH.index("</form>", start)]
    title = form.index('name="resume_title_from_jd"')
    field = form.index('name="how_heard_answer"')
    assert title < field < form.index('id="reloc-options"'), "it sits next to the resume title option"
    block = form[form.rindex("<div>", 0, field):form.index("</datalist>", field)]
    assert "How did you hear about us?" in block and "(application forms)" in block
    assert 'list="how-heard-options"' in block and f'maxlength="{HOW_HEARD_MAX_LEN}"' in block
    for opt in ("Company website", "Job board", "LinkedIn", "Indeed", "Referral", "Other"):
        assert f'<option value="{opt}"></option>' in block, opt
    help_text = ("Used only when a form asks how you heard about the job. Leave it empty to "
                 "answer that question yourself each time.")
    assert help_text in form
    added = block + help_text
    assert "—" not in added and "résumé" not in added.lower()
