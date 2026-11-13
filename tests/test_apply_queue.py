"""The apply queue (founder decision 2026-11-13; docs/AUTO_APPLY_RESEARCH.md §8).

People at work or in class cannot click through twenty forms, so they approve
a LIST of applications once and the extension applies to them one at a time in
their own Chrome. The rules this file pins:

* the server owns the list and hands out ONE item at a time;
* what may be submitted is still _one_click_decision's verdict and nobody
  else's (login-free allow-list, daily cap, kill switch) — the queue adds a
  list, not a second gate;
* an item is "submitted" only when the APPLICATION is (mark_as_submitted: the
  employer's confirmation page or the user's Yes). The extension can report
  that an item is waiting for the person; it has no way to claim a submission;
* looking at the queue spends nothing, and the runner's own calls are not a
  person using the product;
* anything that needs a human (a CAPTCHA, a sign-in, a question) is parked
  with its reason — never retried, never worked around.

Rows this file writes carry the ``aq-`` prefix and are removed by it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from app.api import server
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (Application, ApplicationStatus, ApplyQueueItem, Job, JobSource,
                           UserNotification, UserUsage)

_P = "aq-"
_ROOT = Path(__file__).resolve().parents[1]


def _wipe():
    with get_session() as s:
        job_ids = [j for j in s.exec(select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if job_ids:
            app_ids = [a for a in s.exec(select(Application.id).where(Application.job_id.in_(job_ids))).all()]
            if app_ids:
                s.exec(delete(ApplyQueueItem).where(ApplyQueueItem.application_id.in_(app_ids)))
                s.exec(delete(Application).where(Application.id.in_(app_ids)))
            s.exec(delete(Job).where(Job.id.in_(job_ids)))
        s.exec(delete(ApplyQueueItem).where(ApplyQueueItem.host.like(f"{_P}%")))
        s.exec(delete(UserNotification).where(UserNotification.title.like("Apply queue %")))
        s.exec(delete(UserUsage).where(UserUsage.user_id.like(f"{_P}%")))
        s.commit()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    _wipe()
    # No paid work in a test: the fill pack and the queue both start a tailor.
    monkeypatch.setattr(server, "_tailor_and_settle", lambda *a, **k: True)
    yield
    _wipe()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(server.app)


def _seed(tag: str, url: str, *, status=ApplicationStatus.SHORTLISTED, closed=False,
          score=80.0, flags="") -> int:
    with get_session() as s:
        job = Job(user_id=None, source=JobSource.GREENHOUSE, external_id=f"{_P}{tag}",
                  company=f"AQ {tag}", title="Backend Engineer", url=url, description="A role.",
                  is_closed=closed, rerank_score=score, ghost_flags=flags)
        s.add(job)
        s.commit()
        s.refresh(job)
        row = Application(job_id=job.id, status=status, apply_track="manual")
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _one_click(client, on: bool):
    assert client.put("/api/profile", json={"one_click_submit": on}).status_code == 200


def _item(app_id: int) -> ApplyQueueItem:
    with get_session() as s:
        return s.exec(select(ApplyQueueItem).where(ApplyQueueItem.application_id == app_id)
                      .order_by(ApplyQueueItem.id.desc())).first()


GH = "https://boards.greenhouse.io/aqco/jobs/{}"


# ── what may join the queue ──────────────────────────────────────────────────

def test_only_jobs_the_extension_can_fill_join_the_queue(client):
    _one_click(client, True)
    ok = _seed("ok", GH.format(1))
    workday = _seed("wd", "https://aqco.wd5.myworkdayjobs.com/en-US/careers/job/x")
    board = _seed("agg", "https://www.adzuna.com/details/123")
    closed = _seed("closed", GH.format(2), closed=True)
    done = _seed("done", GH.format(3), status=ApplicationStatus.SUBMITTED)
    r = client.post("/api/apply-queue", json={"application_ids": [ok, workday, board, closed, done, 99999999]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["accepted"]) == {ok, workday}
    why = {x["application_id"]: x["why"] for x in body["rejected"]}
    assert why == {board: "aggregator_link", closed: "closed", done: "not_open", 99999999: "not_found"}
    assert all(x["why_text"] for x in body["rejected"]), "every refusal says why, in words"
    assert _item(ok).auto_submit is True, "a login-free board may be submitted"
    assert _item(workday).auto_submit is False, "an account site is only ever filled"
    again = client.post("/api/apply-queue", json={"application_ids": [ok]}).json()
    assert again["accepted"] == [] and again["rejected"][0]["why"] == "already"
    _one_click(client, False)


def test_with_one_click_off_the_queue_only_fills(client):
    _one_click(client, False)
    a = _seed("off", GH.format(4))
    body = client.post("/api/apply-queue", json={"application_ids": [a]}).json()
    assert body["accepted"] == [a] and body["one_click_submit"] is False
    assert _item(a).auto_submit is False
    out = client.post("/api/apply-queue/next").json()
    assert out["pack"]["auto_submit"] is False and out["pack"]["auto_submit_reason"] == "setting_off"
    assert out["pack"]["auto_submit_background"] is False
    assert out["item"]["mode"] == "fill_only"


def test_jobs_it_may_submit_run_before_the_ones_it_only_fills(client):
    _one_click(client, True)
    wd = _seed("order-wd", "https://aqco.wd5.myworkdayjobs.com/en-US/careers/job/y")
    gh = _seed("order-gh", GH.format(5))
    client.post("/api/apply-queue", json={"application_ids": [wd, gh]})
    first = client.post("/api/apply-queue/next").json()
    assert first["item"]["application_id"] == gh and first["item"]["mode"] == "submit"
    _one_click(client, False)


def test_the_queue_has_a_size_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "apply_queue_max_items", 2)
    ids = [_seed(f"full{i}", GH.format(10 + i)) for i in range(3)]
    body = client.post("/api/apply-queue", json={"application_ids": ids}).json()
    assert len(body["accepted"]) == 2
    assert body["rejected"] == [{"application_id": ids[2], "why": "queue_full",
                                 "why_text": server._AQ_REASON_TEXT["queue_full"]}]


def test_an_empty_or_disabled_approval_is_refused(client, monkeypatch):
    assert client.post("/api/apply-queue", json={"application_ids": []}).status_code == 400
    monkeypatch.setattr(settings, "apply_queue_enabled", False)
    a = _seed("ks", GH.format(20))
    assert client.post("/api/apply-queue", json={"application_ids": [a]}).status_code == 409
    assert client.post("/api/apply-queue/next").json()["done"] is True


# ── one at a time, and only a confirmation is a submission ───────────────────

def test_one_item_at_a_time_and_only_a_confirmed_application_is_submitted(client):
    _one_click(client, True)
    a, b = _seed("run-a", GH.format(30)), _seed("run-b", GH.format(31))
    client.post("/api/apply-queue", json={"application_ids": [a, b]})

    first = client.post("/api/apply-queue/next").json()
    assert first["done"] is False and first["item"]["application_id"] == a
    pack = first["pack"]
    assert pack["app_id"] == a and pack["queue_item_id"] == first["item"]["id"]
    assert pack["auto_submit"] is True and pack["auto_submit_reason"] == "ok"
    assert pack["auto_submit_background"] is True
    assert first["gap_seconds"] >= 5 and first["timeout_seconds"] >= 30 and first["max_parked_tabs"] >= 1
    assert first["remaining"] == 1

    # A second browser asking now gets nothing: one application at a time.
    busy = client.post("/api/apply-queue/next").json()
    assert busy == dict(busy, done=False, wait=True) and "pack" not in busy

    # The extension can say "waiting for the person". It cannot say "submitted".
    r = client.post(f"/api/apply-queue/items/{first['item']['id']}/outcome", json={"reason": "submitted"})
    assert r.json()["status"] == "needs_you"
    assert _item(a).status == "needs_you"
    with get_session() as s:
        assert s.get(Application, a).status == ApplicationStatus.SHORTLISTED

    second = client.post("/api/apply-queue/next").json()
    assert second["item"]["application_id"] == b
    # The employer confirmed (the extension's FORM_SUBMITTED → this route).
    assert client.post(f"/application/{b}/submit").json()["success"] is True
    assert _item(b).status == "submitted" and _item(b).reason == "confirmed"
    # A late "it is parked" report cannot undo that.
    late = client.post(f"/api/apply-queue/items/{second['item']['id']}/outcome", json={"reason": "timeout"})
    assert late.json()["status"] == "submitted"

    end = client.post("/api/apply-queue/next").json()
    assert end["done"] is True and end["reason"] == "empty"
    assert end["summary"] == {"submitted": 1, "needs_you": 1}
    with get_session() as s:
        notes = s.exec(select(UserNotification).where(UserNotification.title == "Apply queue finished")).all()
        assert len(notes) == 1 and "1 submitted, 1 needs you" in notes[0].message
        assert notes[0].type == "needs_you" and notes[0].link == "/dashboard?queue=1"
    # Asking again tells nobody twice.
    assert client.post("/api/apply-queue/next").json()["summary"] == {"submitted": 0, "needs_you": 0}
    with get_session() as s:
        assert len(s.exec(select(UserNotification).where(
            UserNotification.title == "Apply queue finished")).all()) == 1
    # The person finishes the parked one by hand: its item follows the application.
    assert client.post(f"/application/{a}/submit").json()["success"] is True
    assert _item(a).status == "submitted"
    _one_click(client, False)


def test_a_parked_reason_is_stored_and_shown_in_words(client):
    a = _seed("park", GH.format(40))
    client.post("/api/apply-queue", json={"application_ids": [a]})
    item_id = client.post("/api/apply-queue/next").json()["item"]["id"]
    assert client.post(f"/api/apply-queue/items/{item_id}/outcome", json={"reason": "captcha"}).status_code == 200
    row = next(i for i in client.get("/api/apply-queue").json()["items"] if i["id"] == item_id)
    assert row["status"] == "needs_you" and row["reason"] == "captcha"
    assert "CAPTCHA" in row["reason_text"] and "Solve it" in row["reason_text"]
    # An unreadable reason is not stored verbatim.
    b = _seed("park2", GH.format(41))
    client.post("/api/apply-queue", json={"application_ids": [b]})
    item_b = client.post("/api/apply-queue/next").json()["item"]["id"]
    client.post(f"/api/apply-queue/items/{item_b}/outcome", json={"reason": "<script>alert(1)</script>"})
    assert _item(b).reason == "needs_you"


def test_the_daily_limit_ends_the_run_and_keeps_the_approval(client, monkeypatch):
    a = _seed("cap", GH.format(50))
    client.post("/api/apply-queue", json={"application_ids": [a]})
    monkeypatch.setattr(server, "get_fill_pack", lambda app_id, request, submit=False: {
        "app_id": app_id, "auto_submit": False, "auto_submit_reason": "daily_cap"})
    out = client.post("/api/apply-queue/next").json()
    assert out["done"] is True and out["reason"] == "daily_cap" and "pack" not in out
    row = _item(a)
    assert row.status == "queued" and row.started_at is None, "the approval stands for tomorrow"


def test_a_pack_that_cannot_be_built_never_blocks_the_queue(client, monkeypatch):
    from fastapi import HTTPException
    a, b = _seed("gone", GH.format(51)), _seed("boom", GH.format(52))
    client.post("/api/apply-queue", json={"application_ids": [a, b]})

    def _gone(app_id, request, submit=False):
        raise HTTPException(status_code=404, detail="Application not found")
    monkeypatch.setattr(server, "get_fill_pack", _gone)
    assert client.post("/api/apply-queue/next").json()["wait"] is True
    assert (_item(a).status, _item(a).reason) == ("cancelled", "left_board")

    def _boom(app_id, request, submit=False):
        raise RuntimeError("resume storage is down")
    monkeypatch.setattr(server, "get_fill_pack", _boom)
    assert client.post("/api/apply-queue/next").json()["wait"] is True
    assert (_item(b).status, _item(b).reason) == ("needs_you", "error")


# ── the list stays true to what happened ─────────────────────────────────────

def test_an_approval_nobody_ran_expires(client):
    a = _seed("old", GH.format(60))
    client.post("/api/apply-queue", json={"application_ids": [a]})
    with get_session() as s:
        row = s.get(ApplyQueueItem, _item(a).id)
        row.approved_at = datetime.utcnow() - timedelta(hours=settings.apply_queue_approval_hours + 1)
        s.add(row)
        s.commit()
    assert client.post("/api/apply-queue/next").json()["done"] is True
    assert _item(a).status == "expired"


def test_a_browser_that_never_answers_stops_blocking_the_queue(client):
    a, b = _seed("asleep", GH.format(61)), _seed("after", GH.format(62))
    client.post("/api/apply-queue", json={"application_ids": [a, b]})
    assert client.post("/api/apply-queue/next").json()["item"]["application_id"] == a
    with get_session() as s:
        row = s.get(ApplyQueueItem, _item(a).id)
        row.started_at = datetime.utcnow() - timedelta(hours=1)
        s.add(row)
        s.commit()
    nxt = client.post("/api/apply-queue/next").json()
    assert nxt["item"]["application_id"] == b
    assert (_item(a).status, _item(a).reason) == ("needs_you", "no_answer")


def test_a_job_dismissed_before_its_turn_leaves_the_queue(client):
    a = _seed("skip", GH.format(63))
    client.post("/api/apply-queue", json={"application_ids": [a]})
    assert client.post(f"/application/{a}/skip").status_code == 200
    assert client.post("/api/apply-queue/next").json()["done"] is True
    assert (_item(a).status, _item(a).reason) == ("cancelled", "left_board")


def test_removing_and_clearing(client):
    a, b, c = (_seed(f"rm{i}", GH.format(70 + i)) for i in range(3))
    client.post("/api/apply-queue", json={"application_ids": [a, b, c]})
    running = client.post("/api/apply-queue/next").json()["item"]["id"]
    assert client.delete(f"/api/apply-queue/items/{running}").status_code == 409, "not mid-fill"
    state = client.delete(f"/api/apply-queue/items/{_item(b).id}").json()
    assert state["counts"]["queued"] == 1
    assert client.post("/api/apply-queue/clear").json()["counts"]["queued"] == 0
    assert _item(c).status == "cancelled" and _item(a).status == "running"


def test_another_users_item_reads_as_not_there(client):
    a = _seed("theirs", GH.format(80))
    with get_session() as s:
        row = ApplyQueueItem(user_id="someone-else", application_id=a, status="running", host=f"{_P}x")
        s.add(row)
        s.commit()
        s.refresh(row)
        item_id = row.id
    assert client.post(f"/api/apply-queue/items/{item_id}/outcome", json={"reason": "captcha"}).status_code == 404
    assert client.delete(f"/api/apply-queue/items/{item_id}").status_code == 404
    assert all(i["id"] != item_id for i in client.get("/api/apply-queue").json()["items"])
    with get_session() as s:
        assert s.get(ApplyQueueItem, item_id).status == "running"


def test_the_queue_never_follows_an_item_to_an_application_that_is_not_the_callers():
    """Defence in depth (review 2026-11-13). Only apply_queue_approve writes an
    item, and it checks ownership — but every query that follows an item to its
    application is owner-scoped as well, so an item that somehow named another
    tenant's application would be cancelled, never shown or run."""
    import inspect
    sweep = inspect.getsource(server._aq_sweep)
    assert 'if uid != "local":' in sweep and "Application.user_id == uid" in sweep
    state = inspect.getsource(server._aq_state)
    assert state.count("Application.user_id == uid") == 2, "the items' join AND the candidates"
    assert "application.user_id != uid" in inspect.getsource(server._aq_tailor_ahead)
    assert "application.user_id == uid" in inspect.getsource(server.apply_queue_outcome)


