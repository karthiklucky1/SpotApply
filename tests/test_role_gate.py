"""Per-user role gate in _upsert: a board dump can't flood one user's pool with
off-role postings, while the shared pool and un-gated calls keep everything."""
from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Job
from app.discovery.base import RawJob
from app.discovery.pipeline import SHARED_POOL_USER, _upsert


def _clean():
    with get_session() as s:
        s.exec(delete(Job))
        s.commit()


def _raw(ext, title, company="Acme"):
    return RawJob(source="greenhouse", external_id=str(ext), company=company,
                  title=title, location="Remote", remote=True,
                  url=f"https://boards.greenhouse.io/acme/jobs/{ext}",
                  description="desc", posted_at=None)


BATCH = [
    _raw(1, "Senior Machine Learning Engineer"),
    _raw(2, "AI/ML Engineer"),
    _raw(3, "Mechanical Engineer III - Battery Systems"),
    _raw(4, "Contact Center Systems Analyst"),
    _raw(5, "Systems Engineer"),
]


def test_role_gate_keeps_on_role_drops_noise():
    _clean()
    _upsert(BATCH, user_id="u_ml", role_gate_terms=["machine learning engineer"])
    with get_session() as s:
        titles = {j.title for j in s.exec(select(Job).where(Job.user_id == "u_ml")).all()}
    assert "Senior Machine Learning Engineer" in titles
    assert "AI/ML Engineer" in titles
    assert "Mechanical Engineer III - Battery Systems" not in titles
    assert "Contact Center Systems Analyst" not in titles
    assert "Systems Engineer" not in titles


def test_no_gate_keeps_everything():
    _clean()
    _upsert(BATCH, user_id="u_all")  # no role_gate_terms
    with get_session() as s:
        n = len(s.exec(select(Job).where(Job.user_id == "u_all")).all())
    assert n == len(BATCH)


def test_shared_pool_is_never_role_gated():
    _clean()
    _upsert(BATCH, user_id=SHARED_POOL_USER)  # run_discovery passes role_gate_terms=None here
    with get_session() as s:
        n = len(s.exec(select(Job).where(Job.user_id == SHARED_POOL_USER)).all())
    assert n == len(BATCH)


# ── Role families: neighbouring titles must reach the user ───────────────────
# The gate keyed off distinctive domain tokens only, so terms for "Software
# Developer" came out as just {"software developer", "software"} — "developer"
# is a generic token and no alias key appears in the phrase. A Backend
# Developer posting was therefore rejected for a Software Developer user, and
# Machine Learning Engineer for an AI Engineer user: exactly the postings
# people most want. Families make the expansion symmetric.

import pytest  # noqa: E402
from app.discovery.title_filter import role_title_match  # noqa: E402


@pytest.mark.parametrize("title", [
    "Backend Developer", "Backend Engineer", "Back-End Engineer",
    "Frontend Engineer", "Full Stack Developer", "Fullstack Engineer",
    "Software Engineer II", "SDE II", "Python Developer",
    "Web Developer", "Programmer",
])
def test_software_family_reaches_a_software_developer(title):
    assert role_title_match(title, ["Software Developer"]), (
        f"{title!r} is the same market as Software Developer — a user who "
        f"never sees it is the expensive failure, not a wasted score")


@pytest.mark.parametrize("title", [
    "Machine Learning Engineer", "Senior ML Engineer", "MLOps Engineer",
    "Deep Learning Engineer", "LLM Engineer", "GenAI Engineer",
])
def test_ai_family_reaches_an_ai_engineer(title):
    assert role_title_match(title, ["AI Engineer"]), (
        f"{title!r} and AI Engineer are the same job under different names")


def test_the_families_are_symmetric():
    """Targeting one flavour reaches the others, in both directions."""
    assert role_title_match("Software Engineer", ["Backend Engineer"])
    assert role_title_match("Backend Engineer", ["Software Engineer"])
    assert role_title_match("AI Engineer", ["Machine Learning Engineer"])
    assert role_title_match("Machine Learning Engineer", ["AI Engineer"])


@pytest.mark.parametrize("title", [
    "Registered Nurse", "Mechanical Engineer", "Sales Engineer",
    "Staff Accountant", "Business Development Manager", "Civil Engineer",
])
def test_widening_the_families_did_not_open_the_gate(title):
    """Breadth within a family must not become 'everything matches'."""
    assert not role_title_match(title, ["Software Developer"])
    assert not role_title_match(title, ["AI Engineer"])


def test_a_nurse_still_only_gets_nursing():
    assert role_title_match("Registered Nurse (ICU)", ["Registered Nurse"])
    assert not role_title_match("Software Engineer", ["Registered Nurse"])
