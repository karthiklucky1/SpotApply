"""Live test 2026-10-09 (Ashby, Horizon3): what the autofill server must refuse.

The extension asked /api/answer-question about the page's HIDDEN reCAPTCHA
textarea (its name, "g-recaptcha-response", stood in for a label). The model
replied "I'd be happy to help, but I notice the essay question appears
incomplete…", the reply was CACHED in AnswerMemory, and it was typed into the
captcha field of a real application. The extension now skips such fields, but
users on the Store build only get the server's guard, so it must hold alone:

* an anti-bot field is refused BEFORE the cache, so a reply already cached
  under its key (production has exactly one) is never served again;
* a reply about the question is never returned, cached, recalled or saved;
* facts only the applicant holds (how they heard about the role) are never
  written by a model.

Also pinned: the tailored one-page PDF is the resume the extension attaches
whenever the upload field takes PDF (owner, 2026-10-09), the .docx otherwise;
and the fill pack explains why an authorization question is left blank.

Rows here are prefixed "fguard-" / user "fguard-user" and removed by that
prefix (CLAUDE.md: clean up only your own rows).
"""
from __future__ import annotations

import base64
import inspect
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.autofill import field_guards as fg
from app.db.init_db import get_session
from app.db.models import AnswerMemory, Application, ApplicationStatus, Job, JobSource

LIVE_REPLY = ("I'd be happy to help, but I notice the essay question appears incomplete or "
              "unclear. \"g-recaptcha-response\" looks like a technical parameter or form "
              "field name rather than an actual question.")
_USER = "fguard-user"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_session() as s:
        for m in s.exec(select(AnswerMemory).where(
                (AnswerMemory.user_id == _USER) | AnswerMemory.label_normalized.like("fguard%"))).all():
            s.delete(m)
        for j in s.exec(select(Job).where(Job.external_id.like("fguard-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app)


def _mk_app(path: str | None = None) -> int:
    with get_session() as s:
        j = Job(source=JobSource.ASHBY, external_id=f"fguard-{path or 'x'}", company="FGuardCo",
                title="Software Engineer", url="https://jobs.ashbyhq.com/fguard/1", description="d")
        s.add(j); s.commit(); s.refresh(j)
        a = Application(job_id=j.id, status=ApplicationStatus.TAILORED, apply_track="manual",
                        tailored_resume_path=path)
        s.add(a); s.commit(); s.refresh(a)
        return a.id


class _Prof:
    first_name = "Jane"; last_name = "Doe"; email = "jane@example.com"; phone = ""
    linkedin_url = ""; github_url = ""; portfolio_url = ""; current_title = "Engineer"
    location = "Cincinnati, OH"; university = ""; graduation_year = None; years_experience = 3
    professional_summary = ""; key_skills = "Python"


def _no_llm(*a, **k):
    raise AssertionError("the model must not be asked")


# ── the classifiers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "g-recaptcha-response", "h-captcha-response", "cf-turnstile-response",
    "frc-captcha-solution", "website_honeypot", "Leave this field blank",
    "Please leave this field empty.", "If you are human, leave this field blank",
    "Don't fill this out if you're human", "Do not fill in this field",
    "\n  Leave this\n  field blank *\n",
])
def test_anti_bot_fields_are_recognised(label):
    assert fg.is_anti_bot_field(label)
    assert fg.not_a_question_reason(label) == "anti_bot_field"


@pytest.mark.parametrize("label", [
    # Review 2026-10-09: real, visible questions that say when to leave them
    # blank. The first pattern called them honeypots, so the extension stopped
    # filling LinkedIn/GitHub fields the 1.0.0 build filled and the server
    # refused to remember what the user typed there.
    "LinkedIn profile (leave blank if none)",
    "GitHub URL - leave empty if you don't have one",
    "Leave blank if you were not referred",
    "Preferred first name - leave empty if same as legal name",
    "If you have no referral code, leave this field blank.",
    "Do not fill in if you were not referred",
])
def test_a_real_field_that_says_leave_blank_is_not_a_honeypot(label):
    assert not fg.is_anti_bot_field(label)
    assert fg.not_a_question_reason(label) is None


@pytest.mark.parametrize("label,want", [
    ("question_68444493", "field_identifier"), ("_systemfield_name", "field_identifier"),
    ("cards[abc][field0]", "field_identifier"),
    ("Comments", None), ("Why do you want to work here?", None), ("LinkedIn", None),
])
def test_field_names_are_not_questions(label, want):
    assert fg.not_a_question_reason(label) == want


@pytest.mark.parametrize("reply", [
    LIVE_REPLY,
    "I notice the question appears to be incomplete. Could you provide the full question?",
    "It seems like the question is missing. Please provide the actual question.",
    "This doesn't look like an essay question; it is a form field name.",
    "SKIP", "skip.",
    "As an AI language model, I cannot know where you heard about us.",
])
def test_replies_about_the_question_are_not_answers(reply):
    assert fg.looks_like_meta_reply(reply)


@pytest.mark.parametrize("answer", [
    "I notice patterns in messy data quickly, which is why integration work suits me.",
    "I can't wait to build connectors that security teams rely on every day.",
    "It seems natural to me to start with the customer's workflow.",
    "Over three years I built ETL pipelines and REST integrations in Python.",
    "",
    # review 2026-10-09: answers the first patterns read as replies about the question
    "I am unable to start before January 2027 because of my notice period.",
    "I'm not able to relocate, but I am happy to work remotely from Ohio.",
    "For me, choosing a team is a question of mission alignment.",
    "I tuned every technical parameter of our Kafka pipeline.",
    "As an AI engineer, I built retrieval systems for support teams.",
    "I can't help but admire how quickly the team ships.",
])
def test_real_answers_pass(answer):
    assert not fg.looks_like_meta_reply(answer)
    assert not fg.is_model_only_reply(answer)


@pytest.mark.parametrize("reply,model_only", [
    (LIVE_REPLY, True),
    ("As an AI language model, I cannot know where you heard about us.", True),
    ("The question appears to be incomplete.", True),
    # meta from a model, but a person may type these: never refused as THEIR text
    ("SKIP", False),
    ("I'm sorry, but I can't answer that without more context.", False),
    ("This doesn't look like an essay question; it is a form field name.", False),
])
def test_user_text_is_refused_only_in_forms_a_model_alone_writes(reply, model_only):
    assert fg.looks_like_meta_reply(reply)
    assert fg.is_model_only_reply(reply) is model_only


@pytest.mark.parametrize("q,why", [
    ("How did you hear about this opportunity?", "applicant_fact"),
    ("How did you first learn about Acme?", "applicant_fact"),
    ("Referral source", "applicant_fact"),
    ("Will you require visa sponsorship?", "work_authorization"),
    ("Why are you interested in this role?", None),
])
def test_facts_only_the_applicant_holds(q, why):
    assert fg.user_only_reason(q) == why


# ── answer_question: refused before the cache, never cached ───────────────────

def test_a_captcha_field_is_refused_before_the_cache(monkeypatch):
    """Production holds exactly this row (one user, id 445). It must never be
    served again, and the model must not be asked."""
    import app.autofill.answer_pack as ap
    monkeypatch.setattr(ap, "_llm_essay_answer", _no_llm)
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: _Prof)
    with get_session() as s:
        s.add(AnswerMemory(user_id=_USER, label_normalized="g-recaptcha-response",
                           label_original="g-recaptcha-response", answer=LIVE_REPLY))
        s.commit()
    aid = _mk_app()
    answer, source = ap.answer_question_with_source("g-recaptcha-response", aid, user_id=_USER)
    assert (answer, source) == ("", "refused:anti_bot_field")
    # Every other reader of AnswerMemory reads it as a miss too.
    assert ap._lookup_memory("g-recaptcha-response", user_id=_USER) is None


