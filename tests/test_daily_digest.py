"""The daily digest email (founder, 2026-10-28).

New matches used to be emailed as they arrived. Now the default is ONE email a
day, at the hour the user picks (morning / midday / evening) in THEIR time
zone, with their best new shortlisted jobs — at most ten, best fit first.
"Every new match" stays an option, and so does Off.

Rows this file writes carry the ``digesttest-`` prefix and are removed by it.
Every pass is scoped with ``only_users``.
"""
from __future__ import annotations

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
from app.db.models import (Application, ApplicationStatus, Job, JobSource, UserNotification,
                           UserProfile)

A = "digesttest-a"
B = "digesttest-b"
USERS = [A, B]
ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / "app" / "templates" / "dashboard.html").read_text()

# 13:30 UTC on 2026-10-28 is 09:30 in New York (EDT, UTC-4).
NY_MORNING = datetime(2026, 10, 28, 13, 30)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "email_notifications_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_link_secret", "test-link-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://app.spotapply.ai")
    monkeypatch.setattr(settings, "email_notification_types", "fresh_job,high_match,feed_paused")
    monkeypatch.setattr(settings, "email_default_frequency", "daily")
    monkeypatch.setattr(settings, "email_digest_default_hour", 9)
    monkeypatch.setattr(settings, "email_digest_max_jobs", 10)
    monkeypatch.setattr(settings, "email_digest_window_hours", 6)
    monkeypatch.setattr(settings, "email_skip_if_active_minutes", 15)
    with get_session() as s:
        for uid in USERS:
            s.add(UserProfile(user_id=uid, email_timezone="America/New_York",
                              preferred_country="United States"))
        s.commit()
    yield
    with get_session() as s:
        s.exec(delete(UserNotification).where(UserNotification.user_id.in_(USERS)))
        s.exec(delete(Application).where(Application.user_id.in_(USERS)))
        s.exec(delete(Job).where(Job.user_id.in_(USERS)))
        s.exec(delete(UserProfile).where(UserProfile.user_id.in_(USERS)))
        s.commit()


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


_n = iter(range(10_000))


def _match(uid, score, *, title=None, company="Acme", delivered=NY_MORNING - timedelta(hours=3),
           status=ApplicationStatus.SHORTLISTED, track="autofill", closed=False):
    with get_session() as s:
        k = next(_n)
        job = Job(user_id=uid, source=JobSource.GREENHOUSE, external_id=f"digesttest-{k}",
                  company=company, title=title or f"Engineer {k}", url=f"https://x/d{k}",
                  description="A role.", location="Boston, MA", rerank_score=score,
                  is_closed=closed, first_seen=delivered - timedelta(hours=1))
        s.add(job)
        s.commit()
        s.refresh(job)
        s.add(Application(user_id=uid, job_id=job.id, status=status, apply_track=track,
                          created_at=delivered))
        s.commit()
        return job.id


class Outbox:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, html_body, text_body, headers):
        self.sent.append(SimpleNamespace(to=to, subject=subject, html=html_body,
                                         text=text_body, headers=headers))
        return "msg_1"


def _run(outbox, now=NY_MORNING, users=USERS):
    return ne.dispatch_digests(now=now, send=outbox, lookup=lambda uid: f"login-{uid}@example.com",
                               reserve=lambda: True, only_users=users)


# ── when it goes out ────────────────────────────────────────────────────────

def test_due_only_inside_the_chosen_local_hour_window_and_once_a_day():
    p = SimpleNamespace(email_timezone="America/New_York", preferred_country="",
                        email_digest_hour=9, last_digest_day="")
    assert ne.digest_due(p, datetime(2026, 10, 28, 12, 59)) is None      # 08:59 local
    assert ne.digest_due(p, datetime(2026, 10, 28, 13, 0)) == "2026-10-28"
    assert ne.digest_due(p, datetime(2026, 10, 28, 18, 59)) == "2026-10-28"   # 14:59
    assert ne.digest_due(p, datetime(2026, 10, 28, 19, 0)) is None       # window over
    p.last_digest_day = "2026-10-28"
    assert ne.digest_due(p, datetime(2026, 10, 28, 13, 30)) is None


