"""The job drawer's sponsorship card, for a user who never needs sponsorship.

Observed 2026-10-14 on a U.S. citizen's account: the card read "Sponsorship not
stated — The posting is silent and we found no public filing record — many
employers still sponsor case-by-case, so it's worth a quick ask." Advice about
asking an employer to sponsor is irrelevant to someone who never needs it, and
reads as if they might.

The rule pinned here: the ONE work-auth answer decides
(`tenant_prefs.needs_sponsorship`, i.e. `work_auth.assess_profile`), and "no
sponsorship needed" is said only when the user SAVED a status that establishes
it and the authorisation is live. A blank profile is not evidence of anything,
so it keeps the posting-side card; so does an expired or undated status.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobSource, UserProfile

_PREFIX = "spcard-test-"
_FIELDS = ("work_authorization", "visa_status", "work_auth_status",
           "requires_sponsorship", "ead_end_date", "preferred_country")
_SILENT_JD = ("Join the Payments platform team. Full-time salaried position. We build "
              "the APIs that move money for millions of businesses.")


def _client():
    from fastapi.testclient import TestClient

    from app.api.server import app
    return TestClient(app)


@pytest.fixture
def local_profile():
    """The suite's "local" user, with its work-auth fields restored afterwards —
    other files use the same row, so this test never deletes one it did not make."""
    with get_session() as s:
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
        created = prof is None
        if created:
            prof = UserProfile(user_id="local")
            s.add(prof)
            s.commit()
            s.refresh(prof)
        saved = {f: getattr(prof, f) for f in _FIELDS}

    def set_status(**values):
        with get_session() as s:
            p = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
            for f in _FIELDS:
                setattr(p, f, values.get(f, False if f == "requires_sponsorship" else ""))
            s.add(p)
            s.commit()

    yield set_status

    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
        if p is not None:
            if created:
                s.delete(p)
            else:
                for f, v in saved.items():
                    setattr(p, f, v)
                s.add(p)
            s.commit()


@pytest.fixture
def app_id():
    with get_session() as s:
        j = Job(source=JobSource.MANUAL, external_id=f"{_PREFIX}1", company="Orchard Payments Co",
                title="Backend Engineer", url="http://x.test/spcard", description=_SILENT_JD,
                user_id="local")
        s.add(j)
        s.commit()
        s.refresh(j)
        a = Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, user_id="local")
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
    yield aid
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like(f"{_PREFIX}%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def _candidate(app_id):
    r = _client().get(f"/application/{app_id}/sponsorship")
    assert r.status_code == 200, r.text
    return r.json()["candidate"]


@pytest.mark.parametrize("status", [
    dict(work_auth_status="Citizen", work_authorization="US Citizen"),
    dict(work_authorization="U.S. Citizen"),
    dict(work_auth_status="Green Card", work_authorization="Green card / permanent resident"),
])
def test_a_user_who_never_needs_sponsorship_is_told_so(local_profile, app_id, status):
    local_profile(**status)
    c = _candidate(app_id)
    assert c and c["needs_sponsorship"] is False, c
    # The interpreter's own wording, not a second one invented for the card.
    assert "no sponsorship" in c["summary"].lower()
    assert not c["summary"].startswith("✅")


def test_a_user_who_needs_sponsorship_keeps_the_posting_side_card(local_profile, app_id):
    local_profile(work_auth_status="OPT", work_authorization="F-1 OPT", ead_end_date="2099-01-01")
    assert _candidate(app_id) == {"needs_sponsorship": True}


def test_a_blank_profile_is_never_read_as_no_sponsorship_needed(local_profile, app_id):
    """`assess_profile` defaults a blank profile to "Authorized to work" — right for
    a filter, wrong as a statement to the user. Not knowing is not evidence."""
    local_profile()
    assert _candidate(app_id) is None


def test_an_expired_authorisation_is_not_called_no_sponsorship_needed(local_profile, app_id):
    local_profile(work_authorization="TN", ead_end_date="2001-01-01")
    assert _candidate(app_id) is None


def test_the_card_agrees_with_the_one_interpreter(local_profile, app_id):
    from app.common.tenant_prefs import needs_sponsorship
    for status in (dict(work_auth_status="Citizen"), dict(work_auth_status="STEM OPT",
                                                          ead_end_date="2099-01-01"),
                   dict(work_auth_status="Needs Sponsorship", requires_sponsorship=True)):
        local_profile(**status)
        with get_session() as s:
            prof = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
            expected = needs_sponsorship(prof)
        assert _candidate(app_id)["needs_sponsorship"] is expected, status


def test_the_local_single_user_profile_is_found(app_id):
    """Local single-user mode keeps its profile with user_id NULL (that is how
    the demo account where this was seen is stored, and what every other route
    reads via `_get_or_create_profile(None)`); this route matched only
    user_id == "local", so it never saw the citizen's status at all."""
    with get_session() as s:
        held = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).all()
        held_ids = [p.id for p in held]
        for p in held:
            p.user_id = f"{_PREFIX}held"
            s.add(p)
        s.commit()
        row = s.exec(select(UserProfile).where(UserProfile.user_id.is_(None))).first()
        created = row is None
        if created:
            row = UserProfile(user_id=None)
            s.add(row)
            s.commit()
            s.refresh(row)
        saved = {f: getattr(row, f) for f in _FIELDS}
        row_id = row.id
        for f in _FIELDS:
            setattr(row, f, False if f == "requires_sponsorship" else "")
        row.work_auth_status, row.work_authorization = "Citizen", "Citizen"
        s.add(row)
        s.commit()
    try:
        c = _candidate(app_id)
        assert c and c["needs_sponsorship"] is False, c
    finally:
        with get_session() as s:
            row = s.get(UserProfile, row_id)
            if created:
                s.delete(row)
            else:
                for f, v in saved.items():
                    setattr(row, f, v)
                s.add(row)
            for pid in held_ids:
                p = s.get(UserProfile, pid)
                p.user_id = "local"
                s.add(p)
            s.commit()


def test_the_route_uses_the_one_interpreter_not_a_second_check():
    import inspect

    from app.api import server
    src = inspect.getsource(server.application_sponsorship)
    assert "needs_sponsorship" in src and "status_on_file" in src
    for raw in ("work_authorization", "work_auth_status", "visa_status", "requires_sponsorship"):
        assert raw not in src, f"the route reads {raw} itself — the interpreter decides"


def test_the_drawer_renders_the_no_need_card_and_keeps_the_rest():
    html = (Path(__file__).resolve().parents[1] / "app" / "templates"
            / "dashboard.html").read_text(encoding="utf-8")
    block = html[html.index("async function loadSponsorship"):]
    block = block[:block.index("function renderAutopsyButton")]
    assert "d.candidate.needs_sponsorship === false" in block
    assert "No sponsorship needed" in block
    # The branch comes first, so a citizen never reaches the "worth a quick ask" copy…
    assert block.index("if (noNeed)") < block.index("Sponsorship not stated")
    # …and everyone who does need it still gets every posting-side verdict.
    for kept in ("Sponsorship not stated", "No sponsorship for this role",
                 "Conflicting sponsorship signals", "This employer sponsors work visas"):
        assert kept in block
