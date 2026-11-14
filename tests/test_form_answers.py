"""AI answers for the questions a form asks that nothing else answers
(founder decision 2026-11-14; app/autofill/form_answers.py).

A run over 16 real application forms left 74 required fields open, about 80%
of them questions only that employer asks. The rules this file pins:

* a question the applicant alone may answer never reaches a model: work
  authorization, self-identification, "how did you hear", an agreement or
  consent, age, criminal history, clearance, an identity number;
* an answer needs ONE line of evidence copied from the candidate's own facts,
  and an option question's answer is one of the form's own options. No
  evidence, no answer: a blank costs one click, a wrong answer is sent to an
  employer under the applicant's name;
* a free-text answer is copied from its evidence, never composed;
* the facts sent carry no email, phone, street address, work-authorization
  status or self-identification answer;
* the same breaker, platform budget, per-user daily cap and spend ledger as
  every other paid call; a provider problem leaves the question for the
  person, exactly as before.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api import server
from app.autofill import form_answers as fa
from app.config import settings

FACTS = [
    "Home location: Cincinnati, OH",
    "Current job title: Senior Software Engineer",
    "Total years of professional experience: 7",
    "Job held: Senior Software Engineer at Globex, Cincinnati, OH (2021 to Present)",
    "Employers worked for (complete list): Globex; Initech",
    "Desired salary per year: USD 150,000 to 190,000",
    "Saved answer, Willing to travel?: Yes",
]


# ── what never reaches a model ───────────────────────────────────────────────

@pytest.mark.parametrize("question,why", [
    ("Are you legally authorized to work in the United States?", "work_authorization"),
    ("Will you now or in the future require sponsorship?", "work_authorization"),
    ("What is your gender?", "self_identification"),
    ("Are you a protected veteran?", "self_identification"),
    ("How did you hear about this role?", "applicant_fact"),
    ("I agree to the terms and conditions", "agreement"),
    ("Do you consent to the processing of your personal data?", "agreement"),
    ("By submitting this form I certify that my answers are true", "agreement"),
    ("Are you willing to undergo a background check and drug test?", "agreement"),
    ("I would like to receive text messages about jobs", "agreement"),
    ("Have you ever been convicted of a felony?", "applicant_only"),
    ("Are you at least 18 years of age?", "applicant_only"),
    ("Do you hold an active TS/SCI clearance?", "applicant_only"),
    ("What is your date of birth?", "applicant_only"),
    ("Are you a U.S. citizen?", "applicant_only"),
    ("Are you related to anyone who works here?", "applicant_only"),
    ("g-recaptcha-response", "anti_bot_field"),
    ("question_68444493", "field_identifier"),
    ("Phone", "not_a_question"),
    ("", "not_a_question"),
])
def test_a_question_only_the_applicant_may_answer_is_refused(question, why):
    assert fa.refusal(question) == why


@pytest.mark.parametrize("question", [
    "Have you worked at Neo4j before?",
    "Are you local to Austin, TX?",
    "What is your desired compensation for this position?",
    "Are you able to be in the office 3 days per week?",
    "Did a current employee refer you to this position?",
    "Which state do you plan to be physically located in?",
    "How many years of experience do you have with Python?",
    "What is your current stage of interviewing with other companies?",
])
def test_an_employers_own_question_may_be_asked(question):
    assert fa.refusal(question) is None


# ── options and evidence ─────────────────────────────────────────────────────

@pytest.mark.parametrize("options,answer,want", [
    (["Yes", "No"], "No", 1),
    (["Yes.", "No."], "yes", 0),
    (["Yes, I am", "No, I am not"], "Yes", 0),
    (["Yes - Lendable loan", "Yes - Zable credit card", "Not applicable"], "Yes", -1),
    (["None", "Not applicable", "Now"], "No", -1),
    (["None", "Secret", "Top Secret"], "Confidential", -1),
    (["Secret", "Secret"], "Secret", -1),
    (["Bachelor’s degree *", "Master’s degree"], "Bachelor's degree", 0),
    (["Yes", "No"], "", -1),
    ([], "Yes", -1),
])
def test_an_answer_is_one_of_the_forms_own_options(options, answer, want):
    assert fa.option_index(options, answer) == want


def test_evidence_is_one_line_of_the_facts_or_it_is_nothing():
    assert fa.supported_by("Home location: Cincinnati, OH", FACTS) == FACTS[0]
    assert fa.supported_by("location: cincinnati, oh", FACTS) == FACTS[0], "case and trimming do not matter"
    assert fa.supported_by("USD 150000 to 190000", FACTS) == FACTS[5], "a number is the same with or without commas"
    assert fa.supported_by("Home location: Austin, TX", FACTS) is None, "a line nobody wrote"
    assert fa.supported_by("Cincinnati, OH Current job title", FACTS) is None, "stitched across two lines"
    assert fa.supported_by("OH", FACTS) is None and fa.supported_by("", FACTS) is None, "too short to say anything"
    assert fa.supported_by(None, FACTS) is None


def test_a_text_answer_is_copied_from_its_evidence_never_composed():
    assert fa.text_supported("Cincinnati, OH", FACTS[0])
    assert fa.text_supported("150000", FACTS[5])
    assert fa.text_supported("7 years", FACTS[2])
    assert not fa.text_supported("Ohio", FACTS[0]), "a word the line does not hold"
    assert not fa.text_supported("I am based in Cincinnati and happy to relocate", FACTS[0])
    assert not fa.text_supported("200000", FACTS[5])
    assert not fa.text_supported("", FACTS[0])


# ── the facts ────────────────────────────────────────────────────────────────

def test_the_facts_carry_no_contact_identity_or_status():
    profile = SimpleNamespace(
        first_name="Alexandra", last_name="Nguyen", email="alexandra.nguyen@example.com",
        phone="+1 415 555 0199", location="Cincinnati, OH", current_title="Senior Software Engineer",
        years_experience=7, work_authorization="F-1 OPT", visa_status="OPT", requires_sponsorship=True,
        gender="Female", ethnicity="Asian", veteran_status="No", disability_status="No",
        key_skills="Python, SQL", degree="B.S. Computer Science", university="State University",
        graduation_year=2017, open_to_relocation=True, salary_min=150000, salary_max=190000,
        salary_currency="USD", availability="2 weeks", work_modes="remote,hybrid", autofill_json=None)
    autofill = {"address_line1": "500 Market Street", "postal_code": "45202", "city": "Cincinnati",
                "state": "Ohio", "over_18": "Yes", "felony_conviction": "No", "security_clearance": "Secret",
                "drivers_license": "Yes", "willing_to_travel": "Yes", "citizenship_country": "India",
                "languages": [{"language": "English", "proficiency": "Fluent"}]}
    history = {"work_experience": [{"company": "Globex", "title": "Senior Software Engineer",
                                    "start_date": "2021", "end_date": "Present", "location": "Cincinnati, OH"},
                                   {"company": "Initech", "title": "Engineer", "start_date": "2017", "end_date": "2021"}],
               "education": [{"school": "State University", "degree": "B.S.", "field_of_study": "Computer Science",
                              "end_date": "2017"}]}
    resume = ("Alexandra Nguyen\nalexandra.nguyen@example.com | +1 415 555 0199\n"
              "Built Python services used by 40 teams at Globex.\n")
    lines = fa.build_facts(profile, autofill=autofill, history=history, resume_text=resume)
    text = "\n".join(lines)
    for secret in ("alexandra.nguyen@example.com", "415 555", "500 Market", "45202", "F-1", "OPT", "Female",
                   "Asian", "India", "Secret", "felony", "18 years"):
        assert secret.lower() not in text.lower(), f"{secret!r} must not be sent to a model"
    assert "Home location: Cincinnati, OH" in lines
    assert "Employers worked for (complete list): Globex; Initech" in lines
    assert any(l.startswith("Job held: Senior Software Engineer at Globex") for l in lines)
    assert any(l.startswith("Saved answer, ") and l.endswith(": Yes") and "travel" in l.lower() for l in lines)
    assert any("driver" in l.lower() and l.endswith(": Yes") for l in lines)
    assert any("State / province" in l and l.endswith("Ohio") for l in lines)
    assert any("English" in l and "Fluent" in l for l in lines)
    assert "Resume: Built Python services used by 40 teams at Globex." in lines
    assert any(l == "Desired salary per year: USD 150,000 to 190,000" for l in lines)


# ── the call ─────────────────────────────────────────────────────────────────

class _Model:
    """Stands in for the provider: answers from a table, records what it saw."""

    def __init__(self, table):
        self.table, self.prompts, self.fail = table, [], False

    def __call__(self, provider, model, client, user):
        self.prompts.append(user)
        if self.fail:
            raise RuntimeError("provider down")
        asked = json.loads(user.split("QUESTIONS:\n", 1)[1].split("\n\nReturn", 1)[0])
        rows = []
        for q in asked:
            for needle, (answer, evidence) in self.table.items():
                if needle in q["question"].lower():
                    rows.append({"id": q["id"], "answer": answer, "evidence": evidence})
        return json.dumps({"answers": rows}), {"input": 900, "output": 80}, "served-model"


@pytest.fixture()
def wired(monkeypatch):
    """A fresh user, the FACTS above, a table-driven model, and every paid-call
    control observable."""
    import uuid

    from app.analytics import spend
    from app.common import daily_counter
    from app.matching import reranker
    uid = "fa-" + uuid.uuid4().hex[:10]
    model = _Model({})
    seen = SimpleNamespace(uid=uid, model=model, reserved=[], spend=[], reserve_ok=True)
    monkeypatch.setattr(fa, "_load_context", lambda app_id, user_id: {
        "job": {"company": "Neo4j", "title": "Engineer", "location": "Austin, TX"}, "lines": list(FACTS)})
    monkeypatch.setattr(fa, "_backend", lambda: ("anthropic", "configured-model", object()))
    monkeypatch.setattr(fa, "_call_model", model)
    monkeypatch.setattr(reranker, "llm_budget_exhausted", lambda: False)
    monkeypatch.setattr(daily_counter, "reserve",
                        lambda name, cap, day=None: (seen.reserved.append((name, cap)), seen.reserve_ok)[1])
    monkeypatch.setattr(spend, "record_llm_spend", lambda user_id, kind, calls=1, **kw: seen.spend.append((user_id, kind, kw)))
    monkeypatch.setattr(settings, "ai_form_answers_enabled", True)
    return seen


QUESTIONS = [
    {"id": "q1", "question": "Have you worked at Neo4j before?", "kind": "dropdown", "options": ["Yes", "No"]},
    {"id": "q2", "question": "Are you local to Austin, TX?", "kind": "radio", "options": ["Yes.", "No."]},
    {"id": "q3", "question": "What is your desired compensation?", "kind": "text", "options": []},
    {"id": "q4", "question": "Are you legally authorized to work in the US?", "kind": "radio", "options": ["Yes", "No"]},
    {"id": "q5", "question": "Do you have a loan with us?", "kind": "radio", "options": ["Yes", "No"]},
    {"id": "q6", "question": "I agree to the privacy policy", "kind": "radio", "options": ["Yes", "No"]},
]


def test_answers_come_with_evidence_or_not_at_all(wired):
    wired.model.table.update({
        "worked at neo4j": ("No", "Employers worked for (complete list): Globex; Initech"),
        "local to austin": ("No", "Home location: Cincinnati, OH"),
        "desired compensation": ("150000", "Desired salary per year: USD 150,000 to 190,000"),
        "legally authorized": ("Yes", "Home location: Cincinnati, OH"),      # must never even be asked
        "loan with us": ("No", "The candidate has no loan"),                  # evidence nobody wrote
    })
    out = fa.answer_questions(QUESTIONS, application_id=1, user_id=wired.uid)
    assert out["answers"]["q1"] == {"answer": "No", "option_index": 1,
                                    "basis": "Employers worked for (complete list): Globex; Initech"}
    assert out["answers"]["q2"]["answer"] == "No." and out["answers"]["q2"]["option_index"] == 1
    assert out["answers"]["q3"] == {"answer": "150000", "option_index": None,
                                    "basis": "Desired salary per year: USD 150,000 to 190,000"}
    assert set(out["answers"]) == {"q1", "q2", "q3"}
    assert out["skipped"] == {"q4": "work_authorization", "q5": "unsupported", "q6": "agreement"}
    # One call; the refused questions were never in it.
    assert len(wired.model.prompts) == 1
    sent = wired.model.prompts[0]
    assert "legally authorized" not in sent and "privacy policy" not in sent
    assert "FACTS:\nHome location: Cincinnati, OH" in sent and '"company": "Neo4j"' in sent
    # ...and it was paid for like every other call.
    assert wired.reserved == [(f"form_answers:user:{wired.uid}", int(settings.ai_form_answers_daily_cap))]
    assert [(u, k) for u, k, _ in wired.spend] == [(wired.uid, "form_answers")]
    assert wired.spend[0][2]["provider"] == "anthropic" and wired.spend[0][2]["model"] == "served-model"


@pytest.mark.parametrize("answer,evidence,why", [
    ("Maybe", "Home location: Cincinnati, OH", "not_an_option"),
    ("No", "Home location: Austin, TX", "unsupported"),
    ("No", None, "unsupported"),
    (None, "Home location: Cincinnati, OH", "no_evidence"),
    ("", "Home location: Cincinnati, OH", "no_evidence"),
])
def test_an_answer_that_does_not_stand_is_left_for_the_person(wired, answer, evidence, why):
    wired.model.table["local to austin"] = (answer, evidence)
    out = fa.answer_questions([QUESTIONS[1]], application_id=1, user_id=wired.uid)
    assert out == {"answers": {}, "skipped": {"q2": why}}


@pytest.mark.parametrize("answer,evidence", [
    ("I would expect around 150000 depending on the role", "Desired salary per year: USD 150,000 to 190,000"),
    ("200000", "Desired salary per year: USD 150,000 to 190,000"),
    ("I'd be happy to help, but the question appears incomplete", "Desired salary per year: USD 150,000 to 190,000"),
])
def test_a_composed_text_answer_is_dropped(wired, answer, evidence):
    wired.model.table["desired compensation"] = (answer, evidence)
    out = fa.answer_questions([QUESTIONS[2]], application_id=1, user_id=wired.uid)
    assert out["answers"] == {} and out["skipped"]["q3"] in ("unsupported", "not_an_answer")


def test_a_question_is_bought_once(wired):
    wired.model.table["local to austin"] = ("No", "Home location: Cincinnati, OH")
    first = fa.answer_questions(QUESTIONS[1:2] + QUESTIONS[4:5], application_id=1, user_id=wired.uid)
    again = fa.answer_questions(QUESTIONS[1:2] + QUESTIONS[4:5], application_id=1, user_id=wired.uid)
    assert first == again and first["answers"]["q2"]["answer"] == "No." and first["skipped"] == {"q5": "no_evidence"}
    assert len(wired.model.prompts) == 1 and len(wired.reserved) == 1, "the copilot re-runs a page many times"


def test_every_paid_call_control_can_stop_it(wired, monkeypatch):
    from app.matching import reranker
    q = [QUESTIONS[1]]
    wired.model.table["local to austin"] = ("No", "Home location: Cincinnati, OH")

    monkeypatch.setattr(settings, "ai_form_answers_enabled", False)
    assert fa.answer_questions(q, application_id=1, user_id=wired.uid) == {"answers": {}, "skipped": {"q2": "disabled"}}
    monkeypatch.setattr(settings, "ai_form_answers_enabled", True)

    monkeypatch.setattr(fa, "_backend", lambda: None)                       # breaker open / no key
    assert fa.answer_questions(q, application_id=1, user_id=wired.uid)["skipped"] == {"q2": "unavailable"}
    monkeypatch.setattr(fa, "_backend", lambda: ("anthropic", "m", object()))

    monkeypatch.setattr(reranker, "llm_budget_exhausted", lambda: True)
    assert fa.answer_questions(q, application_id=1, user_id=wired.uid)["skipped"] == {"q2": "platform_budget"}
    monkeypatch.setattr(reranker, "llm_budget_exhausted", lambda: False)

    wired.reserve_ok = False
    assert fa.answer_questions(q, application_id=1, user_id=wired.uid)["skipped"] == {"q2": "daily_cap"}
    wired.reserve_ok = True
    assert not wired.model.prompts, "none of those reached the provider"

    wired.model.fail = True
    assert fa.answer_questions(q, application_id=1, user_id=wired.uid)["skipped"] == {"q2": "error"}
    assert not wired.spend, "a call that failed is not booked"
    wired.model.fail = False
    out = fa.answer_questions(q, application_id=1, user_id=wired.uid)
    assert out["answers"]["q2"]["answer"] == "No.", "a failure is not remembered as 'no answer'"


def test_the_caller_cannot_flood_it(wired, monkeypatch):
    monkeypatch.setattr(settings, "ai_form_answers_max_questions", 3)
    many = [{"id": f"q{i}", "question": f"Is option number {i} right for you today?", "kind": "radio",
             "options": ["Yes", "No"]} for i in range(10)]
    many += [{"id": "q0", "question": "a duplicate id is dropped", "options": ["Yes", "No"]}, "not a dict",
             {"id": "long", "question": "x " * 400, "kind": "weird", "options": ["o" * 500] * 50}]
    cleaned = fa.clean_questions(many, 99)
    assert [q["id"] for q in cleaned[:10]] == [f"q{i}" for i in range(10)] and len(cleaned) == 11
    assert len(cleaned[-1]["question"]) <= fa.MAX_QUESTION_CHARS and cleaned[-1]["kind"] == "text"
    assert len(cleaned[-1]["options"]) <= fa.MAX_OPTIONS and len(cleaned[-1]["options"][0]) <= fa.MAX_OPTION_CHARS
    out = fa.answer_questions(many, application_id=1, user_id=wired.uid)
    assert len(out["skipped"]) == 3, "only the first max_questions are considered"


# ── the route, the fill pack, the queue ──────────────────────────────────────

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(server.app)


def test_the_route_is_the_owners_and_hands_over_what_it_was_asked(client, monkeypatch):
    from sqlmodel import delete

    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus, Job, JobSource
    seen = {}
    monkeypatch.setattr(fa, "answer_questions",
                        lambda asked, application_id, user_id: seen.update(asked=asked, app=application_id, uid=user_id)
                        or {"answers": {"q1": {"answer": "No", "option_index": 1, "basis": "x"}}, "skipped": {}})
    with get_session() as s:
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id="fa-route", company="FA Co",
                  title="Engineer", url="https://boards.greenhouse.io/fa/jobs/1", description="A role.")
        s.add(job); s.commit(); s.refresh(job)
        application = Application(job_id=job.id, status=ApplicationStatus.SHORTLISTED)
        s.add(application); s.commit(); s.refresh(application)
        app_id, job_id = application.id, job.id
    try:
        body = {"app_id": app_id, "questions": [{"id": "q1", "question": "Have you worked here before?",
                                                 "kind": "radio", "options": ["Yes", "No"]}]}
        r = client.post("/api/answer-form-questions", json=body)
        assert r.status_code == 200 and r.json()["answers"]["q1"]["answer"] == "No"
        assert seen["app"] == app_id and seen["uid"] is None and seen["asked"][0]["question"] == "Have you worked here before?"
        assert client.post("/api/answer-form-questions", json=dict(body, app_id=987654321)).status_code == 404
        # The fill pack tells the extension whether it may ask at all, and that
        # an unconfirmed AI answer holds an automatic submit.
        monkeypatch.setattr(server, "_tailor_and_settle", lambda *a, **k: True)
        pack = client.get(f"/api/fill-pack/{app_id}").json()
        assert pack["ai_form_answers"] == {"enabled": bool(settings.ai_form_answers_enabled),
                                           "max_questions": int(settings.ai_form_answers_max_questions),
                                           "auto_submit": False}
    finally:
        with get_session() as s:
            s.exec(delete(Application).where(Application.id == app_id))
            s.exec(delete(Job).where(Job.id == job_id))
            s.commit()


def test_a_person_looks_once_before_an_ai_answer_is_sent_for_them():
    assert settings.ai_form_answers_auto_submit is False, "the default: the person checks an AI answer once"
    assert "ai_review" in server._AQ_REASON_TEXT and "Check them once" in server._AQ_REASON_TEXT["ai_review"]
    from pathlib import Path
    content = (Path(__file__).resolve().parents[1] / "extension/content.js").read_text()
    assert "return { reason: 'ai_review'" in content and "ai_review:" in content
    assert "pack.ai_form_answers.auto_submit === true" in content, "only the server lifts the hold"
