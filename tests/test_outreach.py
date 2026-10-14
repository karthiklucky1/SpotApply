"""The outreach kit (2026-10-10): a message the user can send in one click, and
whether anyone answered.

`referral.py` drafts the words. This file pins what `intelligence/outreach.py`
adds — subjects, compose and LinkedIn SEARCH links (never a guessed profile),
the people the posting itself names, reply matching — and the routes that
record a sent message and the inbox-scan hook that flags its reply.

Rows this file writes carry the ``outreachtest-`` prefix and are removed by it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.intelligence import outreach as o

# ── subjects and keys ────────────────────────────────────────────────────────

def test_subject_key_strips_prefixes_case_and_punctuation():
    a = o.subject_key("RE: Re: Quick question about the SRE role at Acme!")
    b = o.subject_key("quick question about the sre role at acme")
    c = o.subject_key("Fwd: FW:   Quick  question about the SRE role at Acme")
    assert a == b == c == "quick question about the sre role at acme"
    assert o.subject_key("") == ""


def test_subjects_are_specific_and_short():
    s = o.subject_for("hiring_manager", "Site Reliability Engineer", "Acme")
    assert s == "Site Reliability Engineer at Acme: a quick note"
    assert len(o.subject_for("referral_request", "Backend Engineer", "Globex Corporation")) < 80
    assert "—" not in s


# ── links ────────────────────────────────────────────────────────────────────

def test_linkedin_link_is_a_people_search_never_a_guessed_profile():
    url = o.linkedin_people_search("Acme Corp", "recruiter")
    assert url.startswith("https://www.linkedin.com/search/results/people/?keywords=")
    assert "Acme%20Corp%20recruiter" in url
    assert "/in/" not in url


def test_compose_links_carry_address_subject_and_body():
    links = o.compose_links("jane@acme.com", "SRE at Acme: a quick note", "Hi Jane,\nline two")
    assert links["gmail"].startswith("https://mail.google.com/mail/?view=cm&fs=1&")
    assert "su=SRE%20at%20Acme%3A%20a%20quick%20note" in links["gmail"]
    assert "body=Hi%20Jane%2C%0Aline%20two" in links["gmail"]
    assert "to=jane%40acme.com" in links["gmail"]
    assert links["outlook"].startswith("https://outlook.office.com/mail/deeplink/compose?")
    assert "subject=" in links["outlook"] and "to=jane%40acme.com" in links["outlook"]
    assert links["mailto"].startswith("mailto:jane%40acme.com?subject=")
    # No address known: the user adds one; the links still open with the text.
    assert o.compose_links("", "s", "b")["mailto"].startswith("mailto:?subject=s")


@pytest.mark.parametrize("draft,expected", [
    ({"type": "hiring_manager", "channel": "Email to the hiring manager"}, "email"),
    ({"type": "referral_request", "channel": "LinkedIn / email to a connection"}, "linkedin"),
    ({"type": "university_alumni", "channel": "LinkedIn connection request to a fellow alum"}, "linkedin"),
    ({"type": "cold_email", "channel": ""}, "email"),
    ({"type": "x", "channel": "Send an email"}, "email"),
])
def test_channel_of(draft, expected):
    assert o.channel_of(draft) == expected


# ── the people the posting names ─────────────────────────────────────────────

def test_people_come_only_from_what_the_posting_established():
    assert o.people_from_context({}, "Acme") == []
    fields = {
        "recruiter_name": {"value": "Jane Doe", "quote": "Recruiter: Jane Doe"},
        "reporting_title": {"value": "Director of Platform", "quote": "reports to the Director of Platform"},
        "contact_email": {"value": "talent@acme.com", "quote": "questions: talent@acme.com"},
    }
    people = o.people_from_context(fields, "Acme")
    labels = [(p.label, p.name, p.title, p.email) for p in people]
    assert labels == [("Recruiter", "Jane Doe", None, None),
                      ("Reports to", None, "Director of Platform", None),
                      ("Contact", None, None, "talent@acme.com")]
    assert people[0].evidence == "Recruiter: Jane Doe"
    assert "Jane%20Doe" in people[0].search_url and "/in/" not in people[0].search_url


def test_build_kit_decorates_every_draft():
    drafts = [
        {"type": "referral_request", "label": "Forward the req",
         "channel": "LinkedIn / email to a connection at the company", "body": "x" * 310},
        {"type": "hiring_manager", "label": "Hiring-manager note", "channel": "Email", "body": "hi"},
        {"type": "university_alumni", "label": "Alum", "channel": "LinkedIn connection request",
         "body": "hello", "suggested_contact": {"profile_url": "https://www.linkedin.com/in/x"}},
    ]
    kit = o.build_kit(drafts, company="Acme", role="Senior Backend Engineer", job_url="https://x/j",
                      context_fields={"recruiter_name": {"value": "Jane Doe", "quote": "Recruiter: Jane Doe"}},
                      reporting_title="Director of Platform")
    d0, d1, d2 = kit["drafts"]
    assert d0["channel_kind"] == "linkedin" and d0["over_limit"] is True and d0["chars"] == 310
    assert d0["links"]["search"].startswith("https://www.linkedin.com/search/results/people/")
    assert d1["channel_kind"] == "email" and d1["subject"] == "Senior Backend Engineer at Acme: a quick note"
    assert "su=" in d1["links"]["gmail"] and d1["links"]["mailto"].startswith("mailto:?")
    assert d2["links"]["profile"] == "https://www.linkedin.com/in/x"
    assert [p["name"] for p in kit["people"]] == ["Jane Doe"]
    assert [s["label"] for s in kit["searches"]] == [
        "Recruiters at Acme", "Director of Platform at Acme", "People in similar roles at Acme"]
    assert "Backend%20Engineer" in kit["searches"][2]["url"]       # "Senior" dropped
    assert kit["linkedin_note_limit"] == 300


# ── replies ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("addr,expected", [
    ("jane@mail.acme-corp.co.uk", "acme-corp"), ("x@acme.com", "acme"),
    ("someone@gmail.com", ""), ("bad-address", ""),
])
def test_company_domain(addr, expected):
    assert o.company_domain(addr) == expected


@pytest.mark.parametrize("addr,company,expected", [
    ("jane@acmecorp.com", "Acme Corp, Inc.", True),
    ("jane@acme-corp.com", "Acme Corp", True),
    ("jane@gmail.com", "Acme Corp", False),
    ("a@abc.com", "ABC", False),               # too short to trust
    ("jane@globex.com", "Acme Corp", False),
])
def test_domain_matches_company(addr, company, expected):
    assert o.domain_matches_company(addr, company) is expected


@pytest.mark.parametrize("addr,robot", [
    ("noreply@acme.com", True), ("no-reply@acme.com", True), ("jobs@acme.com", True),
    ("notifications@greenhouse.io", True), ("jane.doe@acme.com", False), ("jdoe@acme.com", False),
])
def test_robot_mailboxes_never_count_as_a_reply(addr, robot):
    assert o.is_robot_sender(addr) is robot


def _row(**over):
    base = dict(id=1, application_id=10, channel="email",
                subject_key=o.subject_key("Quick question about the SRE role at Acme"),
                recipient_email="jane@acme.com", sent_at=datetime(2026, 10, 1, 12, 0))
    base.update(over)
    return SimpleNamespace(**base)


COMPANY = {10: "Acme Corp"}


def test_a_reply_is_recognised_by_its_subject():
    row = _row()
    email = {"subject": "Re: Quick question about the SRE role at Acme", "sender": "someone@else.com",
             "body": "Happy to chat", "date": "2026-10-02T10:00:00Z"}
    assert o.match_reply(email, [row], company_of=COMPANY) == (row, "email_subject")


def test_a_reply_is_recognised_by_the_recipients_address():
    row = _row()
    email = {"subject": "Hello from Jane", "sender": "Jane@Acme.com", "body": "", "date": "2026-10-02T10:00:00Z"}
    assert o.match_reply(email, [row], company_of=COMPANY) == (row, "email_sender")


def test_a_person_at_the_company_writing_afterwards_is_a_likely_reply():
    row = _row()
    email = {"subject": "Your note", "sender": "bob.smith@acmecorp.com", "body": "",
             "date": "Thu, 02 Oct 2026 10:00:00 +0000"}
    assert o.match_reply(email, [row], company_of=COMPANY) == (row, "email_domain")


def test_nothing_before_the_message_went_out_is_a_reply():
    row = _row()
    email = {"subject": "Your note", "sender": "bob@acmecorp.com", "body": "", "date": "2026-09-30T10:00:00Z"}
    assert o.match_reply(email, [row], company_of=COMPANY) is None
    # Even a subject match: the thread predates our message.
    email2 = {"subject": "Re: Quick question about the SRE role at Acme", "sender": "x@y.com",
              "date": "2026-09-30T10:00:00Z"}
    assert o.match_reply(email2, [row], company_of=COMPANY) is None


def test_robots_and_strangers_are_not_replies():
    row = _row()
    assert o.match_reply({"subject": "Re: Quick question about the SRE role at Acme",
                          "sender": "noreply@acme.com"}, [row], company_of=COMPANY) is None
    assert o.match_reply({"subject": "Weekly digest", "sender": "news@globex.com"},
                         [row], company_of=COMPANY) is None
    assert o.match_reply({"subject": "Re: Quick question about the SRE role at Acme",
                          "sender": "x@y.com"}, [], company_of=COMPANY) is None


# ── routes ───────────────────────────────────────────────────────────────────

_PREFIX = "outreachtest-"
_COMPANY = "OutreachTest Co"


@pytest.fixture()
def seeded():
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus, Job, JobSource, OutreachMessage, UserNotification
    with get_session() as s:
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id=_PREFIX + "job",
                  company=_COMPANY, title="Site Reliability Engineer", url="https://x/ot",
                  description="An SRE role.")
        s.add(job)
        s.commit()
        s.refresh(job)
        app_row = Application(job_id=job.id, status=ApplicationStatus.SUBMITTED, apply_track="manual",
                              submitted_at=datetime.utcnow() - timedelta(days=3))
        s.add(app_row)
        s.commit()
        s.refresh(app_row)
        ids = (job.id, app_row.id)
    yield ids
    with get_session() as s:
        s.exec(delete(OutreachMessage).where(OutreachMessage.application_id == ids[1]))
        s.exec(delete(UserNotification).where(UserNotification.type == "outreach_reply",
                                              UserNotification.message.like(f"{_COMPANY}%")))
        s.exec(delete(Application).where(Application.job_id == ids[0]))
        s.exec(delete(Job).where(Job.id == ids[0]))
        s.commit()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.api import server

    def _reset_limiter():
        # /api/sync-emails is rate-limited per process; this file's calls must
        # not spend the allowance the email-sync tests run on afterwards.
        try:
            server._limiter.reset()
        except Exception:
            pass

    _reset_limiter()
    yield TestClient(server.app)
    _reset_limiter()


def _draft_fixture(application_id, user_id=None):
    return {"application_id": application_id, "company": _COMPANY, "title": "Site Reliability Engineer",
            "note": "Drafts only.",
            "drafts": [{"type": "referral_request", "label": "Forward the req",
                        "channel": "LinkedIn / email to a connection at the company", "body": "Hi {name}, SRE role."},
                       {"type": "hiring_manager", "label": "Hiring-manager note",
                        "channel": "Email to the hiring manager", "body": "Hello, one question."}]}


def test_the_referral_route_returns_the_kit_and_what_was_sent(client, seeded, monkeypatch):
    import app.intelligence.referral as referral
    _, app_id = seeded
    monkeypatch.setattr(referral, "generate_referral_drafts", _draft_fixture)
    r = client.get(f"/application/{app_id}/referral")
    assert r.status_code == 200, r.text
    data = r.json()
    kit = data["kit"]
    assert [d["channel_kind"] for d in kit["drafts"]] == ["linkedin", "email"]
    assert kit["drafts"][1]["subject"] == f"Site Reliability Engineer at {_COMPANY}: a quick note"
    assert kit["drafts"][1]["links"]["gmail"].startswith("https://mail.google.com/")
    assert kit["searches"][0]["url"].startswith("https://www.linkedin.com/search/results/people/")
    assert data["sent"] == []


def test_marking_a_message_sent_records_it_and_notes_the_card(client, seeded):
    from app.db.init_db import get_session
    from app.db.models import Application, OutreachMessage
    _, app_id = seeded
    r = client.post(f"/application/{app_id}/outreach", json={
        "channel": "email", "kind": "hiring_manager", "recipient_label": "Jane Doe",
        "recipient_email": "Jane@Acme.com", "subject": "Re: SRE at Acme: a quick note",
        "body": "Hello Jane, one question."})
    assert r.status_code == 200, r.text
    sent = r.json()["sent"]
    assert sent["sent_at"] and sent["replied_at"] is None and sent["channel"] == "email"
    with get_session() as s:
        row = s.get(OutreachMessage, sent["id"])
        assert row.subject_key == "sre at acme a quick note"       # prefix stripped, normalised
        assert row.recipient_email == "jane@acme.com"
        app_row = s.get(Application, app_id)
        assert "Outreach sent (email) to Jane Doe on" in (app_row.notes or "")
    assert client.get(f"/application/{app_id}/outreach").json()["sent"][0]["id"] == sent["id"]
    # An empty message cannot have been sent.
    assert client.post(f"/application/{app_id}/outreach", json={"body": "  "}).status_code == 400


def test_the_user_can_flag_and_unflag_a_reply_and_remove_a_row(client, seeded):
    _, app_id = seeded
    sent = client.post(f"/application/{app_id}/outreach",
                       json={"channel": "linkedin", "body": "Hi there"}).json()["sent"]
    r = client.post(f"/api/outreach/{sent['id']}/replied", json={"replied": True}).json()["sent"]
    assert r["replied_at"] and r["reply_how"] == "manual"
    r = client.post(f"/api/outreach/{sent['id']}/replied", json={"replied": False}).json()["sent"]
    assert r["replied_at"] is None and r["reply_how"] is None
    assert client.delete(f"/api/outreach/{sent['id']}").status_code == 200
    assert client.delete(f"/api/outreach/{sent['id']}").status_code == 404
    assert client.post(f"/api/outreach/{sent['id']}/replied", json={"replied": True}).status_code == 404


def test_another_tenant_cannot_touch_the_row(client, seeded, monkeypatch):
    from app.api import server
    from app.db.init_db import get_session
    from app.db.models import OutreachMessage
    _, app_id = seeded
    with get_session() as s:
        row = OutreachMessage(user_id=_PREFIX + "owner", application_id=app_id, channel="email",
                              body="x", sent_at=datetime.utcnow())
        s.add(row)
        s.commit()
        s.refresh(row)
        rid = row.id
    monkeypatch.setattr(server, "_require_user", lambda request: _PREFIX + "intruder")
    assert client.post(f"/api/outreach/{rid}/replied", json={"replied": True}).status_code == 404
    assert client.delete(f"/api/outreach/{rid}").status_code == 404
    with get_session() as s:
        assert s.get(OutreachMessage, rid) is not None


def test_the_inbox_scan_flags_the_reply(client, seeded):
    from app.db.init_db import get_session
    from app.db.models import Application, OutreachMessage, UserNotification
    _, app_id = seeded
    sent = client.post(f"/application/{app_id}/outreach", json={
        "channel": "email", "kind": "hiring_manager", "recipient_label": "Jane",
        "subject": "Site Reliability Engineer at OutreachTest Co: a quick note",
        "body": "Hello Jane"}).json()["sent"]
    reply = {"subject": "Re: Site Reliability Engineer at OutreachTest Co: a quick note",
             "sender": "jane.doe@outreachtestco.com", "sender_name": "Jane Doe",
             "body": "Thanks for reaching out, happy to chat this week.",
             "date": (datetime.utcnow() + timedelta(minutes=5)).isoformat() + "Z"}
    r = client.post("/api/sync-emails", json={"emails": [reply], "source": "gmail"})
    assert r.status_code == 200, r.text
    assert r.json()["replies"] == 1
    with get_session() as s:
        row = s.get(OutreachMessage, sent["id"])
        assert row.replied_at is not None and row.reply_how == "email_subject"
        assert row.reply_subject.startswith("Re: Site Reliability Engineer")
        app_row = s.get(Application, app_id)
        assert "Reply to your outreach (email) found in your inbox on" in (app_row.notes or "")
        notes = s.exec(select(UserNotification).where(UserNotification.type == "outreach_reply",
                                                      UserNotification.message.like(f"{_COMPANY}%"))).all()
        assert len(notes) == 1 and f"/dashboard?app={app_id}" == notes[0].link
    # The same inbox scanned again stacks nothing.
    r = client.post("/api/sync-emails", json={"emails": [reply], "source": "gmail"})
    assert r.json()["replies"] == 0


def test_an_ats_mailer_is_never_a_reply(client, seeded):
    from app.db.init_db import get_session
    from app.db.models import OutreachMessage
    _, app_id = seeded
    sent = client.post(f"/application/{app_id}/outreach", json={
        "channel": "email", "subject": "SRE at OutreachTest Co: a quick note", "body": "Hello"}).json()["sent"]
    ack = {"subject": "Re: SRE at OutreachTest Co: a quick note",
           "sender": "no-reply@us.greenhouse-mail.io", "body": "Thank you for applying.",
           "date": (datetime.utcnow() + timedelta(minutes=5)).isoformat() + "Z"}
    r = client.post("/api/sync-emails", json={"emails": [ack], "source": "gmail"})
    assert r.json()["replies"] == 0
    with get_session() as s:
        assert s.get(OutreachMessage, sent["id"]).replied_at is None


def test_the_table_cascades_with_the_application():
    from sqlmodel import SQLModel
    t = SQLModel.metadata.tables["outreach_messages"]
    assert "user_id" in t.columns                       # the schema-driven purge finds it
    fks = list(t.foreign_keys)
    assert [fk.target_fullname for fk in fks] == ["application.id"]
    assert fks[0].ondelete == "CASCADE"


def test_user_facing_copy_says_resume_and_never_sends_for_the_user():
    from pathlib import Path
    html = Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html"
    src = html.read_text()
    i = src.index("function renderOutreachKit")
    block = src[i:i + 9000]
    assert "résumé" not in block
    assert "Mark as sent" in block and "Find them on LinkedIn" in block and "Gmail" in block
    assert "Replies are flagged by your inbox scan" in src
