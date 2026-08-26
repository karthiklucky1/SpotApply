"""Instant fresh-job alerts — the freshness wedge.

After each matching pass, alert the user about newly shortlisted jobs that are
young enough that applying now means being among the first applicants. Alerts
land on the dashboard bell (UserNotification) for every tenant.

TWO BOUNDS, like every other freshness question in the codebase
(``app/common/freshness.py`` — read it before touching the gate below):

  * KNOWN age — how long WE have held the posting. TIGHT
    (``FRESH_ALERT_MAX_AGE_HOURS`` = 24). This is the alert's actual trigger:
    the thing worth pushing is that a strong match became available to this
    user today.
  * POSTED age — what the source claims. LOOSE
    (``settings.fresh_alert_max_posted_age_days``). It exists only to suppress
    evergreen and genuinely ancient reqs.

This gate used to be the single-bound expression that module exists to
eliminate::

    posted = job.posted_at or job.first_seen
    if posted < now - 24h: continue

``posted_at`` won, so a posting discovered ten minutes ago whose ATS dated it
three days back could never alert — and ATS dates are exactly the thing that
cannot carry that weight (Greenhouse's ``updated_at`` moves on any edit,
aggregators stamp their crawl date, evergreen reqs are re-dated). The measured
median detection lag was ~91.5h, so in practice almost every shortlist looked
stale through it: production recorded 2 shortlists and 0 fresh alerts. It also
failed the other way, alerting "posted 1h ago" on a posting we had held unscored
for two days purely because the source said so.

Honesty guards:
  - We only make the strong claim — "posted Xh ago", "be one of the first" —
    when the SOURCE date is itself inside the window. Otherwise the alert says
    what we can actually stand behind: we found it Xh ago, and here is what the
    source claims. An alert is never sold on a date we do not trust.
  - Greenhouse's public list only exposes ``updated_at`` (moves on every edit),
    so before alerting on a Greenhouse job we fetch the posting's true
    ``first_published`` from the public single-job endpoint and correct
    ``Job.posted_at`` — no false "just posted" alerts for edited old posts.
  - Every alert is deduped per (user, job) via the notification link.
  - Both clocks are recorded on the FunnelEvent (``latency_min`` against the
    reference we alerted on, ``known_latency_min`` against discovery, and
    ``posted_trusted``) so the median post-to-alert latency we advertise stays
    a POST-to-alert number rather than quietly becoming a detection one.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import FunnelEvent, Job, JobSource, UserNotification

log = logging.getLogger(__name__)

FRESH_ALERT_MAX_AGE_HOURS = 24
MAX_ALERTS_PER_PASS = 5


def _utc_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _ago(minutes: int) -> str:
    """'12m' / '4h' / '3d' — the alert copy reads better than raw minutes once a
    posting is more than a day old, which the loose posted bound now allows."""
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def _verify_greenhouse_first_published(job: Job) -> Optional[datetime]:
    """True publish time from Greenhouse's public single-job endpoint, or None."""
    try:
        import httpx
        board = None
        # absolute_url style: https://boards.greenhouse.io/{board}/jobs/{id}
        for part in (job.url or "").split("/"):
            if part and part not in ("https:", "http:", "", "boards.greenhouse.io",
                                     "job-boards.greenhouse.io", "jobs"):
                board = part
                break
        if not board:
            return None
        r = httpx.get(
            f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job.external_id}",
            timeout=8,
        )
        if r.status_code != 200:
            return None
        first = (r.json() or {}).get("first_published")
        if not first:
            return None
        return _utc_naive(datetime.fromisoformat(str(first).replace("Z", "+00:00")))
    except Exception as e:
        log.debug("greenhouse first_published check skipped: %s", e)
        return None


