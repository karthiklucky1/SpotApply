"""An unstated work mode is held, not delivered — and can still clear.

PRODUCTION, 2026-09-25. One board delivered five jobs. Four were on-site or
hybrid roles in cities the profile ruled out (`open_to_relocation: false`), and
all four were stamped `eligibility: eligible` with the reason "Located in United
States; work mode not stated". `eligibility.decide` fell through to its
permissive branch because `geo.work_mode` was empty, and Workday populates no
work mode at all.

The rationale in the old test was "the country check decides, the scorer sees
the city". The scorer does not gate on commute: it scored those four 72-78 and
shortlisted them.

Three pieces have to hold together, and each has tests here:

  1. `work_mode_from_description` recovers the work mode for free from text we
     already store — WITHOUT the false positives a bare `\\bremote\\b` over 20 KB
     of prose would produce.
  2. `decide` HOLDS when the mode is still unknown and it actually matters.
  3. `_pending_rows` keeps such a row verifiable, so the hold is not permanent.
     Without (3) the fix trades delivering unreachable jobs for an empty board
     with no recovery, which is worse.

Rows are prefixed `wme-` and deleted by that prefix.
"""
from __future__ import annotations

import pytest

from app.common.eligibility import (ELIGIBLE, INELIGIBLE, UNKNOWN, GeoPrefs,
                                    Geography, decide)
from app.discovery.geo_verify import work_mode_from_description

HOME = GeoPrefs(country="united states", remote_ok=True,
                open_to_relocation=False, home_location="Cincinnati, OH")
MOVER = GeoPrefs(country="united states", remote_ok=True,
                 open_to_relocation=True, home_location="Cincinnati, OH")
NO_CITY = GeoPrefs(country="united states", remote_ok=True,
                   open_to_relocation=False, home_location="")


def _geo(sites, work_mode="", countries=("united states",), **kw):
    return Geography(status="resolved", countries=list(countries), sites=list(sites),
                     work_mode=work_mode, remote_regions=[], areas=[], conflicts=[],
                     **kw)


# ── 1. the description scanner ───────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # Phrasings drawn from the five real postings in the 09-25 sample.
    ("Telecommuting permitted up to 2 days per week.", "hybrid"),
    ("This remote opportunity is ideal for candidates with strong expertise.", "remote"),
    ("Remote-first with flexible working hours.", "remote"),
    ("Design and develop backend services using Python and FastAPI.", ""),
    ("Leading architectural design and ensuring technical consistency.", ""),
    # Unambiguous statements that must resolve.
    ("This is a fully remote position.", "remote"),
    ("100% remote, US only.", "remote"),
    ("You will work from home.", "remote"),
    ("This role is hybrid.", "hybrid"),
    ("Hybrid schedule with 3 days per week in the office.", "hybrid"),
    ("Candidates are required to be on-site.", "onsite"),
    ("This is an in-office role.", "onsite"),
    ("Remote (US) - open to all states.", "remote"),
    ("", ""),
])
def test_the_description_scanner_reads_a_stated_work_mode(text, expected):
    assert work_mode_from_description(text) == expected


@pytest.mark.parametrize("text", [
    # THE REASON this is a separate scanner. `_WORK_MODE_RES` is a bare
    # \bremote\b / \bhybrid\b, which is safe on a short site string and
    # catastrophic on prose. A WRONG work mode produces a wrong eligibility
    # verdict; an absent one merely holds the posting, so every one of these
    # must read as unknown.
    "We build hybrid cloud infrastructure across AWS and Azure.",
    "Experience with remote monitoring and Remote Desktop tooling.",
    "Collaborate with our remote teams across four time zones.",
    "Build a hybrid search index combining BM25 and vectors.",
    "Support remote procedure calls and gRPC services.",
    "Familiarity with hybrid recommendation models.",
    "Debug remote sensors and edge devices in the field.",
])
def test_the_scanner_does_not_mistake_technical_prose_for_a_work_mode(text):
    assert work_mode_from_description(text) == ""


def test_a_bounded_schedule_is_hybrid_not_remote():
    """Order is load-bearing. "Telecommuting permitted up to 2 days per week" is
    a split week; a bare telecommuting pattern would call it remote and hand a
    New York office job to someone who will not move there."""
    assert work_mode_from_description(
        "Telecommuting permitted up to 2 days per week.") == "hybrid"
    assert work_mode_from_description(
        "Remote work is available 2 days per week.") == "hybrid"
    assert work_mode_from_description(
        "3 days a week in the office, the rest remote.") == "hybrid"


# ── 2. the decision ──────────────────────────────────────────────────────────

def test_an_unstated_work_mode_away_from_home_is_held():
    """The production case. Not ELIGIBLE (the old guess, permissive) and not
    INELIGIBLE (a guess the other way — the posting may be remote): UNKNOWN."""
    d = decide(_geo(["Philadelphia, PA"]), HOME)
    assert d.status == UNKNOWN
    assert d.code == "work_mode_unresolved"
    assert "Philadelphia, PA" in d.reason
    assert "pending verification" in d.reason


@pytest.mark.parametrize("site", ["Sunnyvale, CA", "Atlanta, GA",
                                  "New York, New York, United States of America",
                                  "US GA ATL 201 STE 900"])
def test_every_unreachable_site_from_the_production_sample_is_held(site):
    assert decide(_geo([site]), HOME).code == "work_mode_unresolved"