@pytest.mark.parametrize("question", [
    "How did you hear about this opportunity?",   # a fact only the applicant holds
    "question_68444493",                          # a field's name
    "Leave this field blank",                     # a honeypot
])
def test_the_model_is_never_asked_for_these(monkeypatch, question):
    import app.autofill.answer_pack as ap
    monkeypatch.setattr(ap, "_llm_essay_answer", _no_llm)
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: _Prof)
    monkeypatch.setattr(ap.settings, "anthropic_api_key", "test-key")
    aid = _mk_app()
    answer, source = ap.answer_question_with_source(question, aid, user_id=_USER)
    assert answer == "" and source.startswith("refused:")


@pytest.mark.parametrize("reply", [LIVE_REPLY, "SKIP"])
def test_a_reply_about_the_question_is_neither_returned_nor_cached(monkeypatch, reply):
    import app.autofill.answer_pack as ap
    monkeypatch.setattr(ap, "_llm_essay_answer", lambda *a, **k: reply)
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: _Prof)
    monkeypatch.setattr(ap.settings, "anthropic_api_key", "test-key")
    aid = _mk_app()
    q = "Describe the fguard platform you are most proud of building."
    answer, source = ap.answer_question_with_source(q, aid, user_id=_USER)
    assert (answer, source) == ("", "refused:meta_reply")
    with get_session() as s:
        assert not s.exec(select(AnswerMemory).where(AnswerMemory.user_id == _USER)).all()


