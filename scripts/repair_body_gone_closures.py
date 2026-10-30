"""Reopen live postings that a body-text false positive closed (2026-10-30).

`liveness.classify` read a removal notice into ordinary posting text:
"we accept applications on an ongoing basis until the job is filled"
(Everpure — on every one of its postings) and "based primarily on the job
level at which the role is filled" (Intuitive). Both pages were filed
REMOVED / ``body_no_longer_active``, every copy was closed, and the Everpure
copy on one user's board went to Removed a minute after it was placed. The
patterns are fixed (a gone phrase inside an "until/once/at which …" clause no
longer counts), but nothing re-opens what they already closed: board absence
reopens only its own "Removed from company" closures, and the gate never
re-checks a posting it already holds dead (`liveness.needs_check`).

A posting is a CANDIDATE only when ALL of:
  - its JobLiveness verdict is REMOVED with reason ``body_no_longer_active``,
    the one verdict those patterns wrote;
  - its id is scoped: a bare Workday/BambooHR/Teamtailor id names more than
    one employer's posting (`job_identity.looks_unscoped`);
  - its shared-pool row was closed by the liveness gate (`_closed_by_gate`)
    and its board listed it AFTER the verdict (``last_seen > checked_at``).

Dry run (the default) reads the database only: no request, no write.
``--apply`` asks each candidate's page again through the delivery gate's own
SSRF-guarded check, with the CURRENT classifier. Only a LIVE answer reopens;
anything else — still REMOVED, a 429/403, a timeout — leaves the posting as
it is. For a LIVE posting, the verdict is recorded first (or the next
re-verification pass would close it again from the cache), then in ONE
transaction, job rows locked ascending as `slate.place` locks them:
  - every copy the gate closed reopens; a closure for any other reason (the
    user's own report, board absence, retention) keeps it;
  - an application that closure moved to Removed is Shortlisted again only
    when ALL of: SKIPPED, its notes END with the re-check note, it carries no
    dismissal marker, and a passed placement event proves `slate.place` put
    it on the board. Its note is taken off. Every other application is
    counted and left alone.

Idempotent: a repaired posting is LIVE, so it is no longer a candidate.
Prints postings (source, id, company) and counts; never a user id.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import update
from sqlmodel import select

from app.db.init_db import get_session, init_db
from app.db.models import (
    Application, ApplicationStatus, FunnelEvent, Job, JobLiveness, JobLivenessState,
    JobSource,
)

BODY_REASON = "body_no_longer_active"
#: What the gate writes when it closes on a REMOVED verdict:
#: `close_dead_everywhere` ("… REMOVED, confirmed <how>"), `slate.place`
#: ("… REMOVED before delivery") and the scoring lane's own close.
_GATE_PREFIX = "Deactivated (posting REMOVED"
_GATE_GONE = "Deactivated (posting gone before delivery)"
#: `close_dead_everywhere`'s note, which must be the LAST thing on the notes.
_RECHECK_NOTE = re.compile(
    r"(?:^|\n)Job closed — the posting was REMOVED when re-checked on "
    r"\d{4}-\d{2}-\d{2}\.\s*$")


def _closed_by_gate(reason) -> bool:
    reason = reason or ""
    return reason.startswith(_GATE_PREFIX) or reason == _GATE_GONE


@dataclass
class Candidate:
    source: str
    external_id: str
    company: str
    url: str
    checked_at: datetime
    last_seen: datetime


@dataclass
class Totals:
    candidates: int = 0
    rechecked: int = 0
    still_dead: int = 0
    inconclusive: int = 0
    repaired: int = 0
    jobs_reopened: int = 0
    applications_restored: int = 0
    applications_left: int = 0
    states: dict = field(default_factory=dict)


def candidates() -> list:
    """Postings matching every candidate rule. Database only."""
    from app.discovery.job_identity import looks_unscoped
    from app.discovery.pipeline import SHARED_POOL_USER

    out = []
    with get_session() as s:
        verdicts = s.exec(select(JobLiveness.source, JobLiveness.external_id,
                                 JobLiveness.checked_at).where(
            JobLiveness.state == JobLivenessState.REMOVED.value,
            JobLiveness.reason == BODY_REASON,
        ).order_by(JobLiveness.source, JobLiveness.external_id)).all()
        for src, ext, checked_at in verdicts:
            if looks_unscoped(src, ext) or checked_at is None:
                continue
            try:
                source = JobSource(src)
            except ValueError:
                continue
            shared = s.exec(select(Job.company, Job.url, Job.last_seen, Job.closed_reason).where(
                Job.user_id == SHARED_POOL_USER, Job.source == source,
                Job.external_id == ext, Job.is_closed == True,  # noqa: E712
            )).first()
            if shared is None or not _closed_by_gate(shared[3]):
                continue
            if not shared[2] or shared[2] <= checked_at or not shared[1]:
                continue
            out.append(Candidate(src, ext, shared[0] or "", shared[1], checked_at, shared[2]))
    return out


def _gate_closed_ids(s, c: Candidate) -> list:
    """Every copy of this posting the liveness gate closed, ascending."""
    rows = s.exec(select(Job.id, Job.closed_reason).where(
        Job.source == JobSource(c.source), Job.external_id == c.external_id,
        Job.is_closed == True,  # noqa: E712
    )).all()
    return sorted(r[0] for r in rows if _closed_by_gate(r[1]))


def _applications(s, ids: list) -> tuple:
    """(applications to restore, other Removed applications left alone)."""
    from app.matching.preference_learning import USER_DISMISS_MARKER
    from app.strategy.slate import PLACEMENT_STAGE

    restore, left = [], 0
    if not ids:
        return restore, left
    placed = set(s.exec(select(FunnelEvent.job_id).where(
        FunnelEvent.job_id.in_(ids), FunnelEvent.stage == PLACEMENT_STAGE,
        FunnelEvent.passed == True,  # noqa: E712
    )).all())
    for app in s.exec(select(Application).where(Application.job_id.in_(ids))
                      .order_by(Application.id)).all():
        notes = app.notes or ""
        if (app.status == ApplicationStatus.SKIPPED and _RECHECK_NOTE.search(notes)
                and USER_DISMISS_MARKER not in notes and app.job_id in placed):
            restore.append(app)
        elif app.status == ApplicationStatus.SKIPPED:
            left += 1
    return restore, left


def _reopen(c: Candidate) -> tuple:
    """One transaction: reopen the gate's closures, restore what it removed.
    Job rows are locked first, and applications are read only under them."""
    from app.discovery.board_absence import lock_statements

    now = datetime.utcnow()
    with get_session() as s:
        locked = set(_gate_closed_ids(s, c))
        for stmt in lock_statements(sorted(locked)):
            s.exec(stmt).all()
        ids = [i for i in _gate_closed_ids(s, c) if i in locked]
        restore, left = _applications(s, ids)
        reopened = 0
        for start in range(0, len(ids), 500):
            res = s.execute(update(Job).where(
                Job.id.in_(ids[start:start + 500]), Job.is_closed == True,  # noqa: E712
            ).values(is_closed=False, closed_reason=None))
            reopened += int(res.rowcount or 0)
        for app in restore:
            app.status = ApplicationStatus.SHORTLISTED
            app.notes = _RECHECK_NOTE.sub("", app.notes or "").strip() or None
            app.updated_at = now
            s.add(app)
        s.commit()
    return reopened, len(restore), left


def repair(apply: bool, *, check=None) -> Totals:
    """``check(url, external_id) -> (state, reason, http_status)``; the delivery
    gate's own `_check` unless a test passes one."""
    from app.discovery import liveness as lv

    found = candidates()
    t = Totals(candidates=len(found))
    print(f"postings closed on a body-text REMOVED that their board still lists: {len(found)}")
    for c in found:
        with get_session() as s:
            ids = _gate_closed_ids(s, c)
            restore, left = _applications(s, ids)
        print(f"  {c.source}:{c.external_id} ({c.company}) — verdict {c.checked_at:%Y-%m-%d %H:%M}, "
              f"listed {c.last_seen:%Y-%m-%d %H:%M}; {len(ids)} closed copies, "
              f"{len(restore)} application(s) to restore, {left} other Removed left alone")
    if not found or not apply:
        if found:
            print("dry run — pass --apply to re-check each page and reopen the LIVE ones")
        return t
    if check is None:
        from app.strategy.delivery_gate import _check as check
    for c in found:
        state, reason, status = check(c.url, c.external_id)
        t.rechecked += 1
        t.states[state] = t.states.get(state, 0) + 1
        if state != JobLivenessState.LIVE.value:
            if lv.is_dead(state):
                t.still_dead += 1
            else:
                t.inconclusive += 1
            print(f"  {c.source}:{c.external_id}: re-check says {state} ({reason}) — left closed")
            continue
        lv.record(c.source, c.external_id, state, reason=reason,
                  http_status=status, checked_url=c.url)
        reopened, restored, left = _reopen(c)
        t.repaired += 1
        t.jobs_reopened += reopened
        t.applications_restored += restored
        t.applications_left += left
        print(f"  {c.source}:{c.external_id}: LIVE — reopened {reopened} copies, "
              f"restored {restored} application(s)")
    print(f"done: {t.repaired} reopened, {t.still_dead} still dead, "
          f"{t.inconclusive} inconclusive (left closed)")
    return t


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="re-check each candidate page and reopen the LIVE ones (default: dry run)")
    args = ap.parse_args()
    init_db()
    repair(apply=args.apply)