def dispatch_fresh_alerts(user_id: Optional[str], shortlisted_job_ids: List[int]) -> int:
    """Alert on shortlisted jobs posted within the last day. Returns alerts sent."""
    if not shortlisted_job_ids:
        return 0
    uid = user_id if user_id and user_id != "local" else None
    notif_user = user_id or "local"
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=FRESH_ALERT_MAX_AGE_HOURS)
    # 0 disables the loose bound entirely (consistent with the other *_max_*_days
    # knobs) — NOT "cut off at now", which would reject every dated posting.
    _posted_days = int(getattr(settings, "fresh_alert_max_posted_age_days", 30) or 0)
    posted_cutoff = now - timedelta(days=_posted_days) if _posted_days > 0 else None
    sent = 0

    with get_session() as session:
        jobs = session.exec(
            select(Job).where(Job.id.in_(shortlisted_job_ids), Job.user_id == uid)  # noqa: E711
        ).all()

        # Daily cap: lanes call this every few minutes, so the per-pass cap
        # alone still allows dozens of pushes/day — count today's fresh alerts
        # once and shrink the pass budget accordingly.
        pass_budget = MAX_ALERTS_PER_PASS
        if settings.fresh_alert_daily_cap > 0:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            sent_today = len(session.exec(
                select(UserNotification.id).where(
                    UserNotification.user_id == notif_user,
                    UserNotification.type == "fresh_job",
                    UserNotification.created_at >= day_start,
                )
            ).all())
            pass_budget = min(pass_budget,
                              max(0, settings.fresh_alert_daily_cap - sent_today))

        for job in sorted(jobs, key=lambda j: (j.blended_score or j.rerank_score or 0),
                          reverse=True):
            if sent >= pass_budget:
                break
            # Alert only on strong fits: shortlisting keeps its lower bar, but an
            # "apply now" push for a job the model scored 35 trains users to
            # ignore notifications (and the reasoning may even call it a misfit).
            fit = int(job.rerank_score or 0)
            if max(fit, int(job.blended_score or 0)) < settings.fresh_alert_min_score:
                continue

            # BOUND 1 (tight, and the actual trigger): we found it today.
            known = _utc_naive(job.first_seen) or _utc_naive(job.discovered_at)
            if not known or known < cutoff:
                continue
            # BOUND 2 (loose): suppress evergreen/ancient reqs, nothing more.
            posted = _utc_naive(job.posted_at)
            if posted is not None and posted_cutoff is not None and posted < posted_cutoff:
                continue

            # Greenhouse honesty check: updated_at masquerades as posted_at.
            if job.source == JobSource.GREENHOUSE:
                true_posted = _verify_greenhouse_first_published(job)
                if true_posted:
                    if job.posted_at != true_posted:
                        job.posted_at = true_posted
                        session.add(job)
                    posted = true_posted
                    if posted_cutoff is not None and posted < posted_cutoff:
                        session.commit()
                        continue  # edited old post, genuinely ancient — don't alert

            link = f"/dashboard?fresh_job={job.id}"
            dup = session.exec(
                select(UserNotification).where(
                    UserNotification.user_id == notif_user,
                    UserNotification.link == link,
                )
            ).first()
            if dup:
                continue

            # Only claim a POSTING date when the source's own date is inside the
            # window; otherwise the honest thing to report is when WE found it.
            posted_trusted = posted is not None and posted >= cutoff
            known_min = max(1, int((now - known).total_seconds() // 60))
            ref_min = max(1, int((now - posted).total_seconds() // 60)) \
                if posted_trusted else known_min

            if posted_trusted:
                title = "⚡ Fresh match — be one of the first to apply"
                msg = (f"{job.title} @ {job.company} — posted {_ago(ref_min)} ago, "
                       f"fit {fit}. Early applicants win: tailor and apply now.")
            else:
                # No trustworthy posting date. Say what we know, and attribute
                # the source's claim to the source instead of asserting it.
                claim = ""
                if posted is not None:
                    claim = (" Source lists it as posted "
                             f"{_ago(max(1, int((now - posted).total_seconds() // 60)))} ago.")
                title = "⚡ New match found for you"
                msg = (f"{job.title} @ {job.company} — found {_ago(known_min)} ago, "
                       f"fit {fit}.{claim} Tailor and apply now.")

            session.add(UserNotification(
                user_id=notif_user,
                title=title,
                message=msg[:1000],
                type="fresh_job",
                link=link,
            ))
            session.add(FunnelEvent(
                job_id=job.id, stage="fresh_alert", passed=True,
                reason=f"latency_min={ref_min}",
                metadata_json=json.dumps({"latency_min": ref_min,
                                          "known_latency_min": known_min,
                                          "posted_trusted": posted_trusted,
                                          "fit": fit,
                                          "source": job.source.value}),
            ))
            sent += 1

        session.commit()
    if sent:
        log.info("Fresh alerts: %d sent for user %s", sent, user_id)
    return sent
