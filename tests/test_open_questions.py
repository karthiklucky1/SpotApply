"""Questions to answer once (founder, 2026-11-15; app/autofill/open_questions.py).

"What data do we need to take from the user?" is answered by the forms: the
extension reports every REQUIRED question still open after a fill, and the
person answers each ONCE on their profile. The rules pinned here:

* what is listed is the form's own wording and choices, never a value;
* an answer is saved as the person's own (AnswerMemory, the key the extension
  recalls by) only for a question that may be reused: never work
  authorization, self-identification or "how did you hear" (their own place
  in the profile), never an agreement or consent (accepted on the form it
  belongs to), never an identity number or date of birth (not stored), never
  a question about THIS job or employer;
* an answer to a question with choices is one of those choices;
* one person's list never shows another's.

Rows this file writes carry the ``oq-`` prefix and are removed by it.
"""
from __future__ import annotations

import pytest
from sqlmodel import delete, select

from app.api import server
from app.autofill import open_questions as oq
from app.db.init_db import get_session
from app.db.models import AnswerMemory, Application, ApplicationStatus, Job, JobSource, PendingQuestion

_P = "oq-"


def _wipe():
    with get_session() as s:
        job_ids = list(s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all())
        if job_ids:
            app_ids = list(s.exec(select(Application.id).where(Application.job_id.in_(job_ids))).all())
            if app_ids:
                s.exec(delete(PendingQuestion).where(PendingQuestion.application_id.in_(app_ids)))
                s.exec(delete(Application).where(Application.id.in_(app_ids)))
            s.exec(delete(Job).where(Job.id.in_(job_ids)))
        s.exec(delete(AnswerMemory).where(AnswerMemory.label_original.like(f"{_P}%")))
        s.exec(delete(AnswerMemory).where(AnswerMemory.user_id.like(f"{_P}%")))
        s.commit()


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    yield
    _wipe()


def _seed(tag: str, company: str, user_id=None) -> int:
    with get_session() as s:
        job = Job(user_id=user_id, source=JobSource.GREENHOUSE, external_id=f"{_P}{tag}", company=company,
                  title="Engineer", url=f"https://boards.greenhouse.io/{tag}/jobs/1", description="A role.")
        s.add(job); s.commit(); s.refresh(job)
        a = Application(job_id=job.id, status=ApplicationStatus.SHORTLISTED, user_id=user_id)
        s.add(a); s.commit(); s.refresh(a)
        return a.id


def _q(key, question=None, kind="radio", options=("Yes", "No")):
    return {"key": f"{_P}{key}", "question": question or key, "kind": kind, "options": list(options)}


# ── what may be saved for reuse ──────────────────────────────────────────────

@pytest.mark.parametrize("question,kind,want", [
    ("Are you willing to travel up to 25%?", "radio", (True, "")),
    ("Do you have a valid driver's license?", "radio", (True, "")),
    ("Are you subject to a non-compete agreement?", "radio", (True, "")),          # a fact about them, not an agreement
    ("Are you at least 18 years of age?", "radio", (True, "")),                    # their own statement
    ("Have you ever been convicted of a felony?", "radio", (True, "")),
    ("Current clearance level", "dropdown", (True, "")),
    ("Does your name appear on any sex offender database in any state or country?", "radio", (True, "")),
    ("Are you able to legally work in the region you are applying for?", "dropdown", (False, "work_authorization")),
    ("Do you have a loan/credit card/auto loan with us? If yes - which product?", "radio", (False, "job_specific")),
    ("What is your desired compensation?", "text", (True, "")),
    ("Are you local to Austin, TX?", "radio", (True, "")),
    ("Are you legally authorized to work in the United States?", "radio", (False, "work_authorization")),
    ("Will you require sponsorship?", "radio", (False, "work_authorization")),
    ("What is your gender?", "select", (False, "self_identification")),
    ("How did you hear about this role?", "text", (False, "applicant_fact")),
    ("I agree to the terms and conditions", "radio", (False, "agreement")),
    ("Do you consent to the processing of your personal data?", "radio", (False, "agreement")),
    ("(Required) Allow us to process your personal information.", "checkbox", (False, "agreement")),
    ("Virginia (DCJS)", "checkbox", (False, "agreement")),                         # a box is ticked on its own form
    ("What is your date of birth?", "text", (False, "never_stored")),
    ("Social Security Number", "text", (False, "never_stored")),
    ("Have you worked here before?", "radio", (False, "job_specific")),
    ("Did a current employee refer you to this position?", "dropdown", (False, "job_specific")),
    ("Can you commute to our office 3 days a week?", "radio", (False, "job_specific")),
    # A consent's legal text says "authorize": it is still a consent (a real Lever form).
    ("CONSENT TO BACKGROUND CHECK: Section 10-21.9 of The School Code and A.R.S. 15-534(A) authorize the district "
     "to check", "radio", (False, "agreement")),
    ("I certify that I am authorized to work in the United States", "radio", (False, "agreement")),
    # The profile already holds these; the form's own field did not take them.
    ("Address*Required", "text", (False, "profile")),
    ("Phone*Required", "text", (False, "profile")),
    ("Email address *", "text", (False, "profile")),
    ("What city would you relocate to?", "text", (True, "")),                      # a question, not the City box
    ("cover letter a cover letter is required", "textarea", (False, "document")),
    ("Do you have a resume gap of more than a year?", "radio", (True, "")),        # mentions a resume, asks a fact
    ("Where are you currently located? * A response is required", "text", (True, "")),
])
def test_only_a_reusable_answer_is_saved(question, kind, want):
    assert oq.classify(question, kind) == want
    assert want[1] == "" or want[1] in oq._WHERE, "every reason has words for the person"