# ── looking is free; the runner is not a person ──────────────────────────────

def test_the_review_list_says_what_each_job_would_do_and_spends_nothing(client, monkeypatch):
    _one_click(client, True)
    gh = _seed("cand-gh", GH.format(90), score=91)
    wd = _seed("cand-wd", "https://aqco.wd5.myworkdayjobs.com/en-US/careers/job/z", score=88)
    agg = _seed("cand-agg", "https://www.adzuna.com/details/9", score=85)
    cands = {c["application_id"]: c for c in client.get("/api/apply-queue?candidates=1").json()["candidates"]}
    assert cands[gh]["mode"] == "submit" and cands[gh]["why_text"] == ""
    assert cands[wd]["mode"] == "fill_only"
    assert cands[wd]["why_text"] == "Not on the automatic-submit list: SpotApply fills it, you click Submit."
    assert not cands[wd]["why_text"].startswith("Filled"), "nothing has been filled on the review list yet"
    assert cands[agg]["mode"] == "excluded" and "job board" in cands[agg]["why_text"]
    assert cands[gh]["score"] == 91 and cands[gh]["host"] == "boards.greenhouse.io"
    # A job already in the queue is not offered twice.
    client.post("/api/apply-queue", json={"application_ids": [gh]})
    assert gh not in {c["application_id"] for c in client.get("/api/apply-queue?candidates=1").json()["candidates"]}
    assert "candidates" not in client.get("/api/apply-queue").json(), "the poll stays light"
    _one_click(client, False)


