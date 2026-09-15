"""Did this work actually help? One aggregate query, no per-event logging.

The brief asked for metrics on newly shortlisted jobs, broken down by source,
and explicitly warned against high-cardinality logging. So nothing here emits a
log line per job. This computes counts on demand, over a bounded window, and
returns them as plain integers — cheap enough to expose on an admin route and
safe to run against production.

It answers, for the jobs that actually reached users:

    out of 100 delivered jobs
      how many have useful team context?
      how many had a requisition ID?
      how many were discovered dead before delivery?
      how many still reached users dead?
      how many context fields came from free ATS data vs from the description?
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

#: Evidence classes that mean the value came from a structured ATS field
#: rather than from parsing prose. The split matters: free ATS data is exact
#: and stable, text extraction is inference over wording.
_STRUCTURED_EVIDENCE = {"STRUCTURED_PERSON", "TEAM_OR_DEPARTMENT", "ORG_ENTITY"}

_COUNTERS = (
    "context_available",
    "department_available",
    "team_available",
    "requisition_id_available",
    "hiring_entity_available",
    "reporting_title_available",
    "named_recruiter_available",
    "named_manager_available",
    "context_from_ats_fields",
    "context_from_description",
)


def snapshot(days: int = 7, user_id: Optional[str] = None) -> dict:
    """Coverage over jobs delivered to a board in the last `days`.

    Delivered means the posting reached a user's shortlist, which is the only
    population the product question is about — the shared pool is mostly jobs
    nobody ever sees.
    """
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import (
        Application, ApplicationStatus, Job, JobHiringContext, JobLiveness,
    )
    from app.discovery.liveness import DEAD_STATES

    cutoff = datetime.utcnow() - timedelta(days=days)
    delivered_states = (
        ApplicationStatus.SHORTLISTED, ApplicationStatus.TAILORED,
        ApplicationStatus.AUTOFILLED, ApplicationStatus.AWAITING_USER,
        ApplicationStatus.READY_TO_SUBMIT, ApplicationStatus.SUBMITTED,
    )

    totals = {k: 0 for k in _COUNTERS}
    totals["delivered"] = 0
    per_source: dict = {}
    liveness_counts: dict = {}
    dead_delivered = 0

    with get_session() as session:
        rows = session.exec(
            select(Job.source, Job.external_id, Job.origin)
            .join(Application, Application.job_id == Job.id)
            .where(Application.created_at >= cutoff,
                   Application.status.in_(delivered_states),
                   *( [Application.user_id == user_id] if user_id else [] ))
        ).all()
        if not rows:
            return {"window_days": days, "delivered": 0, "by_source": {},
                    "liveness": {}, "note": "no delivered jobs in this window"}

        keys = {(s.value if hasattr(s, "value") else str(s), e) for s, e, _o in rows}
        ctx_by_key: dict = {}
        live_by_key: dict = {}
        key_list = sorted(keys)
        for start in range(0, len(key_list), 300):
            chunk = key_list[start:start + 300]
            srcs = [k[0] for k in chunk]
            exts = [k[1] for k in chunk]
            for r in session.exec(
                select(JobHiringContext).where(
                    JobHiringContext.source.in_(srcs),
                    JobHiringContext.external_id.in_(exts))
            ).all():
                ctx_by_key[(r.source, r.external_id)] = r
            for src, ext, state in session.exec(
                select(JobLiveness.source, JobLiveness.external_id, JobLiveness.state)
                .where(JobLiveness.source.in_(srcs),
                       JobLiveness.external_id.in_(exts))
            ).all():
                live_by_key[(src, ext)] = state

    for source, external_id, origin in rows:
        src = source.value if hasattr(source, "value") else str(source)
        key = (src, external_id)
        # Report under the true producer where one is recorded; that is the
        # whole point of the origin column.
        bucket = origin or src
        stats = per_source.setdefault(bucket, {k: 0 for k in _COUNTERS})
        stats["delivered"] = stats.get("delivered", 0) + 1
        totals["delivered"] += 1

        state = live_by_key.get(key)
        liveness_counts[state or "NOT_CHECKED"] = liveness_counts.get(state or "NOT_CHECKED", 0) + 1
        if state in DEAD_STATES:
            dead_delivered += 1

        row = ctx_by_key.get(key)
        if row is None:
            continue
        try:
            evidence = json.loads(row.evidence_json or "{}")
        except (json.JSONDecodeError, TypeError):
            evidence = {}

        present = {
            "department_available": row.department,
            "team_available": row.team,
            "requisition_id_available": row.requisition_id,
            "hiring_entity_available": row.hiring_entity,
            "reporting_title_available": row.reporting_title,
            "named_recruiter_available": row.recruiter_name or row.posting_creator_name,
            "named_manager_available": row.reporting_manager_name,
        }
        any_field = False
        for counter, value in present.items():
            if value:
                any_field = True
                totals[counter] += 1
                stats[counter] += 1
        if any_field:
            totals["context_available"] += 1
            stats["context_available"] += 1
        classes = {(m or {}).get("evidence") for m in evidence.values()}
        if classes & _STRUCTURED_EVIDENCE:
            totals["context_from_ats_fields"] += 1
            stats["context_from_ats_fields"] += 1
        if classes - _STRUCTURED_EVIDENCE - {None}:
            totals["context_from_description"] += 1
            stats["context_from_description"] += 1

    def _per100(n: int) -> float:
        return round(100.0 * n / totals["delivered"], 1) if totals["delivered"] else 0.0

    return {
        "window_days": days,
        "delivered": totals["delivered"],
        "counts": totals,
        "per_100_delivered": {k: _per100(v) for k, v in totals.items() if k != "delivered"},
        "by_source": per_source,
        "liveness": liveness_counts,
        "dead_reached_users": dead_delivered,
        "dead_reached_users_per_100": _per100(dead_delivered),
    }


if __name__ == "__main__":       # pragma: no cover - operator tool
    import sys
    print(json.dumps(snapshot(days=int(sys.argv[1]) if len(sys.argv) > 1 else 7),
                     indent=2, default=str))