@pytest.mark.parametrize("raw,want", [
    ("Are you okay with working in the Pacific Time Zone? * A response is required",
     "Are you okay with working in the Pacific Time Zone?"),
    ("How would you evaluate your proficiency in English?* (required)",
     "How would you evaluate your proficiency in English?"),
    ("(Required) Allow us to process your personal information. *", "Allow us to process your personal information."),
    ("Current clearance level*", "Current clearance level"),
    ("how did you learn about aledade? ✱", "How did you learn about aledade?"),
    ("Gender*Required We collect this information to support our commitment to diversity", "Gender"),
    ("What is your current Visa Status?*Required", "What is your current Visa Status?"),
    ("briefly describe your last project. 0 /", "Briefly describe your last project."),
    ("cover letter a cover letter is required", "Cover letter"),
    # A sentence that merely ends that way is the question itself.
    ("I understand that a background check is required.", "I understand that a background check is required."),
    ("Are you willing to travel?", "Are you willing to travel?"),
])
def test_the_list_shows_the_question_not_the_forms_decoration(raw, want):
    assert oq.display(raw) == want


def test_the_key_keeps_the_forms_wording_while_the_list_shows_it_clean():
    """The extension recalls a saved answer by the key it reported: only what
    the person READS is tidied."""
    app_id = _seed("deco", "OQ Decorated")
    key = f"{_P}are you ok with pacific time? * a response is required"
    oq.record(app_id, [{"key": key, "question": "Are you ok with Pacific time? * A response is required",
                        "kind": "radio", "options": ["Yes", "No"]},
                       # Several fields read as one (a repeating section) is not a question.
                       {"key": f"{_P}garble", "kind": "textarea", "question":
                        "experience work history * add position a response is required education * add education "
                        "a response is required"}])
    mine = [g for g in oq.listing(None)["questions"] if g["key"].startswith(f"{_P}")
            and ("pacific" in g["key"] or "garble" in g["key"])]
    assert [(g["key"], g["question"]) for g in mine] == [(key, "Are you ok with Pacific time?")]
    assert oq.answer(None, key, "yes")["ok"] is True
    with get_session() as s:
        saved = s.exec(select(AnswerMemory).where(AnswerMemory.label_original == key)).first()
        assert saved is not None and saved.answer == "Yes"