def test_asking_what_would_happen_reserves_no_submission():
    uid = f"{_P}user"
    prof, job = SimpleNamespace(one_click_submit=True), SimpleNamespace(ghost_flags="")
    for _ in range(3):
        d = server._one_click_decision(uid, prof, GH.format(1), job, True, reserve=False)
        assert d["auto_submit"] is True
    with get_session() as s:
        assert s.exec(select(UserUsage).where(UserUsage.user_id == uid)).first() is None
    assert server._one_click_decision(uid, prof, GH.format(1), job, True)["auto_submit"] is True
    with get_session() as s:
        assert s.exec(select(UserUsage).where(UserUsage.user_id == uid)).first().one_click_submit_count == 1


class _Req:
    def __init__(self, method, path):
        self.method = method
        self.url = SimpleNamespace(path=path)


def test_the_runners_own_calls_are_not_a_person_using_the_product():
    assert server._activity_kind(_Req("POST", "/api/apply-queue")) == "meaningful", "the approval is the action"
    assert server._activity_kind(_Req("POST", "/api/apply-queue/next")) is None
    assert server._activity_kind(_Req("POST", "/api/apply-queue/items/7/outcome")) is None
    assert server._activity_kind(_Req("POST", "/api/apply-queue/summary")) is None
    assert server._activity_kind(_Req("DELETE", "/api/apply-queue/items/7")) == "meaningful"
    assert server._activity_kind(_Req("GET", "/api/apply-queue")) is None