def test_the_zone_comes_from_the_browser_else_the_country_never_a_bad_name():
    def tz(**kw):
        base = dict(email_timezone="", preferred_country="")
        base.update(kw)
        return str(ne.timezone_of(SimpleNamespace(**base)))
    assert tz(email_timezone="Asia/Kolkata") == "Asia/Kolkata"
    assert tz(preferred_country="India") == "Asia/Kolkata"
    assert tz(email_timezone="Mars/Olympus", preferred_country="United Kingdom") == "Europe/London"
    assert tz() == "America/New_York"
    for bad in ("../../etc/passwd", "/etc/localtime", "x" * 80, "", None):
        assert ne.valid_timezone(bad) is None


def test_defaults_are_a_morning_daily_summary(monkeypatch):
    blank = SimpleNamespace(email_frequency=None, email_digest_hour=None)
    assert ne.frequency_of(blank) == "daily" and ne.digest_hour_of(blank) == 9
    assert ne.frequency_of(SimpleNamespace(email_frequency="instant")) == "instant"
    assert ne.digest_hour_of(SimpleNamespace(email_digest_hour=99)) == 9


# ── what it holds ───────────────────────────────────────────────────────────

def test_one_email_with_the_best_ten_new_matches_best_first():
    for score in range(70, 82):                      # 12 new matches
        _match(A, score)
    out = Outbox()
    stats = _run(out, users=[A])
    assert stats["sent"] == 1 and len(out.sent) == 1
    mail = out.sent[0]
    assert mail.to == f"login-{A}@example.com"
    assert mail.subject == "Your 10 best new job matches today"
    assert mail.html.count("View job →") == 10
    assert "81% fit" in mail.text and "70% fit" not in mail.text and "71% fit" not in mail.text
    assert mail.text.index("81% fit") < mail.text.index("72% fit")
    assert "…and 2 more new matches on your dashboard." in mail.text
    assert "Boston, MA" in mail.text
    assert mail.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/email/unsubscribe?u=" in mail.html and "Unsubscribe" in mail.text


def test_only_new_open_shortlisted_jobs_spotapply_delivered():
    keep = _match(A, 80, title="Kept Role")
    _match(A, 99, title="Old Role", delivered=NY_MORNING - timedelta(hours=30))
    _match(A, 98, title="Applied Role", status=ApplicationStatus.SUBMITTED)
    _match(A, 97, title="Imported Role", track="email_import")
    _match(A, 96, title="Closed Role", closed=True)
    _match(B, 95, title="Someone Elses Role")
    out = Outbox()
    _run(out, users=[A])
    assert len(out.sent) == 1
    mail = out.sent[0]
    assert mail.subject == "Your new match today: Kept Role at Acme"
    for gone in ("Old Role", "Applied Role", "Imported Role", "Closed Role", "Someone Elses Role"):
        assert gone not in mail.html, gone
    assert f"/dashboard?fresh_job={keep}" in mail.html


def test_once_a_day_and_the_next_digest_starts_where_this_one_ended():
    _match(A, 80)
    out = Outbox()
    _run(out, users=[A])
    assert len(out.sent) == 1
    assert _get_profile(A).last_digest_day == "2026-10-28"
    _match(A, 90, delivered=NY_MORNING + timedelta(minutes=10))
    _run(out, now=NY_MORNING + timedelta(hours=1), users=[A])
    assert len(out.sent) == 1, "one digest a day"
    # Next morning: only what arrived after yesterday's digest.
    _run(out, now=NY_MORNING + timedelta(days=1), users=[A])
    assert len(out.sent) == 2
    assert "90% fit" in out.sent[1].text and "80% fit" not in out.sent[1].text


def test_no_new_matches_means_no_email_and_the_day_stays_open():
    out = Outbox()
    stats = _run(out, users=[A])
    assert stats["empty"] == 1 and out.sent == []
    assert _get_profile(A).last_digest_day == ""
    _match(A, 85, delivered=NY_MORNING + timedelta(minutes=5))
    _run(out, now=NY_MORNING + timedelta(minutes=10), users=[A])
    assert len(out.sent) == 1