def test_boxes_that_are_one_question_are_listed_once_and_answered_on_the_form():
    app_id = _seed("multi", "OQ Multi")
    q = {"key": f"{_P}do you have a security license?", "kind": "multi",
         "question": "Do you have a security license? Please fill out which type below: *",
         "options": ["Virginia (DCJS)", "DC (DLCP)", "Maryland (Guard Card)", "N/A (no I don't)"]}
    assert oq.record(app_id, [q]) == 1
    mine = [g for g in oq.listing(None)["questions"] if g["key"] == q["key"]]
    assert len(mine) == 1 and mine[0]["kind"] == "multi" and mine[0]["saveable"] is False
    assert mine[0]["why"] == "pick_many" and mine[0]["where"] and len(mine[0]["options"]) == 4
    assert mine[0]["question"] == "Do you have a security license? Please fill out which type below:"
    assert oq.answer(None, q["key"], "DC (DLCP)") == {"ok": False, "reason": "pick_many"}


def test_the_server_side_agent_leaves_the_extensions_list_alone():
    """The two kinds of row share a table and an application: the agent clears
    ITS questions before a fill, never the person's list."""
    import inspect

    from app.autofill import agent
    src = inspect.getsource(agent)
    clear = src[src.index("Clear old unanswered pending questions"):]
    clear = clear[:clear.index("session.commit()")]
    assert 'startswith("ext:")' in clear and "continue" in clear


def test_in_multi_tenant_mode_no_user_reads_nothing(monkeypatch):
    """``user_id`` None is the one local user. Were a caller ever to reach this
    without a user in production, it gets an empty list, not everyone's."""
    from app.config import settings
    app_id = _seed("closed", "OQ Closed")
    oq.record(app_id, [_q("closed-q", "Are you willing to work nights?")])
    assert any(g["key"] == f"{_P}closed-q" for g in oq.listing(None)["questions"])
    monkeypatch.setattr(type(settings), "use_supabase", property(lambda self: True), raising=False)
    assert oq.listing(None) == {"questions": [], "to_answer": 0, "total": 0}
    assert oq.answer(None, f"{_P}closed-q", "Yes") == {"ok": False, "reason": "not_found"}
    assert oq.dismiss(None, f"{_P}closed-q") == {"ok": True, "forms": 0}


def test_a_report_is_the_forms_wording_bounded_and_deduplicated():
    raw = [_q("a", "Are you willing to travel?"), _q("A", "duplicate key, other case"), "not a dict",
           {"key": f"{_P}b", "question": "x"},                                       # too short to be a question
           {"key": f"{_P}c", "question": "g-recaptcha-response"},                    # a captcha field
           {"key": f"{_P}d", "question": "q " * 400, "kind": "weird", "options": ["o" * 500] * 60},
           {"key": "", "question": "no key at all"}]
    got = oq.clean_report(raw)
    assert [g["key"] for g in got] == [f"{_P}a", f"{_P}d"]
    assert len(got[1]["question"]) <= oq.MAX_QUESTION_CHARS and got[1]["kind"] == "text"
    assert len(got[1]["options"]) == 30 and len(got[1]["options"][0]) <= 160
    many = [_q(f"n{i}", f"Is number {i} right for you?") for i in range(80)]
    assert len(oq.clean_report(many)) == oq.MAX_PER_REPORT


# ── record, list, answer ─────────────────────────────────────────────────────

def test_a_form_reports_what_it_still_needs_and_drops_what_was_answered_on_it():
    app_id = _seed("one", "OQ One")
    assert oq.record(app_id, [_q("travel", "Are you willing to travel?"), _q("license", "Valid driver's license?")]) == 2
    # The server-side agent's own row for this application is not ours to touch.
    with get_session() as s:
        s.add(PendingQuestion(application_id=app_id, field_label="agent row", field_selector="#x", field_type="text"))
        s.commit()
    # The person answered one of them on the form: the next report has one left.
    assert oq.record(app_id, [_q("travel", "Are you willing to travel?")]) == 1
    with get_session() as s:
        rows = s.exec(select(PendingQuestion).where(PendingQuestion.application_id == app_id)).all()
    assert sorted((r.field_selector, r.field_type) for r in rows) == [("#x", "text"), (f"{_P}travel", "ext:radio")]