def test_a_queue_item_rings_the_bell_once_in_the_summary(client):
    a = _seed("bell", GH.format(95))
    client.post("/api/apply-queue", json={"application_ids": [a]})
    item_id = client.post("/api/apply-queue/next").json()["item"]["id"]
    body = {"event": "captcha_needed", "app_id": a, "host": "boards.greenhouse.io", "meta": {}}
    assert client.post("/api/extension/telemetry", json=body).status_code == 200
    with get_session() as s:
        assert s.exec(select(UserNotification).where(UserNotification.type == "needs_you",
                                                     UserNotification.link == f"/dashboard?app={a}")).first() is None
    client.post(f"/api/apply-queue/items/{item_id}/outcome", json={"reason": "captcha"})
    assert client.post("/api/apply-queue/next").json()["summary"] == {"submitted": 0, "needs_you": 1}


def test_a_run_that_stands_still_tells_the_person_tabs_are_waiting(client):
    """Live check 2026-11-13: four forms parked, the worker paused at its
    parked-tab limit and never asked /next again — so the person who had walked
    away got no notification at all."""
    a = _seed("pause", GH.format(98))
    client.post("/api/apply-queue", json={"application_ids": [a]})
    item_id = client.post("/api/apply-queue/next").json()["item"]["id"]
    client.post(f"/api/apply-queue/items/{item_id}/outcome", json={"reason": "resume_not_attached"})
    row = next(i for i in client.get("/api/apply-queue").json()["items"] if i["id"] == item_id)
    assert "resume could not be attached" in row["reason_text"]
    out = client.post("/api/apply-queue/summary", json={"reason": "parked_limit"}).json()
    assert out["summary"] == {"submitted": 0, "needs_you": 1}
    with get_session() as s:
        notes = s.exec(select(UserNotification).where(UserNotification.title == "Apply queue paused")).all()
        assert len(notes) == 1 and "1 needs you" in notes[0].message
        assert "continues by itself" in notes[0].message and notes[0].type == "needs_you"
    # Said once; a stop with nothing new to report says nothing.
    again = client.post("/api/apply-queue/summary", json={"reason": "stopped"}).json()
    assert again["summary"] == {"submitted": 0, "needs_you": 0}
    with get_session() as s:
        assert len(s.exec(select(UserNotification).where(UserNotification.title.like("Apply queue %"))).all()) == 1


