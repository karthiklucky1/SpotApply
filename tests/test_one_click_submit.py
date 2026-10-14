"""One-click submit (founder decision 2026-10-11; docs/AUTO_APPLY_RESEARCH.md §5).

The dashboard's "Apply & Submit" click is the user's approval of ONE
application. The SERVER decides whether the extension may press the form's
Submit for it (_one_click_decision): the setting must be on, the kill switch
up, the apply URL on the login-free allow-list, the link not an aggregator
redirect, and the day's cap not spent. The extension never carries its own
allow-list or cap — it obeys pack.auto_submit.

Rows this file writes carry the ``ocs-`` prefix and are removed by it.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.api import server
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (Application, ApplicationStatus, Job, JobSource, UserNotification,
                           UserUsage)

_P = "ocs-"
_UID = "ocs-user"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            s.exec(delete(UserUsage).where(UserUsage.user_id == _UID))
            s.exec(delete(UserNotification).where(UserNotification.type == "needs_you",
                                                  UserNotification.message.like(f"{_P}%")))
            s.commit()
    _wipe()
    yield
    _wipe()


def _profile(on: bool):
    return SimpleNamespace(one_click_submit=on)


def _job(flags=""):
    return SimpleNamespace(ghost_flags=flags)


# ── the decision ─────────────────────────────────────────────────────────────

def test_nothing_happens_without_the_click():
    d = server._one_click_decision(_UID, _profile(True), "https://boards.greenhouse.io/acme/jobs/1", _job(), False)
    assert d["auto_submit"] is False and d["auto_submit_reason"] == "not_requested"
    assert "greenhouse.io" in d["auto_submit_hosts"]


def test_the_setting_must_be_on():
    d = server._one_click_decision(_UID, _profile(False), "https://boards.greenhouse.io/acme/jobs/1", _job(), True)
    assert (d["auto_submit"], d["auto_submit_reason"]) == (False, "setting_off")


def test_the_kill_switch_wins(monkeypatch):
    monkeypatch.setattr(settings, "one_click_submit_enabled", False)
    d = server._one_click_decision(_UID, _profile(True), "https://boards.greenhouse.io/acme/jobs/1", _job(), True)
    assert (d["auto_submit"], d["auto_submit_reason"]) == (False, "kill_switch")


@pytest.mark.parametrize("url,allowed", [
    ("https://boards.greenhouse.io/acme/jobs/1", True),
    ("https://job-boards.greenhouse.io/acme/jobs/1", True),
    ("https://jobs.lever.co/acme/abc", True),
    ("https://jobs.ashbyhq.com/acme/xyz", True),
    ("https://jobs.smartrecruiters.com/Acme/123", True),
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x", False),     # account wall
    ("https://www.linkedin.com/jobs/view/1", False),                      # never
    ("https://notgreenhouse.io/x", False),                                # a suffix is not a substring
    ("https://greenhouse.io.evil.com/x", False),
    ("", False),
])
def test_only_allow_listed_login_free_boards(url, allowed):
    d = server._one_click_decision(_UID, _profile(True), url, _job(), True)
    assert d["auto_submit"] is allowed, d
    if not allowed:
        assert d["auto_submit_reason"] == "host_not_allowed"


def test_an_aggregator_redirect_is_never_submitted():
    d = server._one_click_decision(_UID, _profile(True), "https://boards.greenhouse.io/acme/jobs/1",
                                   _job('["aggregator_redirect"]'), True)
    assert (d["auto_submit"], d["auto_submit_reason"]) == (False, "aggregator_link")


def test_each_approved_click_spends_one_of_the_days_allowance(monkeypatch):
    monkeypatch.setattr(settings, "one_click_submit_daily_limit", 2)
    url = "https://jobs.lever.co/acme/abc"
    assert server._one_click_decision(_UID, _profile(True), url, _job(), True)["auto_submit"] is True
    assert server._one_click_decision(_UID, _profile(True), url, _job(), True)["auto_submit"] is True
    d = server._one_click_decision(_UID, _profile(True), url, _job(), True)
    assert (d["auto_submit"], d["auto_submit_reason"]) == (False, "daily_cap")
    with get_session() as s:
        row = s.exec(select(UserUsage).where(UserUsage.user_id == _UID,
                                             UserUsage.usage_date == date.today())).first()
        assert row is not None and row.one_click_submit_count == 2
    # A refused click costs nothing.
    d = server._one_click_decision(_UID, _profile(False), url, _job(), True)
    assert d["auto_submit_reason"] == "setting_off"
    with get_session() as s:
        row = s.exec(select(UserUsage).where(UserUsage.user_id == _UID,
                                             UserUsage.usage_date == date.today())).first()
        assert row.one_click_submit_count == 2


def test_a_revoked_board_leaves_one_click_before_the_allow_list_is_read(monkeypatch):
    """A board that objects is removed the same day by env, no deploy: the
    blocklist is checked FIRST, so an allow-listed suffix cannot re-admit it."""
    monkeypatch.setattr(settings, "one_click_submit_host_blocklist", "boards.greenhouse.io, lever.co")
    d = server._one_click_decision(_UID, _profile(True), "https://boards.greenhouse.io/acme/jobs/1", _job(), True)
    assert (d["auto_submit"], d["auto_submit_reason"]) == (False, "host_blocked")
    d = server._one_click_decision(_UID, _profile(True), "https://jobs.lever.co/acme/x", _job(), True)
    assert (d["auto_submit"], d["auto_submit_reason"]) == (False, "host_blocked")
    # The rest of the allow-list is untouched.
    d = server._one_click_decision(_UID, _profile(True), "https://job-boards.greenhouse.io/acme/jobs/1", _job(), True)
    assert d["auto_submit"] is True
    with get_session() as s:
        s.exec(delete(UserUsage).where(UserUsage.user_id == _UID))
        s.commit()


def test_host_matching_is_by_suffix_never_substring():
    hosts = ["greenhouse.io"]
    assert server._host_allowed("https://boards.greenhouse.io/a", hosts)
    assert server._host_allowed("https://GREENHOUSE.IO/a", hosts)
    assert not server._host_allowed("https://mygreenhouse.io/a", hosts)
    assert not server._host_allowed("https://greenhouse.io.example.com/a", hosts)
    assert not server._host_allowed("not a url", hosts)


# ── the route and the profile ────────────────────────────────────────────────

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(server.app)


@pytest.fixture()
def seeded():
    with get_session() as s:
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id=_P + "job",
                  company="OCS Co", title="Backend Engineer",
                  url="https://boards.greenhouse.io/ocsco/jobs/1", description="A role.")
        s.add(job)
        s.commit()
        s.refresh(job)
        app_row = Application(job_id=job.id, status=ApplicationStatus.SHORTLISTED, apply_track="manual")
        s.add(app_row)
        s.commit()
        s.refresh(app_row)
        ids = (job.id, app_row.id)
    yield ids
    with get_session() as s:
        s.exec(delete(Application).where(Application.job_id == ids[0]))
        s.exec(delete(Job).where(Job.id == ids[0]))
        s.commit()


def _set_profile_flag(client, on: bool):
    r = client.put("/api/profile", json={"one_click_submit": on})
    assert r.status_code == 200, r.text


def test_the_profile_setting_round_trips(client):
    _set_profile_flag(client, True)
    assert client.get("/api/profile").json()["one_click_submit"] is True
    _set_profile_flag(client, False)
    assert client.get("/api/profile").json()["one_click_submit"] is False


def test_the_fill_pack_carries_the_servers_verdict(client, seeded, monkeypatch):
    _, app_id = seeded
    monkeypatch.setattr(server, "_tailor_and_settle", lambda *a, **k: True)   # no paid work in a test
    _set_profile_flag(client, False)
    pack = client.get(f"/api/fill-pack/{app_id}").json()
    assert pack["auto_submit"] is False and pack["auto_submit_reason"] == "not_requested"
    assert pack["auto_submit_hosts"] == server._one_click_hosts()
    pack = client.get(f"/api/fill-pack/{app_id}?submit=1").json()
    assert pack["auto_submit"] is False and pack["auto_submit_reason"] == "setting_off"
    _set_profile_flag(client, True)
    pack = client.get(f"/api/fill-pack/{app_id}?submit=1").json()
    assert pack["auto_submit"] is True and pack["auto_submit_reason"] == "ok", pack["auto_submit_reason"]
    assert pack["one_click_submit"] is True
    _set_profile_flag(client, False)


def test_the_board_offers_apply_and_submit_only_when_the_setting_is_on():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "templates" / "_board_macros.html").read_text()
    assert src.count("{% if one_click_submit %}data-submit=\"1\"{% endif %}") == 4
    assert "'Apply & Submit' if one_click_submit else 'Auto-Fill'" in src
    dash = (Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html").read_text()
    assert "btn.dataset.submit === '1'" in dash and "?submit=1" in dash
    assert 'name="one_click_submit"' in dash
    assert "showCustomConfirm({" in dash.split("async function fillWithExtension")[1][:1500]


# ── the dashboard learns when a human is needed ──────────────────────────────

def test_a_handoff_event_raises_one_notification(client, seeded):
    _, app_id = seeded
    body = {"event": "captcha_needed", "app_id": app_id, "host": "boards.greenhouse.io", "meta": {}}
    assert client.post("/api/extension/telemetry", json=body).status_code == 200
    assert client.post("/api/extension/telemetry", json=body).status_code == 200   # same minute: no second bell
    with get_session() as s:
        notes = s.exec(select(UserNotification).where(UserNotification.type == "needs_you",
                                                      UserNotification.link == f"/dashboard?app={app_id}")).all()
        assert len(notes) == 1
        assert notes[0].message.startswith("OCS Co needs you: solve the CAPTCHA")
        s.exec(delete(UserNotification).where(UserNotification.id == notes[0].id))
        s.commit()


def test_a_blocked_submit_says_why(client, seeded):
    _, app_id = seeded
    body = {"event": "auto_submit_blocked", "app_id": app_id, "host": "boards.greenhouse.io",
            "meta": {"reason": "fields_need_you"}}
    assert client.post("/api/extension/telemetry", json=body).status_code == 200
    with get_session() as s:
        note = s.exec(select(UserNotification).where(UserNotification.type == "needs_you",
                                                     UserNotification.link == f"/dashboard?app={app_id}")).first()
        assert note is not None and "some fields need your answer" in note.message
        s.exec(delete(UserNotification).where(UserNotification.id == note.id))
        s.commit()


def test_ordinary_telemetry_raises_no_notification(client, seeded):
    _, app_id = seeded
    body = {"event": "resume_attached", "app_id": app_id, "host": "boards.greenhouse.io", "meta": {"source": "tailored"}}
    assert client.post("/api/extension/telemetry", json=body).status_code == 200
    with get_session() as s:
        assert s.exec(select(UserNotification).where(UserNotification.type == "needs_you",
                                                     UserNotification.link == f"/dashboard?app={app_id}")).first() is None


# ── the words ────────────────────────────────────────────────────────────────

def test_terms_describe_one_click_submit_and_its_limits():
    from pathlib import Path
    terms = (Path(__file__).resolve().parents[1] / "app" / "templates" / "terms.html").read_text()
    assert "One-click submit" in terms and "off by default" in terms
    assert "never solve or bypass a CAPTCHA" in terms and "store passwords" in terms
    assert "Last updated: October 2026" in terms


def test_the_settings_defaults_are_sane():
    assert settings.one_click_submit_enabled is True
    assert 1 <= settings.one_click_submit_daily_limit <= 100
    hosts = server._one_click_hosts()
    assert "greenhouse.io" in hosts and "lever.co" in hosts and "ashbyhq.com" in hosts
    for never in ("myworkdayjobs.com", "linkedin.com", "indeed.com", "icims.com"):
        assert never not in hosts
    assert server._one_click_blocklist() == []          # nothing revoked by default