def test_one_line_per_question_however_many_forms_asked_it():
    a, b, c = _seed("a", "OQ Alpha"), _seed("b", "OQ Beta"), _seed("c", "OQ Gamma")
    oq.record(a, [_q("travel", "Are you willing to travel?"), _q("auth", "Are you authorized to work in the US?")])
    oq.record(b, [_q("TRAVEL", "Are you willing to travel?"), _q("consent", "I agree to the privacy policy", "checkbox", ())])
    oq.record(c, [_q("travel", "Are you willing to travel?"), _q("pay", "Desired compensation?", "text", ())])
    out = oq.listing(None)
    mine = [g for g in out["questions"] if g["key"].lower().startswith(_P)]
    by = {g["key"].lower(): g for g in mine}
    travel = by[f"{_P}travel"]
    assert travel["forms"] == 3 and travel["saveable"] and travel["options"] == ["Yes", "No"]
    assert set(travel["companies"]) == {"OQ Alpha", "OQ Beta", "OQ Gamma"}
    assert by[f"{_P}pay"]["saveable"] and by[f"{_P}pay"]["kind"] == "text"
    assert not by[f"{_P}auth"]["saveable"] and "work authorization" in by[f"{_P}auth"]["where"]
    assert not by[f"{_P}consent"]["saveable"] and "on the form" in by[f"{_P}consent"]["where"]
    # What can be answered comes first, the most asked first.
    assert [g["saveable"] for g in mine] == sorted((g["saveable"] for g in mine), reverse=True)
    assert mine[0]["key"].lower() == f"{_P}travel"


def test_an_answer_is_saved_as_the_persons_own_and_recalled_by_the_same_key(monkeypatch):
    from fastapi.testclient import TestClient
    a, b = _seed("a", "OQ Alpha"), _seed("b", "OQ Beta")
    q = _q("Are you willing to travel?", "Are you willing to travel?", "radio", ("Yes.", "No."))
    oq.record(a, [q])
    oq.record(b, [q])
    assert oq.answer(None, q["key"], "Maybe") == {"ok": False, "reason": "not_an_option"}
    out = oq.answer(None, q["key"], "yes")
    assert out == {"ok": True, "answer": "Yes.", "forms": 2}, "the form's own option text is what is saved"
    assert not [g for g in oq.listing(None)["questions"] if g["key"] == q["key"]], "answered: off the list"
    # The extension asks /api/recall-answers with the same key and gets it back.
    client = TestClient(server.app)
    got = client.post("/api/recall-answers", json={"labels": [q["key"]]}).json()["answers"]
    assert got == {q["key"]: "Yes."}
    assert oq.answer(None, q["key"], "No") == {"ok": False, "reason": "not_found"}, "nothing left to answer"


@pytest.mark.parametrize("question,kind,reason", [
    ("Are you authorized to work in the US?", "radio", "work_authorization"),
    ("I agree to the terms and conditions", "radio", "agreement"),
    ("Have you worked here before?", "radio", "job_specific"),
    ("Social Security Number", "text", "never_stored"),
])
def test_an_answer_that_must_not_be_reused_is_not_saved(question, kind, reason):
    app_id = _seed("x", "OQ X")
    q = _q(question, question, kind, ("Yes", "No") if kind == "radio" else ())
    oq.record(app_id, [q])
    assert oq.answer(None, q["key"], "Yes") == {"ok": False, "reason": reason}
    with get_session() as s:
        assert not s.exec(select(AnswerMemory).where(AnswerMemory.label_original == q["key"])).first()


def test_skipping_takes_a_line_off_without_saving_anything():
    app_id = _seed("s", "OQ Skip")
    q = _q("overtime", "Are you willing to work overtime?")
    oq.record(app_id, [q])
    assert oq.dismiss(None, q["key"]) == {"ok": True, "forms": 1}
    assert not [g for g in oq.listing(None)["questions"] if g["key"] == q["key"]]
    with get_session() as s:
        assert not s.exec(select(AnswerMemory).where(AnswerMemory.label_original == q["key"])).first()


def test_one_persons_list_never_shows_anothers():
    a = _seed("ua", "OQ Tenant A", user_id=f"{_P}user-a")
    b = _seed("ub", "OQ Tenant B", user_id=f"{_P}user-b")
    oq.record(a, [_q("a-only", "Are you willing to relocate for work?")])
    oq.record(b, [_q("b-only", "Do you have a forklift certificate?")])
    keys_a = {g["key"] for g in oq.listing(f"{_P}user-a")["questions"]}
    keys_b = {g["key"] for g in oq.listing(f"{_P}user-b")["questions"]}
    assert keys_a == {f"{_P}a-only"} and keys_b == {f"{_P}b-only"}
    assert oq.answer(f"{_P}user-a", f"{_P}b-only", "Yes") == {"ok": False, "reason": "not_found"}
    assert oq.dismiss(f"{_P}user-a", f"{_P}b-only") == {"ok": True, "forms": 0}
    assert {g["key"] for g in oq.listing(f"{_P}user-b")["questions"]} == {f"{_P}b-only"}