# ── the settings and the wiring ──────────────────────────────────────────────

def test_the_settings_defaults_are_sane():
    from app.config import Settings
    f = Settings.model_fields
    assert f["apply_queue_enabled"].default is True
    assert 1 <= f["apply_queue_max_items"].default <= 50
    assert 5 <= f["apply_queue_gap_seconds"].default <= 120, "one tab at a time, with a pause"
    assert 60 <= f["apply_queue_item_timeout_seconds"].default <= 900
    assert 1 <= f["apply_queue_max_parked_tabs"].default <= 8, "a broken fill must not open 25 tabs"
    assert 1 <= f["apply_queue_approval_hours"].default <= 48
    # The queue never widens what one click may submit.
    assert f["apply_queue_max_items"].default <= f["one_click_submit_daily_limit"].default


def test_background_submit_is_one_switch(client, monkeypatch):
    _one_click(client, True)
    monkeypatch.setattr(settings, "apply_queue_background_submit", False)
    a = _seed("bg", GH.format(96))
    client.post("/api/apply-queue", json={"application_ids": [a]})
    pack = client.post("/api/apply-queue/next").json()["pack"]
    assert pack["auto_submit"] is True and pack["auto_submit_background"] is False
    _one_click(client, False)


def test_a_single_apply_and_submit_click_never_gets_the_background_flag(client):
    _one_click(client, True)
    a = _seed("single", GH.format(97))
    pack = client.get(f"/api/fill-pack/{a}?submit=1").json()
    assert pack["auto_submit"] is True
    assert "auto_submit_background" not in pack and "queue_item_id" not in pack
    _one_click(client, False)