def test_a_cached_meta_reply_is_replaced_not_served(monkeypatch):
    import app.autofill.answer_pack as ap
    good = "I built a connector framework that cut onboarding from weeks to days."
    monkeypatch.setattr(ap, "_llm_essay_answer", lambda *a, **k: good)
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: _Prof)
    monkeypatch.setattr(ap.settings, "anthropic_api_key", "test-key")
    aid = _mk_app()
    q = "Describe the fguard integration you are most proud of."
    key = ap._normalize_question(q, company="FGuardCo")
    with get_session() as s:
        s.add(AnswerMemory(user_id=_USER, label_normalized=key, label_original=q, answer=LIVE_REPLY))
        s.commit()
    answer, source = ap.answer_question_with_source(q, aid, user_id=_USER)
    assert (answer, source) == (good, "generated")
    assert ap._lookup_memory(key, user_id=_USER) == good


def test_save_memory_refuses_unusable_rows():
    import app.autofill.answer_pack as ap
    ap._save_memory("g-recaptcha-response", "g-recaptcha-response", "03AGdBq-token", user_id=_USER)
    ap._save_memory("fguard essay", "fguard essay", LIVE_REPLY, user_id=_USER)
    with get_session() as s:
        assert not s.exec(select(AnswerMemory).where(AnswerMemory.user_id == _USER)).all()
    # Internal keys are identifiers by design and still work.
    ap._save_memory("__fguard_internal_cache", "Internal", '{"a": 1}', user_id=_USER)
    assert ap._lookup_memory("__fguard_internal_cache", user_id=_USER) == '{"a": 1}'


# ── the routes the Store build calls ─────────────────────────────────────────

def test_answer_question_route_refuses_the_captcha_field(client, monkeypatch):
    import app.autofill.answer_pack as ap
    monkeypatch.setattr(ap, "_llm_essay_answer", _no_llm)
    aid = _mk_app()
    r = client.post("/api/answer-question", json={"question": "g-recaptcha-response", "app_id": aid})
    assert r.status_code == 200
    d = r.json()
    assert d["answer"] == "" and d["cached"] is False and d["refused"] == "anti_bot_field"


def test_recall_never_returns_captcha_labels_or_meta_replies(client):
    with get_session() as s:
        s.add(AnswerMemory(user_id=None, label_normalized="fguard why us",
                           label_original="fguard why us", answer=LIVE_REPLY))
        s.add(AnswerMemory(user_id=None, label_normalized="fguard pronouns ok",
                           label_original="fguard pronouns ok", answer="she/her"))
        s.add(AnswerMemory(user_id=None, label_normalized="fguard-recaptcha-response",
                           label_original="fguard-recaptcha-response", answer="tok"))
        s.commit()
    r = client.post("/api/recall-answers", json={
        "labels": ["fguard why us", "fguard pronouns ok", "fguard-recaptcha-response"]})
    assert r.json()["answers"] == {"fguard pronouns ok": "she/her"}


def test_save_answer_route_never_learns_a_captcha_or_a_meta_reply(client):
    r1 = client.post("/api/save-answer", json={"question": "fguard-recaptcha-response",
                                              "answer": "03AGdBq-token"})
    r2 = client.post("/api/save-answer", json={"question": "fguard essay", "answer": LIVE_REPLY})
    assert r1.json()["ok"] is False and r2.json()["ok"] is False
    with get_session() as s:
        assert not s.exec(select(AnswerMemory).where(
            AnswerMemory.label_normalized.in_(["fguard-recaptcha-response", "fguard essay"]))).all()


# ── what the USER typed stays theirs (review 2026-10-09) ─────────────────────

_TYPED = {
    "fguard notice period": "I am unable to start before January 2027 because of my notice period.",
    "fguard relocation": "I'm not able to relocate, but I am happy to work remotely from Ohio.",
    "fguard what matters": "For me, choosing a team is a question of mission alignment.",
    "fguard proudest work": "I tuned every technical parameter of our Kafka pipeline.",
    "fguard leave blank if none, linkedin": "https://linkedin.com/in/jane-doe",
}


def test_what_the_user_typed_is_saved_recalled_and_served(client, monkeypatch):
    """The meta-reply test is for MODEL output. A person's own answer that
    happens to read like one is remembered, recalled, and served from the
    cache instead of being regenerated over."""
    for q, a in _TYPED.items():
        r = client.post("/api/save-answer", json={"question": q, "answer": a})
        assert r.json() == {"ok": True}, (q, r.json())
    r = client.post("/api/recall-answers", json={"labels": list(_TYPED)})
    assert r.json()["answers"] == _TYPED

    import app.autofill.answer_pack as ap
    monkeypatch.setattr(ap, "_llm_essay_answer", _no_llm)
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: _Prof)
    monkeypatch.setattr(ap.settings, "anthropic_api_key", "test-key")
    aid = _mk_app()
    # No "?" and no company name: the essay cache key IS the saved label, so a
    # user answer read as a miss would be regenerated and overwritten.
    q = "fguard notice period"
    assert ap._normalize_question(q, company="FGuardCo") == q
    assert ap.answer_question_with_source(q, aid) == (_TYPED[q], "cache")
    assert ap._lookup_memory("fguard relocation") == _TYPED["fguard relocation"]