# ── the routes ───────────────────────────────────────────────────────────────

def test_the_routes_are_the_owners_and_reporting_is_not_a_person_using_the_product(monkeypatch):
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    app_id = _seed("r", "OQ Route")
    q = _q("shift", "Can you work night shifts?")
    r = client.post("/api/open-questions", json={"app_id": app_id, "host": "boards.greenhouse.io", "questions": [q]})
    assert r.status_code == 200 and r.json() == {"ok": True, "kept": 1}
    assert client.post("/api/open-questions", json={"app_id": 987654321, "questions": [q]}).status_code == 404
    listed = [g for g in client.get("/api/open-questions").json()["questions"] if g["key"] == q["key"]]
    assert listed and listed[0]["saveable"] and listed[0]["companies"] == ["OQ Route"]
    assert client.post("/api/open-questions/answer", json={"key": q["key"], "answer": "Perhaps"}).status_code == 422
    assert client.post("/api/open-questions/answer", json={"key": q["key"], "answer": "No"}).json()["answer"] == "No"
    assert client.post("/api/open-questions/answer", json={"key": q["key"], "answer": "No"}).status_code == 404
    # The extension's report is bookkeeping about a form; answering is the action.
    assert server._is_passive_write("/api/open-questions")
    assert not server._is_passive_write("/api/open-questions/answer")
    # The fill pack tells the extension it may report.
    monkeypatch.setattr(server, "_tailor_and_settle", lambda *a, **k: True)
    assert client.get(f"/api/fill-pack/{app_id}").json()["open_questions"] == {"enabled": True}
    monkeypatch.setattr(server.settings, "open_questions_enabled", False)
    assert client.post("/api/open-questions", json={"app_id": app_id, "questions": [q]}).json() == {"ok": True, "kept": 0}


# ── the job's country travels with the fill data ─────────────────────────────

@pytest.mark.parametrize("location,eligibility,want", [
    ("Austin, TX", None, "United States"),
    ("London, United Kingdom", None, "United Kingdom"),
    ("Remote", None, ""),                                   # nothing says: never guessed
    ("Remote", "eligible", "United States"),               # the location gate verified it for this user's country
    ("Remote", "unknown", ""),
    ("", "ineligible", ""),
])
def test_the_jobs_country_is_what_the_posting_says_or_nothing(location, eligibility, want):
    """"Are you authorized to work in the country this job is in?" is answered
    by the extension only when the job's country is the one the profile's
    status covers, so the server never guesses it."""
    from types import SimpleNamespace
    job = SimpleNamespace(location=location, eligibility=eligibility)
    profile = SimpleNamespace(preferred_country="United States")
    assert server._job_country(job, profile) == want
    assert server._job_country(None, profile) == ""


# ── the profile's card, and the policy that describes it ─────────────────────

def test_the_profile_shows_the_list_and_the_policy_declares_it():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    dash = (root / "app/templates/dashboard.html").read_text()
    for needle in ('id="pp-oq"', "Questions to answer once", "async function ppLoadOpenQuestions()",
                   "'/api/open-questions/answer'", "'/api/open-questions/dismiss'", "ppLoadOpenQuestions();"):
        assert needle in dash, needle
    card = dash[dash.index("function ppRenderOpenQuestions()"):dash.index("async function ppOqSave(")]
    assert "_ppE(" in card and "innerHTML" in card, "the employer's wording is escaped before it is shown"
    assert "q.where" in card, "a question that cannot be saved says where it is answered"
    privacy = (root / "app/templates/privacy.html").read_text()
    assert "Questions application forms ask" in privacy and "never sends what" in privacy
    assert "we do\n                    not sign or accept agreements for you" in privacy, "still true: nothing here accepts an agreement"
