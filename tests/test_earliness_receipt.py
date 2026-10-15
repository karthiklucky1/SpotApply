"""Earliness receipt (2026-10-11, docs/EARLINESS_RECEIPT.md).

Three rules: ONE reference clock per posting (the earlier of a trusted publish
date and when we first saw it — a rule that can only make a receipt LESS
flattering); the receipt states a clock, never odds; an email-imported
application never gets one. Plus: the first employer response is first-write-
wins and 'ghosted' never sets it, and the landing speed line hides under its
sample floor. Rows carry the ``earl-`` prefix and are removed here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import delete

from app.api import server
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource
from app.intelligence import urgency
from app.strategy import earliness

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 11, 12, 0, 0)


def _job(source="greenhouse", posted=None, first_seen=None, discovered=None):
    return SimpleNamespace(source=SimpleNamespace(value=source), posted_at=posted,
                           first_seen=first_seen, discovered_at=discovered or first_seen,
                           is_closed=False, last_seen=first_seen, ghost_score=0.0)


# ── the one reference clock ──────────────────────────────────────────────────

def test_a_trusted_publish_date_earlier_than_first_seen_is_the_clock():
    posted, seen = NOW - timedelta(hours=10), NOW - timedelta(hours=3)
    assert earliness.reference(_job("greenhouse", posted, seen)) == (posted, "posted")


def test_a_publish_date_later_than_first_seen_is_an_edit_date_and_is_ignored():
    """Greenhouse updated_at / future-dated feeds: the rule can only make the
    receipt less flattering, never more."""
    seen, posted = NOW - timedelta(hours=30), NOW - timedelta(hours=1)
    assert earliness.reference(_job("lever", posted, seen)) == (seen, "found")


@pytest.mark.parametrize("source", ["workday", "personio", "indeed", "linkedin", "manual", "serpapi"])
def test_boards_without_a_true_publish_timestamp_use_the_found_clock(source):
    posted, seen = NOW - timedelta(days=9), NOW - timedelta(hours=5)
    assert earliness.reference(_job(source, posted, seen)) == (seen, "found")


def test_no_dates_means_no_receipt():
    assert earliness.reference(_job("greenhouse", None, None)) == (None, "")
    app_row = SimpleNamespace(apply_track="autofill", applied_after_min=None, applied_ref=None)
    assert earliness.stamp(app_row, _job("greenhouse", None, None), NOW) is False
    assert earliness.receipt(app_row) is None


def test_stamp_writes_minutes_and_clock_but_never_for_an_email_import():
    job = _job("ashby", NOW - timedelta(hours=3, minutes=12), NOW - timedelta(hours=2))
    app_row = SimpleNamespace(apply_track="autofill", applied_after_min=None, applied_ref=None)
    assert earliness.stamp(app_row, job, NOW) is True
    assert (app_row.applied_after_min, app_row.applied_ref) == (192, "posted")
    r = earliness.receipt(app_row)
    assert r["label"] == "Applied 3h 12m after posting" and r["early"] is True
    imported = SimpleNamespace(apply_track="email_import", applied_after_min=None, applied_ref=None)
    assert earliness.stamp(imported, job, NOW) is False
    assert earliness.receipt(imported) is None


def test_a_found_clock_says_so_and_a_late_receipt_is_not_early():
    r = earliness.receipt(SimpleNamespace(apply_track="autofill", applied_after_min=3 * 24 * 60 + 60, applied_ref="found"))
    assert r["label"] == "Applied 3d 1h after we found it"
    assert r["early"] is False and "when SpotApply first saw" in r["detail"]
    assert "after posting" not in r["label"]


def test_the_window_is_measured_from_the_reference_clock():
    job = _job("greenhouse", NOW - timedelta(hours=40), NOW - timedelta(hours=1))
    w = earliness.early_window(job, NOW)
    assert w["in_window"] is True and w["hours_left"] == 8 and w["kind"] == "posted"
    old = _job("greenhouse", NOW - timedelta(hours=60), NOW - timedelta(hours=50))
    assert earliness.early_window(old, NOW)["in_window"] is False
    assert settings.earliness_window_hours == 48


# ── the words: a clock, never odds ───────────────────────────────────────────

def test_every_earliness_and_urgency_string_states_a_clock_never_odds():
    strings = [earliness.EVIDENCE_NOTE]
    for minutes, kind in ((5, "posted"), (3000, "found"), (60 * 24 * 9, "posted")):
        r = earliness.receipt(SimpleNamespace(apply_track="autofill", applied_after_min=minutes, applied_ref=kind))
        strings += [r["label"], r["detail"]]
    for job in (_job("greenhouse", NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
                _job("workday", None, NOW - timedelta(hours=2)),
                _job("greenhouse", NOW - timedelta(days=4), NOW - timedelta(days=4)),
                _job("greenhouse", NOW - timedelta(days=40), NOW - timedelta(days=40))):
        a = urgency.assess(job, now=NOW)
        strings += [a.label, a.reason]
    for text in strings:
        assert earliness.copy_is_honest(text), text
    macros = (ROOT / "app" / "templates" / "_board_macros.html").read_text()
    assert "4× reply rate" not in macros and "reply rate" not in macros
    for tmpl in ("_board_macros.html", "_board_pane.html", "dashboard.html", "landing.html"):
        src = (ROOT / "app" / "templates" / tmpl).read_text()
        for bad in ("more likely to hear back", "4× reply", "4x reply", "interview odds", "maximizes your"):
            assert bad not in src, (tmpl, bad)


def test_the_hot_tier_cannot_be_lit_by_an_edit_date():
    """A posting we have held for a week whose Greenhouse `updated_at` moved
    this morning is NOT in the early window."""
    job = _job("greenhouse", NOW - timedelta(hours=2), NOW - timedelta(days=7))
    a = urgency.assess(job, now=NOW)
    assert "Early window" not in a.label and a.tone != "hot"
    fresh = _job("greenhouse", NOW - timedelta(hours=5), NOW - timedelta(hours=4))
    a = urgency.assess(fresh, now=NOW)
    assert a.label.startswith("⚡ Early window") and "h left" in a.label and a.tone == "hot"
    real_now = datetime.utcnow()                     # no `now` = the real clock (production)
    live = _job("greenhouse", real_now - timedelta(hours=5), real_now - timedelta(hours=4))
    assert urgency.assess(live).label.startswith("⚡ Early window")


# ── the routes: stamping and the first response ──────────────────────────────

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(server.app)


@pytest.fixture()
def seeded():
    created = []
    with get_session() as s:
        def mk(source, posted, seen, track="autofill"):
            job = Job(user_id="local", source=source, external_id=f"earl-{source.value}-{len(created)}",
                      company="Earl Co", title="Engineer", url=f"https://x.test/{len(created)}", description="d",
                      posted_at=posted, first_seen=seen, discovered_at=seen)
            s.add(job); s.flush()
            app_row = Application(user_id="local", job_id=job.id, status=ApplicationStatus.SHORTLISTED,
                                  apply_track=track, notes="earl-test")
            s.add(app_row); s.flush()
            created.append((job.id, app_row.id))
            return app_row.id
        now = datetime.utcnow()
        ids = {
            "trusted": mk(JobSource.GREENHOUSE, now - timedelta(hours=6), now - timedelta(hours=2)),
            "edit_date": mk(JobSource.GREENHOUSE, now - timedelta(minutes=30), now - timedelta(days=3)),
            "no_publish_date": mk(JobSource.WORKDAY, now - timedelta(days=20), now - timedelta(hours=4)),
            "imported": mk(JobSource.MANUAL, now - timedelta(hours=1), now - timedelta(hours=1), track="email_import"),
        }
        s.commit()
    yield ids
    with get_session() as s:
        s.exec(delete(Application).where(Application.notes == "earl-test"))
        s.exec(delete(Job).where(Job.external_id.like("earl-%")))
        s.commit()


def _row(app_id):
    with get_session() as s:
        return s.get(Application, app_id)


def test_marking_submitted_stamps_the_receipt_from_the_right_clock(client, seeded):
    r = client.post(f"/application/{seeded['trusted']}/submit")
    assert r.status_code == 200 and r.json()["success"]
    row = _row(seeded["trusted"])
    assert row.applied_ref == "posted" and 350 <= row.applied_after_min <= 370
    client.post(f"/application/{seeded['edit_date']}/submit")
    row = _row(seeded["edit_date"])
    assert row.applied_ref == "found" and row.applied_after_min >= 3 * 24 * 60 - 5
    client.post(f"/application/{seeded['no_publish_date']}/submit")
    row = _row(seeded["no_publish_date"])
    assert row.applied_ref == "found" and 235 <= row.applied_after_min <= 250


def test_an_email_import_never_carries_a_receipt(client, seeded):
    client.post(f"/application/{seeded['imported']}/submit")
    row = _row(seeded["imported"])
    assert row.status == ApplicationStatus.SUBMITTED
    assert row.applied_after_min is None and row.applied_ref is None
    assert earliness.receipt(row) is None


def test_the_first_response_is_first_write_wins_and_ghosted_never_sets_it(client, seeded):
    app_id = seeded["trusted"]
    client.post(f"/application/{app_id}/submit")
    assert client.post(f"/application/{app_id}/outcome", params={"outcome": "ghosted"}).status_code == 200
    assert _row(app_id).first_response_at is None
    assert client.post(f"/application/{app_id}/outcome", params={"outcome": "interviewing"}).status_code == 200
    first = _row(app_id).first_response_at
    assert first is not None
    client.post(f"/application/{app_id}/outcome", params={"outcome": "rejected"})
    assert _row(app_id).first_response_at == first


def test_an_email_signal_sets_the_first_response_only_when_confident(seeded):
    app_id = seeded["no_publish_date"]
    with get_session() as s:
        row = s.get(Application, app_id); row.status = ApplicationStatus.SUBMITTED; s.add(row); s.commit()
    match = SimpleNamespace(id=app_id)
    stats = {"rejections": 0, "interviews": 0, "offers": 0, "assessments": 0, "uncertain": 0}
    weak = SimpleNamespace(kind="rejection", moves_status=False, cues=["wording"])
    server._apply_email_signal(match, weak, {"date": "Sat, 10 Oct 2026 09:15:00 +0000"}, None, None, stats)
    assert _row(app_id).first_response_at is None
    strong = SimpleNamespace(kind="interview", moves_status=True, cues=[])
    server._apply_email_signal(match, strong, {"date": "Sat, 10 Oct 2026 09:15:00 +0000"}, None, None, stats)
    row = _row(app_id)
    assert row.status == ApplicationStatus.INTERVIEWING
    assert row.first_response_at == datetime(2026, 10, 10, 9, 15)


def test_email_dates_are_read_as_utc_and_a_future_header_is_ignored():
    assert server._email_datetime({"date": "Sat, 10 Oct 2026 05:15:00 -0400"}) == datetime(2026, 10, 10, 9, 15)
    assert server._email_datetime({"date": "2026-10-10T09:15:00Z"}) == datetime(2026, 10, 10, 9, 15)
    assert server._email_datetime({"date": "Sat, 10 Oct 2099 09:15:00 +0000"}) is None
    assert server._email_datetime({"date": "nonsense"}) is None and server._email_datetime({}) is None


# ── the surfaces ─────────────────────────────────────────────────────────────

def test_the_board_puts_the_early_window_first_and_the_card_shows_the_receipt():
    macros = (ROOT / "app" / "templates" / "_board_macros.html").read_text()
    assert 'data-early="1"' in macros and "early_window_of(job)" in macros
    assert "earliness_of(app, job)" in macros
    dash = (ROOT / "app" / "templates" / "dashboard.html").read_text()
    fn = dash[dash.index("function sortShortlist"):]
    fn = fn[:fn.index("function _initShortlistSort")]
    assert "dataset.early === '1'" in fn and "early-window-divider" in fn
    pane = (ROOT / "app" / "templates" / "_board_pane.html").read_text()
    assert "early_stats.measured" in pane and "early-month-line" in pane


def test_the_landing_speed_line_is_measured_and_hidden_under_its_floor():
    landing = (ROOT / "app" / "templates" / "landing.html").read_text()
    assert 'id="latency-line" class="hidden"' in landing
    assert "f.fresh_alerts_7d >= 50" in landing and "median_post_to_alert_min" in landing
    assert "Greenhouse, Lever, Ashby" in landing
    for bad in ("more likely", "4× ", "odds"):
        assert bad not in landing[landing.index('id="latency-line"'):landing.index('id="latency-line"') + 900]


def test_the_submitted_pane_renders_the_monthly_line_only_when_measured(client, seeded):
    client.post(f"/application/{seeded['trusted']}/submit")
    r = client.get("/api/board/pane", params={"stage": "submitted"})
    assert r.status_code == 200, r.text
    html = r.json()["html"]
    assert "early-month-line" in html and "Early (inside 48h) on" in html
    assert "after posting" in html                      # the receipt chip on the card
