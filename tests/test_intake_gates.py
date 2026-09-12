"""Every door into a user's pool applies the SAME gate, and copying a posting
does not make it younger.

Three separate bugs, one shape (2026-09-12 audit):

  * the pulse lane — the door that delivers ~70% of shortlists — called
    ``_upsert`` with only ``user_id`` and ``user_keywords``, so it was the one
    ingestion path with no country gate and no role gate;
  * adoption skipped the country gate whenever the profile's country was blank,
    while the scoring prompt still told Claude the candidate wants the default
    country and scored everything else 0-30 — we admitted for free and paid to
    reject;
  * adoption rebuilt each posting as a RawJob without ``first_seen``, so
    ``_build_job`` stamped ``now`` and a three-week-old shared row entered the
    board as "found today".

Rows are prefixed ``ig_`` and deleted by that prefix: a wholesale delete(Job)
takes out fixtures other files built in the same session.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, UserProfile
from app.discovery.base import RawJob
from app.discovery.pipeline import SHARED_POOL_USER

_PREFIX = "ig_"


def _clean():
    with get_session() as s:
        s.exec(delete(Job).where(Job.external_id.like(f"{_PREFIX}%")))
        s.exec(delete(UserProfile).where(UserProfile.user_id.like(f"{_PREFIX}%")))
        s.commit()


def _shared(ext, title, *, held_days=0, location="Remote"):
    now = datetime.utcnow()
    return Job(user_id=SHARED_POOL_USER, source=JobSource.GREENHOUSE,
               external_id=_PREFIX + ext, company=f"Co{ext}", title=title,
               location=location, remote=True, url=f"https://x/{ext}",
               description="Python ML role at a great company.",
               posted_at=now - timedelta(days=1),
               first_seen=now - timedelta(days=held_days),
               discovered_at=now - timedelta(days=held_days))


# ── Freshness survives the copy ──────────────────────────────────────────────

def test_build_job_honours_a_carried_first_seen():
    from app.discovery.pipeline import _build_job
    origin = datetime.utcnow() - timedelta(days=9)
    r = RawJob(source="greenhouse", external_id=_PREFIX + "b1", company="Co",
               title="ML Engineer", location="Remote", remote=True,
               url="https://x/b1", description="d", first_seen=origin)
    job = _build_job(r, "hash", "slug", "ig_user", ["ml engineer"])
    assert job.first_seen == origin, "a copied posting must keep its first sighting"
    # last_seen is still "we touched it now" — only first_seen is carried.
    assert job.last_seen > origin


def test_build_job_still_stamps_now_for_a_genuine_first_sighting():
    from app.discovery.pipeline import _build_job
    before = datetime.utcnow()
    r = RawJob(source="greenhouse", external_id=_PREFIX + "b2", company="Co",
               title="ML Engineer", location="Remote", remote=True,
               url="https://x/b2", description="d")
    job = _build_job(r, "hash", "slug", None, None)
    assert job.first_seen >= before


def test_adoption_does_not_restart_the_freshness_clock():
    from app.strategy.adoption import adopt_shared_jobs
    _clean()
    held = 9
    with get_session() as s:
        s.add(UserProfile(user_id="ig_fresh", target_roles="Machine Learning Engineer",
                          preferred_country="United States"))
        s.add(_shared("f1", "Senior ML Engineer", held_days=held))
        s.commit()

    adopt_shared_jobs("ig_fresh")

    with get_session() as s:
        copy = s.exec(select(Job).where(Job.user_id == "ig_fresh",
                                        Job.external_id == _PREFIX + "f1")).first()
    assert copy is not None, "the posting should have been adopted"
    age_days = (datetime.utcnow() - copy.first_seen).total_seconds() / 86400.0
    assert age_days > held - 1, (
        "the user's copy was stamped first_seen=now, so a posting we had held "
        f"for {held} days re-entered the board as found-today (age {age_days:.1f}d)")
    _clean()


# ── The country gate applies at every door ───────────────────────────────────

def test_adoption_applies_the_country_gate_when_the_profile_has_none():
    """A blank preferred_country must not mean 'admit every country'."""
    from app.config import settings
    from app.strategy.adoption import adopt_shared_jobs
    assert settings.default_intake_country, (
        "this test is about the default the scoring prompt also assumes")
    _clean()
    with get_session() as s:
        # No preferred_country at all — the case that used to disable the gate.
        s.add(UserProfile(user_id="ig_blank", target_roles="Machine Learning Engineer"))
        s.add(_shared("c1", "Machine Learning Engineer", location="Austin, TX"))
        s.add(_shared("c2", "Machine Learning Engineer", location="Zurich, Switzerland"))
        s.add(_shared("c3", "Machine Learning Engineer", location="Remote (Europe)"))
        s.commit()

    adopt_shared_jobs("ig_blank")

    with get_session() as s:
        got = {j.external_id for j in
               s.exec(select(Job).where(Job.user_id == "ig_blank")).all()}
    assert _PREFIX + "c1" in got
    assert _PREFIX + "c2" not in got, "a Swiss posting reached a US-default user"
    assert _PREFIX + "c3" not in got, "an EU-only remote posting reached a US-default user"
    _clean()


def test_upsert_country_gate_drops_foreign_postings():
    from app.discovery.pipeline import _upsert
    _clean()
    now = datetime.utcnow()
    raw = [RawJob(source="greenhouse", external_id=_PREFIX + "u1", company="Co",
                  title="Machine Learning Engineer", location="Austin, TX",
                  remote=False, url="https://x/u1", description="d", posted_at=now),
           RawJob(source="greenhouse", external_id=_PREFIX + "u2", company="Co",
                  title="Machine Learning Engineer", location="Milan, Italy",
                  remote=False, url="https://x/u2", description="d", posted_at=now)]
    _upsert(raw, user_id="ig_gate", preferred_country="United States",
            remote_ok=True, user_keywords=["machine learning engineer"])
    with get_session() as s:
        got = {j.external_id for j in
               s.exec(select(Job).where(Job.user_id == "ig_gate")).all()}
    assert _PREFIX + "u1" in got
    assert _PREFIX + "u2" not in got
    _clean()


# ── The pulse lane carries the preferences it needs ──────────────────────────

def test_active_users_carry_location_preferences():
    """`_active_users` is what the pulse lane routes on; without these keys the
    lane has nothing to pass to the gate."""
    from app.strategy.hot_lane import _active_users
    sig = inspect.getsource(_active_users)
    assert "preferred_country" in sig and "remote_ok" in sig


def test_pulse_per_user_routing_passes_the_full_gate():
    """The pulse lane's per-user `_upsert` must pass country, remote and the
    role gate — not just user_id and keywords.

    Asserted on the source because reproducing a whole tick would need a live
    board; the call site is the invariant that regressed."""
    import app.strategy.pulse_lane as pl
    src = inspect.getsource(pl)
    marker = "n = _upsert(relevant,"
    assert marker in src, "the per-user routing call moved — re-point this guard"
    call = src[src.index(marker):src.index(marker) + 600]
    for kw in ("preferred_country=", "remote_ok=", "role_gate_terms=", "user_keywords="):
        assert kw in call, f"pulse per-user routing no longer passes {kw}"