# ── the resume the extension attaches ────────────────────────────────────────

def _resume_app(tmp_path, with_pdf=True):
    d = tmp_path / "app_fguard"
    d.mkdir()
    docx = d / "Jane_Doe_Resume.docx"
    docx.write_bytes(b"PK\x03\x04 docx")
    if with_pdf:
        docx.with_suffix(".pdf").write_bytes(b"%PDF-1.4 one page")
    return _mk_app(str(docx))


@pytest.fixture()
def resume_route(monkeypatch):
    from app.api import server
    from app.tailoring.export_gate import ExportVerdict
    import app.autofill.answer_pack as ap
    monkeypatch.setattr(server, "_export_verdict", lambda *a, **k: ExportVerdict(True, "ok", ""))
    monkeypatch.setattr(server, "_autofill_resume_source", lambda s, u: "tailored")
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda user_id=None: _Prof)
    return server


@pytest.mark.parametrize("accept,want", [
    ("", "pdf"),                                   # the Store build sends nothing
    (".pdf,.doc,.docx", "pdf"),
    ("application/pdf", "pdf"),
    (".doc,.docx", "docx"),                        # the field rules PDF out
    ("application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
])
def test_the_one_page_pdf_is_attached_where_the_field_takes_it(client, resume_route, tmp_path, accept, want):
    aid = _resume_app(tmp_path)
    r = client.get(f"/api/fill-pack/{aid}/resume", params={"accept": accept} if accept else None)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["filename"] == f"Jane_Doe_Resume.{want}"
    if want == "pdf":
        assert d["mime"] == "application/pdf"
        assert base64.b64decode(d["base64"]).startswith(b"%PDF")
    else:
        assert d["mime"].endswith("wordprocessingml.document")
        assert base64.b64decode(d["base64"]).startswith(b"PK")


def test_old_and_new_builds_are_told_apart(client, resume_route, tmp_path, caplog):
    """1.0.1+ always sends ?accept= ("*" when the field names nothing) and
    gets the PDF; a request with no parameter is the 1.0.0 Store build. It
    gets the PDF too (the documented tradeoff: on a Word-only field it then
    attaches nothing), and that is logged so the old share is measurable."""
    import logging
    aid = _resume_app(tmp_path)
    with caplog.at_level(logging.INFO, logger="app.api.server"):
        d = client.get(f"/api/fill-pack/{aid}/resume", params={"accept": "*"}).json()
    assert d["filename"] == "Jane_Doe_Resume.pdf" and d["mime"] == "application/pdf"
    assert not [r for r in caplog.records if "no accept=" in r.getMessage()]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.api.server"):
        d = client.get(f"/api/fill-pack/{aid}/resume").json()
    assert d["filename"] == "Jane_Doe_Resume.pdf"
    assert [r for r in caplog.records if "no accept=" in r.getMessage()]
    from app.api import server
    doc = server.get_tailored_resume.__doc__
    assert "1.0.0 Store build" in doc and ".doc,.docx" in doc


def test_no_pdf_means_the_word_file(client, resume_route, tmp_path):
    aid = _resume_app(tmp_path, with_pdf=False)
    d = client.get(f"/api/fill-pack/{aid}/resume").json()
    assert d["filename"] == "Jane_Doe_Resume.docx"


def test_accept_reading_matches_the_extension():
    from app.api.server import _accept_allows
    assert _accept_allows("", ".pdf", "application/pdf")
    assert _accept_allows(".PDF, .docx", ".pdf", "application/pdf")
    assert _accept_allows("application/*", ".pdf", "application/pdf")
    assert not _accept_allows(".doc,.docx", ".pdf", "application/pdf")
    assert not _accept_allows("image/*", ".pdf", "application/pdf")


# ── the fill pack explains a blank authorization question ────────────────────

def _auth_profile(**kw):
    base = dict(work_authorization="", work_auth_status="", visa_status="",
                requires_sponsorship=False, ead_end_date="", stem_opt=False, user_id="x")
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_pack_says_why_authorization_is_left_blank():
    from app.api import server
    opt = _auth_profile(work_authorization="F-1 OPT", work_auth_status="OPT", requires_sponsorship=True)
    assert server._authorized_now_for_pack(opt) is None
    note = server._work_auth_note_for_pack(opt)
    assert "end date" in note and "—" not in note and "résumé" not in note
    assert server._work_auth_note_for_pack(_auth_profile(work_authorization="US Citizen")) == ""
    assert "work authorization" in server._work_auth_note_for_pack(_auth_profile())
    assert server._work_auth_note_for_pack(None) == ""
    assert '"work_auth_note"' in inspect.getsource(server.get_fill_pack)