def test_the_hold_does_not_fire_when_the_answer_is_already_knowable():
    """It must hold only what it has to. Each of these is decidable without the
    work mode, so holding them would be gratuitous."""
    # Site in the user's own area: fine on any work mode.
    assert decide(_geo(["Cincinnati, OH"]), HOME).status == ELIGIBLE
    # Open to relocation: not blocked by a commute they would move for.
    assert decide(_geo(["Philadelphia, PA"]), MOVER).status == ELIGIBLE
    # Work mode stated: the existing rules decide it outright.
    assert decide(_geo(["Philadelphia, PA"], "remote"), HOME).status == ELIGIBLE
    assert decide(_geo(["Philadelphia, PA"], "onsite"), HOME).status == INELIGIBLE
    # No physical site at all (remote-only listing): nothing to commute to.
    assert decide(_geo(["Remote"]), HOME).status == ELIGIBLE


def test_a_user_who_never_entered_a_city_keeps_their_board():
    """`home_area_matches` returns None with no city, and holding on None would
    empty the board of every user who never typed one. The narrower ask already
    exists where the POSTING asserts a residence restriction."""
    assert decide(_geo(["Philadelphia, PA"]), NO_CITY).status == ELIGIBLE


def test_a_country_mismatch_still_decides_without_any_work_mode():
    """Regression on the fix itself. Making resolution depend on work mode once
    stopped a page-verified Stockholm posting from ever reaching
    `redecide_copies`, so a provably ineligible job stayed held forever."""
    d = decide(_geo(["Stockholm, SE"], countries=("sweden",)), HOME)
    assert d.status == INELIGIBLE and d.code == "country_mismatch"


def test_resolving_the_work_mode_clears_the_hold():
    """The recovery path, at the decision level: the same posting, once its mode
    is known, stops being held and gets a real verdict either way."""
    held = _geo(["Philadelphia, PA"])
    assert decide(held, HOME).status == UNKNOWN
    assert decide(_geo(["Philadelphia, PA"], "remote"), HOME).status == ELIGIBLE
    assert decide(_geo(["Philadelphia, PA"], "onsite"), HOME).status == INELIGIBLE


# ── 3. the hold is not permanent ─────────────────────────────────────────────

PREFIX = "wme-"


def _cleanup():
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job, JobGeography
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like(f"{PREFIX}%"))).all():
            s.delete(j)
        for g in s.exec(select(JobGeography).where(
                JobGeography.external_id.like(f"{PREFIX}%"))).all():
            s.delete(g)
        s.commit()


@pytest.fixture
def clean():
    _cleanup()
    yield
    _cleanup()


def test_a_country_resolved_row_with_no_work_mode_is_still_verifiable(clean):
    """WITHOUT THIS THE FIX IS A TRAP. `_pending_rows` used to select only
    `status != RESOLVED`. A posting whose country is known but whose work mode
    is not is RESOLVED, so it would never be re-verified and the
    `work_mode_unresolved` hold would be permanent — trading unreachable jobs
    for an empty board with no way back."""
    from datetime import datetime

    from app.db.init_db import get_session
    from app.db.models import Job, JobGeography, JobSource
    from app.discovery import geo_verify as gv

    ext = f"{PREFIX}held1"
    with get_session() as s:
        s.add(JobGeography(
            source="workday", external_id=ext, status="resolved",
            countries_json='["united states"]', sites_json='["Philadelphia, PA"]',
            work_mode=None, next_attempt_at=None, attempts=0,
            created_at=datetime.utcnow()))
        # An open, unscored, HELD copy — `_pending_rows` drops rows nobody waits
        # on, so the row only counts as pending because this exists.
        s.add(Job(user_id="local", source=JobSource.WORKDAY, external_id=ext,
                  company="Acme", title="Engineer",
                  url="https://acme.wd5.myworkdayjobs.com/x/job/Y_1",
                  description="d", location="Philadelphia, PA",
                  eligibility=UNKNOWN, rerank_score=None, is_closed=False))
        s.commit()

    picked = gv._pending_rows(50)
    assert any(r.external_id == ext for r in picked), (
        "a country-resolved row with no work mode must stay verifiable, or the "
        "hold it causes can never clear")


def test_a_row_with_a_work_mode_is_not_re_verified(clean):
    """The other half: once the mode is known the row is done, so this does not
    become an endless re-verification of every posting."""
    from datetime import datetime

    from app.db.init_db import get_session
    from app.db.models import Job, JobGeography, JobSource
    from app.discovery import geo_verify as gv

    ext = f"{PREFIX}done1"
    with get_session() as s:
        s.add(JobGeography(
            source="workday", external_id=ext, status="resolved",
            countries_json='["united states"]', sites_json='["Philadelphia, PA"]',
            work_mode="onsite", next_attempt_at=None, attempts=0,
            created_at=datetime.utcnow()))
        s.add(Job(user_id="local", source=JobSource.WORKDAY, external_id=ext,
                  company="Acme", title="Engineer",
                  url="https://acme.wd5.myworkdayjobs.com/x/job/Y_2",
                  description="d", location="Philadelphia, PA",
                  eligibility=UNKNOWN, rerank_score=None, is_closed=False))
        s.commit()

    assert not any(r.external_id == ext for r in gv._pending_rows(50))
