"""The forward path, end to end: does a newly-discovered posting survive?

Every other test in this area checks ONE stage. This file walks the canonical
problem case through ALL of them in order, because the bug it guards against was
never in a single stage — it was the same wrong idea ("posted_at is how old this
job is") repeated at four independent gates. Fixing three of four looks green in
unit tests and still delivers nothing: the job dies at whichever gate was
missed.

The canonical case is a posting SpotApply found minutes ago that an ATS dates
three weeks back. It is not stale — we have never had the chance to show it to
anyone. Production measured 36.7% of intake as >7d old at first sight and a
~91.5h median detection lag, so this is not an edge case, it is the common one.

Stages, in the order a job meets them:

    shared pool -> adoption -> age gate -> scoring queue -> render window

A negative control (an evergreen listing dated 400 days back) rides alongside
through every stage, so "everything survives" can never be mistaken for a pass.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import delete, select

from app.common.freshness import is_fresh
from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import Job, JobSource, UserProfile
from app.discovery.pipeline import SHARED_POOL_USER

_PREFIX = "fwd-"
_USER = "fwd-path-user"
_ROLE = "Machine Learning Engineer"


def _cleanup():
    with get_session() as s:
        s.exec(delete(Job).where(Job.external_id.like(f"{_PREFIX}%")))
        s.exec(delete(UserProfile).where(UserProfile.user_id == _USER))
        s.commit()


def _shared(ext, title, *, held_days, posted_days):
    """A shared-pool posting with the two timestamps set INDEPENDENTLY."""
    now = datetime.utcnow()
    return Job(
        user_id=SHARED_POOL_USER, source=JobSource.GREENHOUSE,
        external_id=_PREFIX + ext, company=f"Co-{ext}", title=title,
        location="Remote", remote=True, url=f"https://x.test/{_PREFIX}{ext}",
        description="Python and PyTorch role building ML systems at scale.",
        posted_at=(None if posted_days is None
                   else now - timedelta(days=posted_days)),
        first_seen=now - timedelta(days=held_days),
        discovered_at=now - timedelta(days=held_days),
    )


def _user_rows() -> dict[str, Job]:
    with get_session() as s:
        return {j.external_id: j for j in s.exec(
            select(Job).where(Job.user_id == _USER,
                              Job.external_id.like(f"{_PREFIX}%"))).all()}


def test_a_fresh_find_with_an_old_source_date_survives_the_whole_forward_path():
    from app.strategy.adoption import adopt_shared_jobs
    from app.strategy.scoring_lane import _expire_stale_unscored, _user_queue

    init_db()
    _cleanup()

    with get_session() as s:
        s.add(UserProfile(user_id=_USER, target_roles=_ROLE))
        # THE CASE: found minutes ago, ATS says three weeks back.
        s.add(_shared("keep", "Senior Machine Learning Engineer",
                      held_days=0, posted_days=21))
        # NEGATIVE CONTROL: found minutes ago, but genuinely evergreen.
        s.add(_shared("evergreen", "Machine Learning Engineer",
                      held_days=0, posted_days=400))
        s.commit()

    # ── Stage 1: adoption ────────────────────────────────────────────────────
    adopt_shared_jobs(_USER)
    rows = _user_rows()
    assert _PREFIX + "keep" in rows, (
        "STAGE 1 (adoption): dropped before it could ever be scored")
    assert _PREFIX + "evergreen" not in rows, (
        "STAGE 1 (adoption): an evergreen listing was adopted")
    kept_id = rows[_PREFIX + "keep"].id

    # The user's copy starts its own clock — known age is how long WE have had
    # it in THIS pool, which is what the age gate measures.
    assert rows[_PREFIX + "keep"].rerank_score is None, "should arrive unscored"

    # ── Stage 2: the age gate ────────────────────────────────────────────────
    _expire_stale_unscored()
    after = _user_rows()[_PREFIX + "keep"]
    assert after.rerank_score is None, (
        "STAGE 2 (age gate): expired on arrival because of the source date")
    assert after.expired_at is None

    # ── Stage 3: the scoring queue ───────────────────────────────────────────
    assert kept_id in _user_queue(_USER, cap=50), (
        "STAGE 3 (scoring queue): survived the gate but is not scorable — it "
        "would sit queued forever")

    # ── Stage 4: the render window ───────────────────────────────────────────
    with get_session() as s:
        visible = {(r[0] if isinstance(r, tuple) else r) for r in s.exec(
            select(Job.external_id).where(
                Job.user_id == _USER,
                Job.is_closed == False,  # noqa: E712
                _render_filter(),
            )).all()}
    assert _PREFIX + "keep" in visible, (
        "STAGE 4 (render window): scored but never displayed — the exact waste "
        "the scoring/render lockstep exists to prevent")
    _cleanup()


def _render_filter():
    """The board's freshness predicate, built the way the API builds it."""
    from app.common.freshness import is_fresh_expr
    return is_fresh_expr(
        settings.shortlist_max_age_days,
        int(getattr(settings, "shortlist_max_posted_age_days", 0) or 0),
        for_render=True,
    )


def test_every_forward_stage_agrees_on_the_same_job():
    """The stages must not merely each work — they must AGREE.

    A job kept by adoption and killed by the age gate (or vice versa) is a
    pipeline that loses work silently. Sweeping the posted-age axis with the
    known age pinned at "found today" shows the three Python-side gates drawing
    the same line.
    """
    from app.strategy.adoption import ADOPT_MAX_AGE_DAYS

    now = datetime.utcnow()
    posted_bound = int(settings.scoring_max_posted_age_days)

    class _J:
        def __init__(self, posted_days):
            self.first_seen = now
            self.discovered_at = now
            self.posted_at = now - timedelta(days=posted_days)

    for posted_days in (0, 1, 5, 12, 20, 25, posted_bound - 1,
                        posted_bound + 1, 60, 400):
        j = _J(posted_days)
        adoption_keeps = is_fresh(j, ADOPT_MAX_AGE_DAYS, posted_bound, now=now)
        gate_keeps = is_fresh(j, settings.scoring_max_job_age_days,
                              posted_bound, now=now)
        render_keeps = is_fresh(j, settings.shortlist_max_age_days,
                                int(settings.shortlist_max_posted_age_days),
                                now=now)
        assert adoption_keeps == gate_keeps == render_keeps, (
            f"found today / posted {posted_days}d ago: adoption={adoption_keeps} "
            f"gate={gate_keeps} render={render_keeps} — the stages disagree, so "
            f"a job is kept by one and discarded by the next")


def test_the_posted_bound_is_what_actually_binds_at_every_stage():
    """Guards the specific regression: adoption's own window silently narrowing
    the posted bound below what the scoring gate and render window allow.

    While ADOPT_MAX_AGE_DAYS < scoring_max_posted_age_days, any stage that
    measures the source date against ITS OWN window instead of the shared
    posted bound reintroduces the gap.
    """
    from app.strategy.adoption import ADOPT_MAX_AGE_DAYS

    now = datetime.utcnow()

    class _J:
        first_seen = now
        discovered_at = now
        posted_at = now - timedelta(days=ADOPT_MAX_AGE_DAYS + 3)

    assert is_fresh(_J(), ADOPT_MAX_AGE_DAYS,
                    int(settings.scoring_max_posted_age_days), now=now), (
        f"a posting found today but dated {ADOPT_MAX_AGE_DAYS + 3}d back is "
        f"being judged against adoption's {ADOPT_MAX_AGE_DAYS}d known-age "
        f"window instead of the {settings.scoring_max_posted_age_days}d posted "
        f"bound")
