"""Target roles that people can actually pick (founder, 2026-10-28).

"When I type python developer nothing shows — I just pressed Enter." The box
accepted any text, lower-cased, and offered six generic suggestions. Many
users do not know which titles to search, and those titles decide what reaches
their board. Now the box completes real titles and their neighbours as they
type, and the page suggests roles from the resume, roles similar to the ones
picked, and roles common in their field — each with the reason.

Rows this file writes carry the ``rolesuggesttest-`` prefix and are removed by it.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete, select

from app.api import server
from app.common import role_catalog as rc
from app.db.init_db import get_session
from app.db.models import UserProfile

UID = "rolesuggesttest-user"
DASH = (Path(__file__).resolve().parents[1] / "app" / "templates" / "dashboard.html").read_text()


def _titles(items):
    return [i["title"] for i in items]


# ── typeahead ───────────────────────────────────────────────────────────────

def test_typing_python_developer_offers_it_and_its_neighbours():
    out = rc.complete("python developer")
    assert out[0] == {"title": "Python Developer", "why": ""}
    titles = _titles(out)
    assert {"Backend Engineer", "Software Engineer"} <= set(titles)
    assert all(i["why"] == "similar role" for i in out[1:] if i["title"] in ("Backend Engineer",))


@pytest.mark.parametrize("q, want", [
    ("py", "Python Developer"), ("ml eng", "Machine Learning Engineer"), ("dev ops", "DevOps Engineer"),
    ("gen ai", "Generative AI Engineer"), ("full st", "Full Stack Engineer"), ("nurse", "Registered Nurse"),
])
def test_partial_words_find_the_title(q, want):
    assert want in _titles(rc.complete(q))


def test_too_short_unknown_and_already_chosen_give_nothing_extra():
    assert rc.complete("p") == [] and rc.complete("zzzz") == []
    assert "Python Developer" not in _titles(rc.complete("python dev", exclude=["python developer"]))


def test_a_typed_title_is_stored_in_title_case_with_acronyms():
    assert rc.canonical("python developer") == "Python Developer"
    assert rc.canonical("ai engineer") == "AI Engineer"
    assert rc.canonical("senior ios engineer") == "Senior iOS Engineer"
    assert rc.canonical("Quantum Whisperer") == "Quantum Whisperer"


# ── suggestions from the resume ─────────────────────────────────────────────

FOUNDER = SimpleNamespace(
    current_title="AI Full Stack Developer", years_experience=4,
    key_skills="Python, FastAPI, Flask, Azure OpenAI, LangChain, RAG, PostgreSQL, Docker, "
               "Kubernetes, REST APIs, AI/ML, React.js, AWS, GCP, CI/CD, Git, Microservices",
    experience_json=json.dumps([{"title": "AI Full Stack Developer"}, {"title": "Software Developer"}]))


def test_resume_suggestions_name_their_reason():
    groups = {g["key"]: g for g in rc.suggest(FOUNDER, [], popular=["Software Engineer"])}
    resume = {i["title"]: i["why"] for i in groups["resume"]["items"]}
    assert resume["AI Full Stack Developer"] == "your current title"
    assert resume["Software Developer"] == "a title on your resume"
    assert resume["AI Engineer"].startswith("fits your skills:") and "LangChain" in resume["AI Engineer"]
    assert "Python" in resume["Python Developer"]
    # "Azure OpenAI" is an AI skill, not evidence for a cloud job.
    assert "Azure OpenAI" not in resume.get("Cloud Engineer", "")


def test_similar_roles_name_the_role_they_are_similar_to_and_nothing_repeats():
    chosen = ["Generative AI Platform Engineer", "Backend Engineer"]
    groups = rc.suggest(FOUNDER, chosen, popular=["Backend Engineer", "Software Engineer", "Data Scientist"])
    related = next(g for g in groups if g["key"] == "related")["items"]
    whys = {i["why"] for i in related}
    assert "similar to Backend Engineer" in whys and "similar to Generative AI Platform Engineer" in whys
    all_titles = [i["title"].lower() for g in groups for i in g["items"]]
    assert len(all_titles) == len(set(all_titles)), "a title appears once"
    assert not {c.lower() for c in chosen} & set(all_titles), "never a role already chosen"


def test_a_junior_is_not_offered_senior_titles():
    junior = SimpleNamespace(current_title="Junior Developer", years_experience=1,
                             key_skills="Python, Docker", experience_json=None)
    groups = rc.suggest(junior, ["Software Engineer"],
                        popular=["Senior Software Engineer", "Staff Software Engineer", "Data Analyst"])
    titles = [i["title"] for g in groups for i in g["items"]]
    assert not [t for t in titles if t.startswith(("Senior", "Staff", "Principal"))]


def test_an_empty_profile_gets_only_the_popular_group():
    groups = rc.suggest(SimpleNamespace(), [], popular=["Software Engineer"])
    assert [g["key"] for g in groups] == ["popular"]


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "_require_user", lambda request: UID)
    monkeypatch.setattr(server, "_get_user_id", lambda request: UID)
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id == UID))
        s.add(UserProfile(user_id=UID, current_title="Data Analyst", years_experience=3,
                          key_skills="SQL, Tableau, Python"))
        s.commit()
    yield TestClient(server.app)
    with get_session() as s:
        s.exec(delete(UserProfile).where(UserProfile.user_id == UID))
        s.commit()


def test_the_suggest_route_completes_and_groups(client):
    d = client.get("/api/target-roles/suggest", params={"q": "python dev"}).json()
    assert d["matches"][0]["title"] == "Python Developer" and d["typed"] == "Python Dev"
    g = client.get("/api/target-roles/suggest", params={"roles": "Data Analyst"}).json()["groups"]
    assert g and all(i["title"] != "Data Analyst" for grp in g for i in grp["items"])
    assert "groups" in client.get("/api/target-roles").json()
    assert "_require_user(request)" in inspect.getsource(server.suggest_target_roles)


def test_a_lower_case_role_is_saved_in_title_case(client, monkeypatch):
    import app.strategy.adoption as adoption
    import app.strategy.realign as realign
    monkeypatch.setattr(adoption, "seed_new_user", lambda uid: 0)
    monkeypatch.setattr(realign, "realign_if_roles_changed", lambda *a, **k: None)
    r = client.put("/api/target-roles", json={"roles": ["python developer", "Backend Engineer", "PYTHON DEVELOPER"]})
    assert r.json()["roles"] == ["Python Developer", "Backend Engineer"]
    with get_session() as s:
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == UID)).one()
        assert prof.target_roles == "Python Developer, Backend Engineer"


# ── the page ────────────────────────────────────────────────────────────────

def test_the_roles_box_is_a_typeahead_and_the_page_uses_the_screen():
    assert 'id="role-input"' in DASH and 'role="combobox"' in DASH and 'id="role-typeahead"' in DASH
    assert "/api/target-roles/suggest?q=" in DASH and "ArrowDown" in DASH
    # Two columns that stack on small screens; a full page in the setup.
    assert ".rm-grid { display:grid; grid-template-columns:minmax(0,1.2fr) minmax(0,1fr)" in DASH
    assert "modal.classList.toggle('rm-page', _rolesOnboarding)" in DASH
    # A title left in the box is saved, not lost.
    save = DASH[DASH.index("async function saveRoles()"):]
    save = save[:save.index("\n        }\n")]
    assert "inp.value.trim()" in save and "_pushRole" in save


def test_the_setup_pages_use_the_full_width():
    assert "max-width:880px" not in DASH
    assert "#profile-review .pr-grid { display:grid; grid-template-columns:minmax(0,1.45fr) minmax(0,1fr)" in DASH
