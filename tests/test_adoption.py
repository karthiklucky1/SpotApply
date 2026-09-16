"""Adoption: instant per-user feeds copied from the shared job pool (no HTTP)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.db.init_db import get_session
from app.db.models import Job, JobSource, UserProfile
from app.discovery.pipeline import SHARED_POOL_USER


def _clean():
    with get_session() as s:
        s.exec(delete(Job))
        s.exec(delete(UserProfile))
        s.commit()


def _shared_job(ext_id, title, days_old=1, closed=False):
    now = datetime.utcnow()
    return Job(user_id=SHARED_POOL_USER, source=JobSource.GREENHOUSE,
               external_id=ext_id, company=f"Co{ext_id}", title=title,
               location="Remote", remote=True, url=f"https://x/{ext_id}",
               description="Python ML role at a great company.",
               posted_at=now - timedelta(days=days_old),
               first_seen=now - timedelta(days=days_old), is_closed=closed)


def _shared_job_split(ext_id, title, *, held_days, posted_days):
    """A shared-pool row whose two timestamps are set INDEPENDENTLY.

    ``_shared_job`` ties them together, which is exactly the conflation that hid
    this bug: with one "age" number you cannot express "we found it this morning
    but the ATS dates it three weeks back."
    """
    now = datetime.utcnow()
    return Job(user_id=SHARED_POOL_USER, source=JobSource.GREENHOUSE,
               external_id=ext_id, company=f"Co{ext_id}", title=title,
               location="Remote", remote=True, url=f"https://x/{ext_id}",
               description="Python ML role at a great company.",
               posted_at=(None if posted_days is None
                          else now - timedelta(days=posted_days)),
               first_seen=now - timedelta(days=held_days),
               discovered_at=now - timedelta(days=held_days))


def test_adoption_does_not_drop_a_fresh_find_with_an_old_source_date():
    """Adoption was the EARLIEST gate on the forward path and the last place
    ``posted_at`` still won a coalesce, so it silently narrowed the effective
    posted bound from ``scoring_max_posted_age_days`` (30d) to
    ``ADOPT_MAX_AGE_DAYS`` (21d).

    A posting discovered today that an ATS dated 25 days back was discarded
    here and never entered the user's pool at all — so neither the scoring gate
    nor the render window ever got the chance to keep it. It also made the
    outcome depend on WHICH LANE found the job, since the pulse lane's per-user
    upsert applies no age filter at all.

    Both bounds are still enforced: a genuinely ancient source date is dropped,
    and so is a shared row we have been sitting on for weeks.
    """
    from app.config import settings
    from app.strategy.adoption import ADOPT_MAX_AGE_DAYS, adopt_shared_jobs

    assert settings.scoring_max_posted_age_days > ADOPT_MAX_AGE_DAYS, (
        "this test only means something while the posted bound reaches further "
        "back than adoption's known-age window")

    _clean()
    with get_session() as s:
        s.add(UserProfile(user_id="u_split", target_roles="Machine Learning Engineer"))
        # Found today; ATS says 25d. Inside the 30d posted bound -> must adopt.
        s.add(_shared_job_split("sp1", "Senior ML Engineer",
                                held_days=0, posted_days=25))
        # Found today; no source date at all -> judged on known age only.
        s.add(_shared_job_split("sp2", "Machine Learning Engineer",
                                held_days=0, posted_days=None))
        # Found today; genuinely ancient/evergreen -> still suppressed.
        s.add(_shared_job_split("sp3", "ML Engineer",
                                held_days=0, posted_days=400))
        # Sat in the shared pool past the known-age window -> not adopted.
        s.add(_shared_job_split("sp4", "ML Platform Engineer",
                                held_days=ADOPT_MAX_AGE_DAYS + 4, posted_days=1))
        s.commit()

    adopt_shared_jobs("u_split")

    with get_session() as s:
        got = {j.external_id for j in
               s.exec(select(Job).where(Job.user_id == "u_split")).all()}

    assert "sp1" in got, (
        "a posting discovered today was dropped at adoption because an "
        "unreliable ATS date called it old — it can never reach scoring")
    assert "sp2" in got
    assert "sp3" not in got, "an evergreen listing must still be suppressed"
    assert "sp4" not in got, "a shared row held past the window must not be adopted"
    _clean()


def test_adopt_copies_role_matching_recent_jobs_only():
    _clean()
    with get_session() as s:
        s.add(UserProfile(user_id="u_adopt", target_roles="Machine Learning Engineer"))
        s.add(_shared_job("s1", "Senior ML Engineer", days_old=2))       # match
        s.add(_shared_job("s2", "Product Designer", days_old=2))         # wrong role
        s.add(_shared_job("s3", "Machine Learning Engineer", days_old=40))  # too old
        s.add(_shared_job("s4", "MLOps Engineer", days_old=5, closed=True))  # closed
        s.commit()

    from app.strategy.adoption import adopt_shared_jobs
    inserted = adopt_shared_jobs("u_adopt")
    assert inserted == 1

    with get_session() as s:
        mine = s.exec(select(Job).where(Job.user_id == "u_adopt")).all()
    assert [j.title for j in mine] == ["Senior ML Engineer"]
    # The copy keeps the original posting date (freshness stays honest).
    assert mine[0].posted_at is not None

    # Second pass is a no-op — dedupe by (source, external_id) per user.
    assert adopt_shared_jobs("u_adopt") == 0


def test_adopt_backfills_after_role_edit():
    """Editing roles must surface already-collected shared jobs for the NEW
    role — the reason adoption is triggered from the target-roles endpoint."""
    _clean()
    with get_session() as s:
        s.add(UserProfile(user_id="u_pivot", target_roles="Machine Learning Engineer"))
        s.add(_shared_job("p1", "Senior ML Engineer", days_old=1))
        s.add(_shared_job("p2", "Data Engineer", days_old=1))
        s.commit()

    from app.strategy.adoption import adopt_shared_jobs
    assert adopt_shared_jobs("u_pivot") == 1  # only the ML job

    # User pivots to data engineering — adoption now pulls the data job too.
    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == "u_pivot")).first()
        p.target_roles = "Data Engineer"
        s.add(p)
        s.commit()
    assert adopt_shared_jobs("u_pivot") == 1

    with get_session() as s:
        titles = sorted(j.title for j in
                        s.exec(select(Job).where(Job.user_id == "u_pivot")).all())
    assert titles == ["Data Engineer", "Senior ML Engineer"]


# ══════════════════════════════════════════════════════════════════════════
# The cap must bound NEW jobs, not re-selected duplicates
# ══════════════════════════════════════════════════════════════════════════
# Reviewed 2026-09-16. The newest page was cut to `limit` and deduplicated only
# later, inside `_upsert` — so every later pass re-selected the same
# already-copied jobs, inserted nothing, and eligible postings ranked below the
# cap were never adopted at all. Not a slow feed: a permanently stuck one.

def test_a_second_pass_reaches_jobs_the_cap_cut_off_the_first_time():
    _clean()
    with get_session() as s:
        s.add(UserProfile(user_id="u_cap", target_roles="Machine Learning Engineer"))
        # Newest first: c1, c2, c3. A two-job cap takes c1+c2 on pass one.
        s.add(_shared_job("c1", "Senior ML Engineer", days_old=1))
        s.add(_shared_job("c2", "ML Engineer", days_old=2))
        s.add(_shared_job("c3", "Machine Learning Engineer", days_old=3))
        s.commit()

    from app.strategy.adoption import adopt_shared_jobs
    assert adopt_shared_jobs("u_cap", limit=2) == 2
    assert adopt_shared_jobs("u_cap", limit=2) == 1, \
        "the third job must eventually be adopted, not re-skipped forever"
    assert adopt_shared_jobs("u_cap", limit=2) == 0   # and then it is genuinely done

    with get_session() as s:
        mine = {j.external_id for j in
                s.exec(select(Job).where(Job.user_id == "u_cap")).all()}
    assert mine == {"c1", "c2", "c3"}


def test_already_adopted_jobs_do_not_consume_the_cap():
    """The direct statement of the bug: with the whole pool already adopted,
    a pass must not spend its budget re-selecting copies."""
    _clean()
    with get_session() as s:
        s.add(UserProfile(user_id="u_dup", target_roles="Machine Learning Engineer"))
        for n in range(5):
            s.add(_shared_job(f"d{n}", "ML Engineer", days_old=n + 1))
        s.commit()

    from app.strategy.adoption import _drop_already_adopted, adopt_shared_jobs
    assert adopt_shared_jobs("u_dup", limit=10) == 5

    with get_session() as s:
        shared = s.exec(select(Job).where(Job.user_id == SHARED_POOL_USER)).all()
    assert _drop_already_adopted(shared, "u_dup") == [], \
        "every one of these is already in the user's pool"


def test_the_duplicate_filter_keeps_jobs_another_user_adopted():
    """Adoption is per-user. One user's copy must never hide a posting from
    everyone else — that would be a cross-tenant leak of the worst kind."""
    _clean()
    with get_session() as s:
        s.add(UserProfile(user_id="u_a", target_roles="Machine Learning Engineer"))
        s.add(UserProfile(user_id="u_b", target_roles="Machine Learning Engineer"))
        s.add(_shared_job("x1", "ML Engineer", days_old=1))
        s.commit()

    from app.strategy.adoption import adopt_shared_jobs
    assert adopt_shared_jobs("u_a", limit=5) == 1
    assert adopt_shared_jobs("u_b", limit=5) == 1, "u_b gets their own copy"
