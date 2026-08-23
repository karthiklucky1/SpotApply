"""Two-concept job freshness, and the lifecycle sentinels — one home.

Until Aug 2026 every freshness question in the codebase was answered with the
same expression::

    coalesce(posted_at, first_seen, discovered_at) < now - 5 days

which conflates two things that are not the same and do not fail the same way:

  * **posting age** — what the SOURCE says about when the role went live.
  * **known age** — how long SpotApply itself has had the posting in hand.

``posted_at`` wins that coalesce, so a job DISCOVERED TODAY was declared
terminally stale the moment an ATS said it went up six days ago. Production
measured the damage: 82.9% of the ``rerank_score = 8.0`` expiry stamps were
already >= 5 days old at first discovery, median detection lag was ~91.5h, and
36.7% of intake was >7d old the first time we ever saw it. The gate was
expiring most of the funnel before the finals budget could reach it, which is
why 11 of 13 users showed ZERO unscored jobs and vanished from
``_scorable_user_ids`` entirely.

ATS ``posted_at`` values are not trustworthy enough to carry that weight:
Greenhouse's ``updated_at`` moves on any edit, aggregators frequently stamp
their own crawl date, evergreen reqs are re-dated on a rolling basis, and some
feeds emit future dates. ``first_seen - posted_at`` is therefore NOT crawler
latency — it is mostly source noise.

So the two ages get two separate, differently-sized bounds:

    stale  <=>  known_age > known_days  OR  posting_age > posted_days

with ``known_days`` tight (the product promise: be first to apply, so a posting
that sat unscored in the queue for days is past its moment) and ``posted_days``
deliberately loose (it exists ONLY to suppress genuinely ancient and evergreen
listings, not to second-guess a fresh discovery).

Worked through the cases that matter:

  ==================================  ==========  ===========  ========
  case                                known_age   posting_age  verdict
  ==================================  ==========  ===========  ========
  found today, source says 6d old            0d           6d   FRESH
  found today, no posted_at                  0d           0d   FRESH
  found today, future-dated posted_at        0d      negative  FRESH
  sat unscored in our queue 6d               6d           6d   stale
  found today, evergreen req from 2024       0d         400d   stale
  ==================================  ==========  ===========  ========

Row 1 is the bug this module exists to fix. Row 5 is the behaviour it must not
lose.

LOCKSTEP: the scoring gate must never reach further back than the render
window on EITHER axis, or we pay for finals the board then hides. Both windows
are built from this module and asserted in tests/test_settings_defaults.py.

The ``known`` reference is ``coalesce(first_seen, discovered_at)``:
``first_seen`` reached production via a bare ALTER TABLE with no backfill, so
it is NULL on the oldest rows and ``NULL < cutoff`` is NULL (never true), which
would silently exempt exactly those rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from app.db.models import Job

# ── Lifecycle sentinels ──────────────────────────────────────────────────────
#
# ``rerank_score`` is overloaded: it carries real 0-100 verdicts AND two
# "this job left the queue without a verdict" stamps. Nothing about the number
# says which, so an expired job reads as "scored" in every count that only
# checks ``rerank_score IS NOT NULL`` — production's "621k scored jobs" was
# mostly these stamps rather than real verdicts.
#
# The stamps stay (feed/ranking logic reads the number, and a rewrite of that
# is not this patch), but they are no longer the only record: ``Job.expired_at``
# and ``Job.scored_at`` say what actually happened, and every scoring metric
# reads the predicates below instead of ``rerank_score IS NOT NULL``.
EXPIRY_SENTINEL_SCORE = 8.0   # left the queue on age, never scored
GHOST_SENTINEL_SCORE = 5.0    # left the queue as a ghost/fake posting
SENTINEL_SCORES = (EXPIRY_SENTINEL_SCORE, GHOST_SENTINEL_SCORE)


def known_ref():
    """SQL: when SpotApply first had this posting in this pool."""
    return func.coalesce(Job.first_seen, Job.discovered_at)


def posting_ref():
    """SQL: the source's claim about when the role went live, with fallbacks.

    Identical to the expression behind ``ix_job_user_fresh`` when
    ``include_discovered`` is not needed, so render paths keep their index.
    """
    return func.coalesce(Job.posted_at, Job.first_seen, Job.discovered_at)


def render_posting_ref():
    """SQL: the posting reference EXACTLY as ``ix_job_user_fresh`` indexes it.

    ``(user_id, is_closed, (COALESCE(posted_at, first_seen)) DESC, id DESC)``.
    Adding a third coalesce arm here would make the predicate a different
    expression from the index and cost the Job Explorer its ordered index scan,
    so the render path uses this two-arm form and the age gates use
    ``posting_ref()``.
    """
    return func.coalesce(Job.posted_at, Job.first_seen)


def is_fresh_expr(known_days: int, posted_days: int,
                  now: Optional[datetime] = None, *, for_render: bool = False):
    """SQL predicate: this job is still within BOTH freshness bounds.

    ``known_days <= 0`` or ``posted_days <= 0`` disables that leg.
    """
    now = now or datetime.utcnow()
    post_ref = render_posting_ref() if for_render else posting_ref()
    clauses = []
    if known_days and known_days > 0:
        clauses.append(known_ref() >= now - timedelta(days=known_days))
    if posted_days and posted_days > 0:
        clauses.append(post_ref >= now - timedelta(days=posted_days))
    if not clauses:
        return None
    out = clauses[0]
    for c in clauses[1:]:
        out = out & c
    return out


def is_fresh(job, known_days: int, posted_days: int,
             now: Optional[datetime] = None) -> bool:
    """Python mirror of :func:`is_fresh_expr` — same rule, for loaded rows."""
    now = now or datetime.utcnow()

    def _naive(dt):
        if dt is None:
            return None
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    first_seen = _naive(getattr(job, "first_seen", None))
    discovered = _naive(getattr(job, "discovered_at", None))
    posted = _naive(getattr(job, "posted_at", None))

    known = first_seen or discovered
    if known_days and known_days > 0:
        if known is None or known < now - timedelta(days=known_days):
            return False
    if posted_days and posted_days > 0:
        post = posted or first_seen or discovered
        if post is not None and post < now - timedelta(days=posted_days):
            return False
    return True


# ── Scoring-metric predicates ────────────────────────────────────────────────

def genuinely_scored_expr():
    """SQL: rows that carry a REAL scoring verdict.

    ``rerank_score IS NOT NULL`` is not that question — it also matches expiry
    and ghost stamps, which is how production's "621k scored jobs" came to be
    mostly age stamps.

    Two regimes, and the order matters. ``scored_at`` is written only by paths
    that actually called a scorer, so where it exists it is authoritative — a
    genuine Claude final that happens to land on 8.0 still counts. Only on
    LEGACY rows (written before the column shipped, so ``scored_at`` is NULL)
    do we fall back to excluding the sentinel values, which is the best that
    can be said about them.
    """
    return Job.expired_at.is_(None) & (
        Job.scored_at.is_not(None)
        | (Job.rerank_score.is_not(None)
           & Job.rerank_score.notin_(SENTINEL_SCORES))
    )


def expired_without_scoring_expr():
    """SQL: rows that left the queue on age, never scored.

    Same two regimes: ``expired_at`` is authoritative, and the bare sentinel
    only counts on legacy rows that have no ``scored_at`` to contradict it.
    """
    return Job.expired_at.is_not(None) | (
        (Job.rerank_score == EXPIRY_SENTINEL_SCORE) & Job.scored_at.is_(None))


def is_expiry_stamp(job) -> bool:
    """Python mirror: did this row leave the queue on age rather than a verdict?"""
    if getattr(job, "expired_at", None) is not None:
        return True
    if getattr(job, "scored_at", None) is not None:
        return False
    return getattr(job, "rerank_score", None) == EXPIRY_SENTINEL_SCORE