def test_instant_off_and_unsubscribed_users_get_no_digest():
    _match(A, 80)
    _match(B, 80)
    _profile(A, email_frequency="instant")
    _profile(B, email_notifications=False)
    out = Outbox()
    _run(out)
    assert out.sent == []
    _profile(B, email_notifications=True, email_unsubscribed_at=datetime.utcnow())
    _run(out)
    assert out.sent == []


def test_job_text_is_escaped_and_links_stay_on_our_site():
    _match(A, 80, title='<img src=x onerror="steal()">', company="Evil & Co")
    out = Outbox()
    _run(out, users=[A])
    html = out.sent[0].html
    assert "<img src=x" not in html and "&lt;img src=x" in html and "Evil &amp; Co" in html
    assert 'href="https://app.spotapply.ai/dashboard?fresh_job=' in html


def test_nothing_without_a_configured_sender(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "")
    _match(A, 80)
    out = Outbox()
    assert _run(out, users=[A])["configured"] is False and out.sent == []


# ── the per-match path leaves daily users to the digest ─────────────────────

def test_daily_users_new_match_alerts_wait_for_the_digest_but_status_notices_go():
    with get_session() as s:
        s.add(UserNotification(user_id=A, title="⚡ New match found for you", type="fresh_job",
                               message="Backend Engineer @ Acme — found 5m ago, fit 88.",
                               link="/dashboard?fresh_job=1"))
        s.add(UserNotification(user_id=A, title="Your search is paused", type="feed_paused",
                               message="We paused your search.", link="/dashboard"))
        s.commit()
    out = Outbox()
    stats = ne.dispatch_pending(now=datetime.utcnow(), send=out,
                                lookup=lambda uid: "login@example.com",
                                reserve=lambda: True, only_users=[A])
    assert stats["to_digest"] == 1 and len(out.sent) == 1
    assert out.sent[0].subject == "Your SpotApply job search is paused"
    with get_session() as s:
        states = {n.type: n.email_state for n in s.exec(
            select(UserNotification).where(UserNotification.user_id == A)).all()}
    assert states == {"fresh_job": "digest", "feed_paused": "sent"}


# ── settings ────────────────────────────────────────────────────────────────

def test_settings_choose_frequency_hour_and_time_zone(monkeypatch):
    monkeypatch.setattr(server, "_require_user", lambda request: A)
    c = TestClient(server.app)
    d = c.get("/api/email-notifications").json()
    assert d["enabled"] is True and d["frequency"] == "daily" and d["digest_hour"] == 9
    assert d["timezone"] == "America/New_York"
    r = c.put("/api/email-notifications", json={"frequency": "daily", "digest_hour": 12,
                                                  "timezone": "Asia/Kolkata"})
    assert r.status_code == 200 and r.json()["digest_hour"] == 12
    p = _get_profile(A)
    assert (p.email_frequency, p.email_digest_hour, p.email_timezone) == ("daily", 12, "Asia/Kolkata")
    assert c.put("/api/email-notifications", json={"frequency": "instant"}).json()["frequency"] == "instant"
    for bad in ({"frequency": "hourly"}, {"digest_hour": 24}, {"digest_hour": "9"},
                {"digest_hour": True}, {}, {"timezone": "Nowhere/Never"}):
        assert c.put("/api/email-notifications", json=bad).status_code == 400, bad
    assert _get_profile(A).email_timezone == "Asia/Kolkata", "an unknown zone is never stored"


def test_the_setup_saves_the_browser_time_zone_through_the_profile(monkeypatch):
    from app.api.server import ProfileUpdate
    assert ProfileUpdate(email_timezone="Europe/Berlin").email_timezone == "Europe/Berlin"
    assert ProfileUpdate(email_timezone="Not/AZone").email_timezone is None


def test_dashboard_offers_daily_summary_times_instant_and_off():
    for v in ('value="daily:9"', 'value="daily:12"', 'value="instant"', 'value="off"'):
        assert v in DASH, v
    assert 'onchange="setEmailNotifications(this.value)"' in DASH
    assert "resolvedOptions().timeZone" in DASH


def test_the_digest_runs_in_the_email_loop():
    src = Path(server.__file__).read_text()
    loop = src[src.index("async def _email_notifications"):]
    loop = loop[:loop.index("\nasync def ")]
    assert "dispatch_digests" in loop and "dispatch_pending" in loop
