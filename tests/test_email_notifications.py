"""Email notifications (app/common/notify_email.py, 2026-10-15).

The dashboard bell only reached users who had the dashboard open: of 312
notifications in the 30 days before this, 2 were read. The dispatcher emails
each user their unread notifications — batched, capped, never while they are
on the dashboard, never after they turned emails off — to their CONFIRMED login
address, with a signed one-click unsubscribe and the postal address.

Rows this file writes carry the ``emailnotiftest-`` prefix and are removed by it.
Every pass is scoped with ``only_users`` so other files' notifications are
never touched.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete, select

from app.api import server
from app.common import notify_email as ne
from app.config import settings
from app.db.init_db import get_session
from app.db.models import UserNotification, UserProfile

A = "emailnotiftest-a"
B = "emailnotiftest-b"
USERS = [A, B]
ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "app" / "templates" / "dashboard.html").read_text()
PRIVACY = (ROOT / "app" / "templates" / "privacy.html").read_text()


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "email_notifications_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_link_secret", "test-link-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://app.spotapply.ai")
    monkeypatch.setattr(settings, "email_notification_types", "fresh_job,high_match,feed_paused")
    monkeypatch.setattr(settings, "email_max_per_user_daily", 3)
    monkeypatch.setattr(settings, "email_min_gap_minutes", 120)
    monkeypatch.setattr(settings, "email_max_age_hours", 12)
    monkeypatch.setattr(settings, "email_skip_if_active_minutes", 15)
    with get_session() as s:
        for uid in USERS:
            s.add(UserProfile(user_id=uid, email=f"resume-{uid}@example.com"))
        s.commit()
    yield
    with get_session() as s:
        s.exec(delete(UserNotification).where(UserNotification.user_id.in_(USERS)))
        s.exec(delete(UserProfile).where(UserProfile.user_id.in_(USERS)))
        s.commit()


def _notify(uid, type_="fresh_job", title="⚡ New match found for you",
            message="Backend Engineer @ Acme — found 5m ago, fit 88. Tailor and apply now.",
            link="/dashboard?fresh_job=1", read=False, age_hours=0.0):
    with get_session() as s:
        n = UserNotification(user_id=uid, title=title, message=message, type=type_, link=link,
                             read=read, created_at=datetime.utcnow() - timedelta(hours=age_hours))
        s.add(n)
        s.commit()
        s.refresh(n)
        return n.id


def _profile(uid, **fields):
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).one()
        for k, v in fields.items():
            setattr(p, k, v)
        s.add(p)
        s.commit()


def _get_profile(uid):
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).one()
        s.expunge(p)
        return p


def _states(uid):
    with get_session() as s:
        return [n.email_state for n in s.exec(
            select(UserNotification).where(UserNotification.user_id == uid)
            .order_by(UserNotification.id)).all()]


class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, to, subject, html_body, text_body, headers):
        if self.fail:
            raise ne.EmailSendError("resend 422: domain not verified")
        self.sent.append(SimpleNamespace(to=to, subject=subject, html=html_body,
                                         text=text_body, headers=headers))
        return "msg_1"


def _run(outbox, now=None, lookup=None, reserve=None):
    return ne.dispatch_pending(
        now=now, send=outbox, lookup=lookup or (lambda uid: f"login-{uid}@example.com"),
        reserve=reserve or (lambda: True), only_users=USERS)


# ── when nothing may be sent ────────────────────────────────────────────────

def test_nothing_is_sent_or_marked_without_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "")
    _notify(A)
    out = Outbox()
    stats = _run(out)
    assert stats["configured"] is False and out.sent == []
    assert _states(A) == [None]


def test_without_a_signing_key_nothing_is_sent(monkeypatch):
    # No working unsubscribe link = no email.
    monkeypatch.setattr(settings, "email_link_secret", "")
    monkeypatch.setattr(settings, "supabase_service_role_key", "")
    assert ne.is_configured() is False


def test_the_kill_switch_stops_everything(monkeypatch):
    monkeypatch.setattr(settings, "email_notifications_enabled", False)
    _notify(A)
    out = Outbox()
    _run(out)
    assert out.sent == [] and _states(A) == [None]


# ── batching, scope, types ──────────────────────────────────────────────────

def test_a_users_unread_alerts_go_out_as_one_email():
    for i in range(3):
        _notify(A, message=f"Role {i} @ Acme — found 5m ago, fit 80. Tailor and apply now.")
    out = Outbox()
    stats = _run(out)
    assert stats["sent"] == 1 and len(out.sent) == 1
    mail = out.sent[0]
    assert mail.to == f"login-{A}@example.com"
    assert mail.subject == "3 new job matches for you"
    assert all(f"Role {i} @ Acme" in mail.text for i in range(3))
    assert _states(A) == ["sent"] * 3
    p = _get_profile(A)
    assert p.notification_emails_today == 1 and p.last_notification_email_at is not None


def test_each_email_holds_only_its_own_users_notifications():
    _notify(A, message="Alpha Role @ AlphaCo — found 5m ago, fit 90.")
    _notify(B, message="Beta Role @ BetaCo — found 5m ago, fit 91.")
    out = Outbox()
    _run(out)
    by_to = {m.to: m for m in out.sent}
    assert set(by_to) == {f"login-{A}@example.com", f"login-{B}@example.com"}
    assert "AlphaCo" in by_to[f"login-{A}@example.com"].text
    assert "BetaCo" not in by_to[f"login-{A}@example.com"].text
    assert "BetaCo" in by_to[f"login-{B}@example.com"].text
    assert "AlphaCo" not in by_to[f"login-{B}@example.com"].text
    # The unsubscribe link is the recipient's own.
    assert ne.unsubscribe_token(A) in by_to[f"login-{A}@example.com"].text
    assert ne.unsubscribe_token(B) not in by_to[f"login-{A}@example.com"].text


def test_only_listed_types_unread_and_recent_notifications_are_emailed():
    _notify(A, type_="tailor_failed", title="Tailoring failed", message="x")
    _notify(A, read=True)
    _notify(A, age_hours=13)
    out = Outbox()
    _run(out)
    assert out.sent == []
    assert _states(A) == [None, None, None]


def test_one_match_names_the_job_and_a_paused_search_says_so():
    _notify(A)
    _notify(B, type_="feed_paused", title="Your job search is paused",
            message="Your search is paused to save resources while you're away.")
    out = Outbox()
    _run(out)
    subjects = {m.to: m.subject for m in out.sent}
    assert subjects[f"login-{A}@example.com"] == "New job match: Backend Engineer at Acme"
    assert subjects[f"login-{B}@example.com"] == "Your SpotApply job search is paused"


# ── the user's choices and the caps ─────────────────────────────────────────

def test_a_user_who_turned_emails_off_or_unsubscribed_gets_none():
    _profile(A, email_notifications=False)
    _profile(B, email_unsubscribed_at=datetime.utcnow())
    _notify(A)
    _notify(B)
    out = Outbox()
    stats = _run(out)
    assert out.sent == [] and stats["skipped"] == 2
    assert _states(A) == ["skipped"] and _states(B) == ["skipped"]


def test_no_email_while_the_user_is_on_the_dashboard_but_it_waits():
    now = datetime.utcnow()
    _profile(A, last_active_at=now - timedelta(minutes=3))
    _notify(A)
    out = Outbox()
    assert _run(out, now=now)["deferred"] == 1 and out.sent == []
    assert _states(A) == [None]
    later = now + timedelta(minutes=30)
    _run(out, now=later)
    assert len(out.sent) == 1 and _states(A) == ["sent"]


def test_emails_are_spaced_out_and_capped_per_day(monkeypatch):
    # 01:00 UTC today, so the whole sequence stays inside one UTC day.
    now = datetime.utcnow().replace(hour=1, minute=0, second=0, microsecond=0)
    out = Outbox()
    _notify(A)
    _run(out, now=now)
    _notify(A, message="Second Role @ Acme — found 1m ago, fit 85.")
    _run(out, now=now + timedelta(minutes=30))           # inside the 2 h gap
    assert len(out.sent) == 1
    _run(out, now=now + timedelta(minutes=125))
    assert len(out.sent) == 2 and "Second Role" in out.sent[1].text
    # Daily cap
    monkeypatch.setattr(settings, "email_max_per_user_daily", 2)
    _notify(A, message="Third Role @ Acme — found 1m ago, fit 85.")
    _run(out, now=now + timedelta(minutes=260))
    assert len(out.sent) == 2


def test_no_confirmed_address_skips_and_an_auth_outage_defers():
    _notify(A)
    _notify(B)

    def lookup(uid):
        if uid == A:
            return None
        raise RuntimeError("auth down")
    out = Outbox()
    stats = _run(out, lookup=lookup)
    assert out.sent == [] and stats["skipped"] == 1 and stats["deferred"] == 1
    assert _states(A) == ["skipped"] and _states(B) == [None]


def test_a_refused_send_is_recorded_and_not_retried_every_pass():
    _notify(A)
    stats = _run(Outbox(fail=True))
    assert stats["failed"] == 1 and _states(A) == ["failed"]
    out = Outbox()
    _run(out)
    assert out.sent == []


def test_the_platform_daily_cap_stops_the_pass_without_marking():
    _notify(A)
    out = Outbox()
    stats = _run(out, reserve=lambda: False)
    assert stats["platform_capped"] is True and out.sent == [] and _states(A) == [None]


# ── what the email says ─────────────────────────────────────────────────────

def test_every_email_can_be_unsubscribed_in_one_click_and_names_the_company():
    _notify(A)
    out = Outbox()
    _run(out)
    mail = out.sent[0]
    url = ne.unsubscribe_url(A)
    assert url.startswith("https://app.spotapply.ai/email/unsubscribe?u=")
    assert url in mail.text and url.replace("&", "&amp;") in mail.html
    assert mail.headers["List-Unsubscribe"].startswith(f"<{url}>")
    assert mail.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    for body in (mail.text, mail.html):
        assert "SpotApply LLC" in body and "3227 Bishop Street" in body
        assert "Email settings" in body
    # The logo is the PNG app icon on our own host (email clients drop SVG).
    assert '<img src="https://app.spotapply.ai/static/apple-touch-icon.png"' in mail.html
    assert 'alt="SpotApply"' in mail.html
    assert (ROOT / "app" / "static" / "apple-touch-icon.png").is_file()


def test_notification_text_is_escaped_and_links_stay_on_our_site():
    _notify(A, title="<script>alert(1)</script>", message="<b>x</b> @ Co — y",
            link="https://evil.example/phish")
    out = Outbox()
    _run(out)
    mail = out.sent[0]
    assert "<script>" not in mail.html and "&lt;script&gt;" in mail.html
    assert "evil.example" not in mail.html and "evil.example" not in mail.text
    assert "https://app.spotapply.ai/dashboard" in mail.html


def test_the_recipient_is_the_confirmed_login_email_not_the_resume_email(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://x.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "srk")
    users = {
        A: SimpleNamespace(email="login-a@example.com", email_confirmed_at="2026-10-01T00:00:00Z"),
        # A confirmed PHONE sets Auth's confirmed_at; it proves nothing about the address.
        B: SimpleNamespace(email="login-b@example.com", email_confirmed_at=None,
                           confirmed_at="2026-10-01T00:00:00Z"),
    }
    fake = SimpleNamespace(auth=SimpleNamespace(admin=SimpleNamespace(
        get_user_by_id=lambda uid: SimpleNamespace(user=users[uid]))))
    import app.db.supabase_client as sc
    monkeypatch.setattr(sc, "service_client", lambda: fake)
    assert ne.recipient_for(A) == "login-a@example.com"
    assert ne.recipient_for(B) is None            # unconfirmed: never emailed


# ── unsubscribe and settings routes ─────────────────────────────────────────

def test_unsubscribe_link_shows_a_page_and_only_the_post_unsubscribes():
    c = TestClient(server.app)
    tok = ne.unsubscribe_token(A)
    page = c.get(f"/email/unsubscribe?u={A}&t={tok}")
    assert page.status_code == 200 and "Unsubscribe" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert _get_profile(A).email_notifications is not False      # GET changed nothing
    done = c.post(f"/email/unsubscribe?u={A}&t={tok}")
    assert done.status_code == 200 and "You're unsubscribed" in done.text
    p = _get_profile(A)
    assert p.email_notifications is False and p.email_unsubscribed_at is not None


def test_mail_clients_one_click_post_unsubscribes():
    c = TestClient(server.app)
    r = c.post(f"/email/unsubscribe?u={B}&t={ne.unsubscribe_token(B)}",
               content="List-Unsubscribe=One-Click",
               headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and _get_profile(B).email_notifications is False


def test_a_wrong_or_borrowed_token_changes_nothing():
    c = TestClient(server.app)
    for t in ("", "deadbeef", ne.unsubscribe_token(B), "é" * 32):   # B's token used for A
        assert c.get(f"/email/unsubscribe?u={A}&t={t}").status_code == 400
        assert c.post(f"/email/unsubscribe?u={A}&t={t}").status_code == 400
    assert _get_profile(A).email_notifications is not False


def test_a_provider_error_never_puts_the_address_in_the_log(monkeypatch):
    import httpx

    class Resp:
        status_code = 422
        text = '{"name":"validation_error","message":"Invalid `to` field: someone@example.com"}'

        def json(self):
            return {"name": "validation_error", "message": "Invalid `to` field: someone@example.com"}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp())
    with pytest.raises(ne.EmailSendError) as err:
        ne.send_email("someone@example.com", "s", "<p>h</p>", "t")
    assert str(err.value) == "resend 422 validation_error"
    assert "@" not in str(err.value)


def test_settings_switch_turns_emails_off_and_back_on(monkeypatch):
    monkeypatch.setattr(server, "_require_user", lambda request: A)
    c = TestClient(server.app)
    assert c.get("/api/email-notifications").json() == {"enabled": True, "available": True}
    assert c.put("/api/email-notifications", json={"enabled": False}).json() == {"enabled": False}
    assert c.get("/api/email-notifications").json()["enabled"] is False
    _profile(A, email_unsubscribed_at=datetime.utcnow())
    assert c.put("/api/email-notifications", json={"enabled": True}).status_code == 200
    p = _get_profile(A)
    assert p.email_notifications is True and p.email_unsubscribed_at is None
    assert c.put("/api/email-notifications", json={"enabled": "yes"}).status_code == 400


def test_settings_routes_require_a_user():
    for fn in (server.email_notifications_state, server.set_email_notifications):
        assert "_require_user(request)" in inspect.getsource(fn)


# ── owner test email ────────────────────────────────────────────────────────

def test_owner_test_email_goes_only_to_the_owners_own_address(monkeypatch):
    from fastapi import HTTPException
    from app.common import daily_counter
    sent = []
    monkeypatch.setattr(ne, "send_email", lambda to, subject, h, t, headers=None: sent.append((to, subject, headers)) or "id")
    monkeypatch.setattr(daily_counter, "reserve", lambda name, cap: True)
    monkeypatch.setattr(server, "_get_user_id", lambda request: A)
    c = TestClient(server.app)

    def not_admin(request):
        raise HTTPException(status_code=403, detail="Admin access only.")
    monkeypatch.setattr(server, "_require_admin_user", not_admin)
    assert c.post("/api/admin/email-test").status_code == 403 and sent == []

    monkeypatch.setattr(server, "_require_admin_user", lambda request: "owner@example.com")
    r = c.post("/api/admin/email-test")
    assert r.status_code == 200 and r.json() == {"sent": True, "to": "owner@example.com"}
    assert sent[0][0] == "owner@example.com" and sent[0][1].startswith("[Test] ")
    assert sent[0][2]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert _states(A) == []                    # no notification written or marked

    monkeypatch.setattr(settings, "resend_api_key", "")
    assert c.post("/api/admin/email-test").status_code == 503


# ── wiring ──────────────────────────────────────────────────────────────────

def test_the_dispatcher_runs_only_in_the_lanes_process():
    # Two replicas must never both send: it starts after the web-only return.
    boot = Path(server.__file__).read_text()
    lanes_return = boot.index("if not settings.lanes_enabled:\n        return")
    assert boot.index("asyncio.create_task(_email_notifications())") > lanes_return


def test_dashboard_has_the_switch_and_privacy_names_the_provider():
    assert 'onclick="toggleEmailNotifications()"' in DASH and 'id="email-notif-state"' in DASH
    assert "settings') === 'notifications'" in DASH
    assert "JobAgent — discovery complete" not in DASH
    assert "<strong>Resend</strong>" in PRIVACY and "unsubscribe link" in PRIVACY
