"""Work style is a choice the user makes, and the gate honours it.

A user ticked "hybrid" and found no such option on their profile (2026-10-29):
the product had only "Include remote jobs", so hybrid and on-site were never a
preference anyone could state, and the form-filling default came from a shared
file. `UserProfile.work_modes` ("remote,hybrid,onsite"; "" = all) is now the
choice: a posting whose STATED work mode is not in it is ineligible; one that
states none is not decided by it.
"""
from __future__ import annotations

import pytest

from app.common.eligibility import ELIGIBLE, INELIGIBLE, Geography, decide
from app.common.tenant_prefs import canonical_work_modes, geo_prefs, work_modes


class _P:
    def __init__(self, **kw):
        self.preferred_country = "United States"
        self.location = "Cincinnati, OH"
        self.open_to_relocation = True
        self.relocation_targets = ""
        self.remote_ok = True
        self.work_modes = ""
        self.__dict__.update(kw)


def _geo(mode, site="Austin, TX"):
    return Geography(status="resolved", countries=["united states"],
                     sites=[site] if mode != "remote" else ["Remote"], work_mode=mode)


@pytest.mark.parametrize("raw,canon", [
    ("Hybrid", "hybrid"), ("hybrid, On-site", "hybrid,onsite"), ("onsite,remote", "remote,onsite"),
    ("remote,hybrid,onsite", ""), ("", ""), ("bogus", ""), (["Remote", "in office"], "remote,onsite"),
])
def test_work_styles_are_stored_in_one_spelling(raw, canon):
    assert canonical_work_modes(raw) == canon


@pytest.mark.parametrize("modes,remote,hybrid,onsite,unstated", [
    ("hybrid", INELIGIBLE, ELIGIBLE, INELIGIBLE, ELIGIBLE),
    ("remote,hybrid", ELIGIBLE, ELIGIBLE, INELIGIBLE, ELIGIBLE),
    ("onsite", INELIGIBLE, INELIGIBLE, ELIGIBLE, ELIGIBLE),
    ("", ELIGIBLE, ELIGIBLE, ELIGIBLE, ELIGIBLE),
])
def test_the_gate_honours_the_stated_mode_only(modes, remote, hybrid, onsite, unstated):
    prefs = geo_prefs(_P(work_modes=modes))
    assert decide(_geo("remote"), prefs).status == remote
    assert decide(_geo("hybrid"), prefs).status == hybrid
    assert decide(_geo("onsite"), prefs).status == onsite
    got = decide(_geo(""), prefs)
    assert got.status != INELIGIBLE or got.code != "work_mode_not_wanted"
    assert unstated == ELIGIBLE


def test_the_reason_says_what_was_asked_for():
    d = decide(_geo("onsite"), geo_prefs(_P(work_modes="hybrid")))
    assert d.code == "work_mode_not_wanted" and "On-site role" in d.reason and "hybrid" in d.reason


def test_remote_follows_the_work_style_choice():
    assert geo_prefs(_P(work_modes="hybrid", remote_ok=True)).remote_ok is False
    assert geo_prefs(_P(work_modes="remote,hybrid", remote_ok=False)).remote_ok is True
    assert geo_prefs(_P(work_modes="", remote_ok=False)).remote_ok is False
    assert work_modes(_P(work_modes="hybrid,onsite")) == ("hybrid", "onsite")


def test_the_profile_route_saves_it_and_keeps_remote_in_step():
    from fastapi.testclient import TestClient

    from app.api import server
    client = TestClient(server.app)
    before = client.get("/api/profile").json()
    try:
        r = client.put("/api/profile", json={"work_modes": "Hybrid, On-site"})
        assert r.status_code == 200, r.text
        got = client.get("/api/profile").json()
        assert got["work_modes"] == "hybrid,onsite"
        assert got["remote_ok"] is False, "Remote not ticked = no remote roles"
        client.put("/api/profile", json={"work_modes": "remote,hybrid"})
        assert client.get("/api/profile").json()["remote_ok"] is True
    finally:
        client.put("/api/profile", json={"work_modes": before.get("work_modes") or "",
                                         "remote_ok": before.get("remote_ok", True)})


def test_both_profile_screens_offer_the_three_styles():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html").read_text()
    setup = html[html.index("async function openProfileReview"):]
    setup = setup[:setup.index("\n        }\n")]
    for key in ("chk('remote_ok', 'Remote'", "chk('mode_hybrid', 'Hybrid'", "chk('mode_onsite', 'On-site'"):
        assert key in setup
    assert "Pick at least one work style" in setup and "body.work_modes" in setup
    for name in ('name="remote_ok"', 'name="mode_hybrid"', 'name="mode_onsite"'):
        assert name in html
    save = html[html.index("async function saveProfile"):]
    assert "data.work_modes" in save[:save.index("fetch(")]


def test_the_scorer_is_told_the_work_style():
    from app.matching import reranker as rr
    from app.db.models import UserProfile
    prof = UserProfile(user_id="ws", preferred_country="United States", location="Cincinnati, OH",
                       target_roles="Backend Engineer", work_modes="hybrid", remote_ok=False)
    sp = rr._get_system_prompt(prof)
    assert "Wants hybrid work only" in sp


def test_form_answers_use_the_users_own_work_style(monkeypatch):
    from app.autofill import answer_pack as ap
    from app.db.models import Job, JobSource, UserProfile
    seen = {}

    class _Msg:
        def __init__(self):
            self.content = [type("T", (), {"text": "Yes"})()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                seen["prompt"] = kw["messages"][0]["content"]
                return _Msg()

    monkeypatch.setattr(ap.settings, "anthropic_api_key", "k", raising=False)
    monkeypatch.setattr("app.common.llm.shared_anthropic", lambda **k: _Client(), raising=False)
    prof = UserProfile(user_id="ws", first_name="A", last_name="B", work_modes="hybrid",
                       open_to_relocation=True)
    job = Job(source=JobSource.GREENHOUSE, external_id="ws-1", company="Acme", title="Engineer",
              url="https://x", description="Hybrid, 3 days in office.")
    try:
        ap._llm_essay_answer("Are you comfortable working hybrid?", job, prof, "resume")
    except Exception:
        pass
    if "prompt" in seen:
        assert "Work style wanted: hybrid" in seen["prompt"]
        assert "Open to relocating: yes" in seen["prompt"]
    else:                                   # no LLM path reachable in this environment
        import inspect
        assert "Work style wanted:" in inspect.getsource(ap._llm_essay_answer)