def test_the_extension_runs_the_queue_and_cannot_claim_a_submission():
    bg = (_ROOT / "extension" / "background.js").read_text()
    cs = (_ROOT / "extension" / "content.js").read_text()
    assert "/api/apply-queue/next" in bg and "QUEUE_START" in bg and "QUEUE_SIGNAL" in bg
    assert "chrome.alarms" in bg, "an MV3 worker sleeps; the queue must be woken, not kept alive"
    # The only thing the runner reports is that an item is parked.
    outcome_calls = [ln for ln in bg.splitlines() if "/outcome" in ln and "apply-queue" in ln]
    assert outcome_calls and all("submitted" not in ln for ln in outcome_calls)
    # The page side: the dashboard bridge, the signal, and the visible-tab rule
    # relaxed ONLY for a pack the server flagged.
    assert "'SPOTAPPLY_QUEUE_START'" in cs and "function queueSignal" in cs
    assert "pack.auto_submit_background" in cs
    manifest = (_ROOT / "extension" / "manifest.json").read_text()
    assert '"alarms"' in manifest


def test_the_dashboard_offers_the_queue_with_a_review_step():
    dash = (_ROOT / "app" / "templates" / "dashboard.html").read_text()
    assert 'id="apply-queue-modal"' in dash and "function openApplyQueue" in dash
    assert "'/api/apply-queue' + (withCandidates ? '?candidates=1' : '')" in dash
    assert "'SPOTAPPLY_QUEUE_' + kind" in dash and "runnerCall('START', authPack())" in dash
    queue_js = dash.split("function renderApplyQueue")[1].split("</script>")[0]
    assert "By approving, you tell SpotApply" in queue_js and "and start" in queue_js
    # The page shows and asks; it never decides what may be submitted.
    assert "greenhouse.io" not in queue_js and "auto_submit" not in queue_js


def test_terms_describe_the_queue():
    terms = (_ROOT / "app" / "templates" / "terms.html").read_text()
    assert "apply queue" in terms.lower()
    assert "never solve or bypass a CAPTCHA" in terms and "store passwords" in terms
    assert "Last updated: November 2026" in terms
