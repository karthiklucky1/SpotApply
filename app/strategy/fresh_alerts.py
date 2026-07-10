"""Instant fresh-job alerts — the freshness wedge.

After each matching pass, alert the user about newly shortlisted jobs that are
young enough that applying now means being among the first applicants. Alerts
land on the dashboard bell (UserNotification) for every tenant, plus Telegram
when a bot is configured (single-user/local mode).

Honesty guards:
  - Greenhouse's public list only exposes ``updated_at`` (moves on every edit),
    so before alerting on a Greenhouse job we fetch the posting's true
    ``first_published`` from the public single-job endpoint and correct
    ``Job.posted_at`` — no false "just posted" alerts for edited old posts.
  - Every alert is deduped per (user, job) via the notification link.
  - Detection latency (posted → alerted) is recorded as a FunnelEvent so the
    median post-to-alert latency we advertise is measured, not claimed.
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


def _send_telegram(text: str) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    try:
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.debug("fresh-alert telegram send failed: %s", e)


def dispatch_fresh_alerts(user_id: Optional[str], shortlisted_job_ids: List[int]) -> int:
    """Alert on shortlisted jobs posted within the last day. Returns alerts sent."""
    if not shortlisted_job_ids:
        return 0
    uid = user_id if user_id and user_id != "local" else None
    notif_user = user_id or "local"
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=FRESH_ALERT_MAX_AGE_HOURS)
    sent = 0

    with get_session() as session:
        jobs = session.exec(
            select(Job).where(Job.id.in_(shortlisted_job_ids), Job.user_id == uid)  # noqa: E711
        ).all()

        for job in sorted(jobs, key=lambda j: (j.blended_score or j.rerank_score or 0),
                          reverse=True):
            if sent >= MAX_ALERTS_PER_PASS:
                break
            posted = _utc_naive(job.posted_at) or job.first_seen
            if not posted or posted < cutoff:
                continue

            # Greenhouse honesty check: updated_at masquerades as posted_at.
            if job.source == JobSource.GREENHOUSE:
                true_posted = _verify_greenhouse_first_published(job)
                if true_posted:
                    if job.posted_at != true_posted:
                        job.posted_at = true_posted
                        session.add(job)
                    posted = true_posted
                    if posted < cutoff:
                        session.commit()
                        continue  # edited old post — not fresh, don't alert

            link = f"/dashboard?fresh_job={job.id}"
            dup = session.exec(
                select(UserNotification).where(
                    UserNotification.user_id == notif_user,
                    UserNotification.link == link,
                )
            ).first()
            if dup:
                continue

            age_min = max(1, int((now - posted).total_seconds() // 60))
            age_txt = f"{age_min}m" if age_min < 60 else f"{age_min // 60}h"
            fit = int(job.rerank_score or 0)
            msg = (f"{job.title} @ {job.company} — posted {age_txt} ago, fit {fit}. "
                   f"Early applicants win: tailor and apply now.")
            session.add(UserNotification(
                user_id=notif_user,
                title="⚡ Fresh match — be one of the first to apply",
                message=msg[:1000],
                type="fresh_job",
                link=link,
            ))
            session.add(FunnelEvent(
                job_id=job.id, stage="fresh_alert", passed=True,
                reason=f"latency_min={age_min}",
                metadata_json=json.dumps({"latency_min": age_min, "fit": fit,
                                          "source": job.source.value}),
            ))
            sent += 1
            _send_telegram("⚡ Fresh match\n" + msg)

        session.commit()
    if sent:
        log.info("Fresh alerts: %d sent for user %s", sent, user_id)
    return sent
