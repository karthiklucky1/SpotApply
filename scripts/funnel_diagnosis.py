"""Why did N discovered jobs become M shortlists and K alerts — read-only.

The Aug 2026 delivery investigation had production counts for the ENDS of the
funnel (8,065 pool entries -> 2 shortlists -> 0 alerts) and nothing for the
middle. It could say the Tier-1 drain rate was 90.3% but not what was being
drained, and could not tell a matching problem from a budget problem from a
delivery problem. This report answers those from what the funnel already
records, so the next question is settled with a query instead of a guess.

    python -m scripts.funnel_diagnosis                 # last 24h
    python -m scripts.funnel_diagnosis --days 7
    python -m scripts.funnel_diagnosis --user <uuid>
    python -m scripts.funnel_diagnosis --sample 25     # print reject examples

STRICTLY READ-ONLY: every statement is a SELECT. Safe against production.

WHERE THE REASONS COME FROM. A job that leaves the queue without a Tier-2 final
still records WHY, in ``Job.rerank_reasoning``, written by
``scoring_lane._stamp_job``:

    RuleFilter rejection  ->  "<Gate> pre-filtered: <specifics>"
                              (reranker._pre_filter_job, no LLM call at all)
    ghost/fake posting    ->  "Ghost filtered (score=...): <flags>"
    Tier-1 model drain    ->  "Pre-screened (Tier-1 fit N): <model's reason>"

So the histogram below is built from RECORDED reasons, not inferred from
scores. The rule gates are structured and classify exactly; Tier-1 model
drains carry free text, so they are bucketed by score band and the reason text
is available with --sample.

The tier split uses app/common/freshness.py's predicates rather than
``rerank_score IS NOT NULL``, which also matches expiry and ghost sentinels —
the trap that made production report "621k scored jobs" that were mostly age
stamps.
"""
from __future__ import annotations

import argparse
import re
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import select

from app.common.freshness import (
    EXPIRY_SENTINEL_SCORE, GHOST_SENTINEL_SCORE,
    expired_without_scoring_expr, terminal_verdict_expr, tier1_drain_expr,
)
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, UserNotification
from app.discovery.pipeline import SHARED_POOL_USER

# Ordered: first match wins, so the specific gates are tried before the
# catch-alls. Every pattern here is a literal string this codebase writes —
# see app/matching/filters/rule_filter.py and scoring_lane._stamp_job.
_REASON_RULES = [
    ("ghost / fake posting",     re.compile(r"^Ghost filtered", re.I)),
    ("work auth / sponsorship",  re.compile(r"Sponsorship pre-filtered", re.I)),
    ("experience (years)",       re.compile(r"Experience pre-filtered", re.I)),
    ("seniority (title)",        re.compile(r"Title pre-filtered", re.I)),
    ("salary out of band",       re.compile(r"Salary too (high|low)", re.I)),
    ("internship / job type",    re.compile(r"(Internship|Full-time) filtered", re.I)),
    ("location / remote",        re.compile(r"(Location|Remote|Country)\s", re.I)),
    ("stack mismatch (C++/Rust)", re.compile(r"Hire-probability: C\+\+/Rust", re.I)),
    ("stack mismatch (GPU/systems)", re.compile(r"Hire-probability: GPU", re.I)),
    ("research / PhD required",  re.compile(r"Hire-probability: pure research", re.I)),
    ("other hire-probability",   re.compile(r"^Hire-probability", re.I)),
    ("embedding similarity",     re.compile(r"Embedding similarity", re.I)),
    ("degraded / local scorer",  re.compile(r"local (estimate|scoring)|distilled scorer", re.I)),
]
_TIER1 = re.compile(r"^Pre-screened \(Tier-1 fit (\d+)\)", re.I)


def _classify(reasoning: str | None, is_drain: bool) -> tuple[str, int | None]:
    """(bucket, tier1_score) for one recorded reason.

    ORDER MATTERS, and getting it wrong hides the whole taxonomy. A RuleFilter
    rejection does not bypass Tier-1 — ``Reranker.prescore`` calls
    ``_pre_filter_job`` first and RETURNS its verdict as the prescore, so
    ``_score_one`` stamps it through the same path and the stored text reads
    "Pre-screened (Tier-1 fit 10): Sponsorship pre-filtered: ...". Matching the
    Tier-1 prefix first would therefore file every structured gate rejection
    under "model drain" and report a single opaque bucket. So: strip the
    prefix, classify what is underneath, and only call it a model drain when
    nothing underneath matches a gate.
    """
    text = (reasoning or "").strip()
    if not text:
        return ("tier-1 drain, reason not recorded" if is_drain
                else "final scored, no reason recorded"), None
    tier1_fit = None
    m = _TIER1.match(text)
    if m:
        tier1_fit = int(m.group(1))
        text = text[m.end():].lstrip(": ").strip()
    for label, rx in _REASON_RULES:
        if rx.search(text):
            return label, tier1_fit
    if tier1_fit is not None:
        return "tier-1 model drain (no hard rule)", tier1_fit
    return ("tier-1 drain, unclassified" if is_drain
            else "tier-2 final (scored, not rejected)"), None


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    - "


def _dist(name: str, xs: list[float]) -> None:
    if not xs:
        print(f"  {name}: no rows")
        return
    xs = sorted(xs)

    def p(q):
        k = (len(xs) - 1) * q
        lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

    print(f"  {name}  n={len(xs)}")
    print(f"    min {xs[0]:5.1f} | p25 {p(.25):5.1f} | med {p(.5):5.1f} | "
          f"p75 {p(.75):5.1f} | p90 {p(.9):5.1f} | p95 {p(.95):5.1f} | "
          f"max {xs[-1]:5.1f} | mean {statistics.mean(xs):5.1f}")


def _buckets(scores: list[float], edges: list[int]) -> None:
    total = len(scores)
    if not total:
        return
    prev = -1e9
    for e in edges:
        n = sum(1 for s in scores if prev <= s < e)
        lo = "   <" if prev < -1e8 else f"{prev:>3.0f}-"
        print(f"    {lo}{e:<4} {n:>7}  {_pct(n, total)}")
        prev = e
    n = sum(1 for s in scores if s >= prev)
    print(f"    {prev:>3.0f}+     {n:>7}  {_pct(n, total)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--sample", type=int, default=0,
                    help="print this many example rejects per bucket")
    args = ap.parse_args()

    now = datetime.utcnow()
    since = now - timedelta(days=args.days)
    print(f"=== SpotApply funnel diagnosis — {args.days}d window "
          f"(since {since:%Y-%m-%d %H:%M} UTC) ===")
    if args.user:
        print(f"    user filter: {args.user}")
    print(f"    thresholds: shortlist>={settings.shortlist_score_threshold} "
          f"tier1_gate>={min(settings.prescore_advance_threshold, settings.shortlist_score_threshold)} "
          f"alert_min={settings.fresh_alert_min_score} "
          f"daily_shortlist_limit={settings.daily_shortlist_limit}")

    def scope(q):
        q = q.where(Job.user_id != SHARED_POOL_USER)
        return q.where(Job.user_id == args.user) if args.user else q

    with get_session() as s:
        # ── Stage 1-3: intake and what left the queue without a verdict ──────
        pool_new = s.exec(scope(select(func.count()).select_from(Job)
                                .where(Job.first_seen >= since))).one()
        expired = s.exec(scope(select(func.count()).select_from(Job)
                               .where(Job.first_seen >= since,
                                      expired_without_scoring_expr()))).one()
        pending = s.exec(scope(select(func.count()).select_from(Job)
                               .where(Job.first_seen >= since,
                                      Job.rerank_score.is_(None),
                                      Job.is_closed == False))).one()  # noqa: E712
        terminal = s.exec(scope(select(func.count()).select_from(Job)
                                .where(Job.first_seen >= since,
                                       terminal_verdict_expr()))).one()
        drained = s.exec(scope(select(func.count()).select_from(Job)
                               .where(Job.first_seen >= since,
                                      tier1_drain_expr()))).one()
        # NOT `terminal - drained`. A ghost stamp is a terminal verdict that
        # never ran Tier-1 (no prescore, so tier1_drain_expr misses it) and
        # never ran Tier-2 either — subtracting only the drains would report
        # every ghost as a Claude final. This is the same trap
        # terminal_verdict_expr's docstring describes one level up.
        _final_pred = (terminal_verdict_expr() & ~tier1_drain_expr()
                       & Job.rerank_score.notin_([EXPIRY_SENTINEL_SCORE,
                                                  GHOST_SENTINEL_SCORE]))
        finals = s.exec(scope(select(func.count()).select_from(Job)
                              .where(Job.first_seen >= since, _final_pred))).one()
        ghosts = terminal - drained - finals

        print("\n── Funnel ─────────────────────────────────────────────────────")
        print(f"  entered user pools      {pool_new:>8}")
        print(f"  expired on age          {expired:>8}  {_pct(expired, pool_new)} of intake")
        print(f"  still queued            {pending:>8}  {_pct(pending, pool_new)}")
        print(f"  terminal verdicts       {terminal:>8}  {_pct(terminal, pool_new)}")
        print(f"    of which Tier-1 drain {drained:>8}  {_pct(drained, terminal)} of verdicts")
        print(f"    of which ghost stamp  {ghosts:>8}  {_pct(ghosts, terminal)} of verdicts")
        print(f"    of which Tier-2 final {finals:>8}  {_pct(finals, terminal)} of verdicts")

        # ── P1A: the rejection-reason histogram, from RECORDED reasons ───────
        rows = s.exec(scope(
            select(Job.id, Job.rerank_reasoning, Job.rerank_score, Job.prescore,
                   Job.title, Job.company)
            .where(Job.first_seen >= since, terminal_verdict_expr())
            .limit(200_000))).all()

        hist: dict[str, int] = {}
        tier1_scores: list[float] = []
        examples: dict[str, list] = {}
        for jid, reasoning, score, pre, title, company in rows:
            # A Tier-1 drain stamps the prescore AS the final score — that is
            # the convention _stamp_job documents and tier1_drain_expr reads.
            is_drain = pre is not None and score is not None and score == pre
            bucket, t1 = _classify(reasoning, is_drain)
            hist[bucket] = hist.get(bucket, 0) + 1
            if t1 is not None and is_drain:
                tier1_scores.append(float(t1))
            if args.sample and len(examples.setdefault(bucket, [])) < args.sample:
                examples[bucket].append((jid, title, company, score,
                                         (reasoning or "")[:160]))

        print("\n── P1A: why jobs left the queue (recorded reasons) ────────────")
        tot = sum(hist.values())
        for label, n in sorted(hist.items(), key=lambda kv: -kv[1]):
            print(f"  {label:<34} {n:>8}  {_pct(n, tot)}")
        if not tot:
            print("  (no terminal verdicts in this window)")

        if tier1_scores:
            print("\n  Tier-1 drain score distribution (the model's own fit number):")
            _dist("tier-1 fit", tier1_scores)
            print("  bands:")
            _buckets(tier1_scores, [10, 20, 30, 40])
            near = sum(1 for x in tier1_scores
                       if x >= settings.prescore_advance_threshold - 10)
            print(f"  within 10 points of the advance gate: {near} "
                  f"({_pct(near, len(tier1_scores))}) "
                  "— a large share here means the gate, not the inventory, is binding")

        # ── P1C: Tier-2 score distribution and the shortlist bar ─────────────
        f_rows = s.exec(scope(
            select(Job.rerank_score)
            .where(Job.first_seen >= since, terminal_verdict_expr(),
                   ~tier1_drain_expr(),
                   Job.rerank_score.notin_([EXPIRY_SENTINEL_SCORE,
                                            GHOST_SENTINEL_SCORE]))
            .limit(200_000))).all()
        f_scores = [float(r[0] if isinstance(r, tuple) else r)
                    for r in f_rows if (r[0] if isinstance(r, tuple) else r) is not None]

        print("\n── P1C: Tier-2 final score distribution ───────────────────────")
        _dist("final score", f_scores)
        if f_scores:
            print("  buckets:")
            _buckets(f_scores, [50, 60, 65, 70, 75, 80])
            bar = settings.shortlist_score_threshold
            just_under = sum(1 for x in f_scores if bar - 10 <= x < bar)
            over = sum(1 for x in f_scores if x >= bar)
            print(f"  at/over shortlist bar ({bar}): {over} ({_pct(over, len(f_scores))})")
            print(f"  within 10 under the bar      : {just_under} "
                  f"({_pct(just_under, len(f_scores))})")
            print("  -> clustering just under the bar means the threshold is binding;")
            print("     a mass well below it means the inventory genuinely does not fit.")

        # ── P1C cont.: why a passing final still did not shortlist ───────────
        q = select(Application).where(Application.created_at >= since)
        if args.user:
            q = q.where(Application.user_id == args.user)
        apps = s.exec(q).all()
        shortlisted = [a for a in apps if a.status == ApplicationStatus.SHORTLISTED]
        print(f"\n  Applications created in window: {len(apps)} "
              f"({len(shortlisted)} still SHORTLISTED)")
        over_bar = sum(1 for x in f_scores if x >= settings.shortlist_score_threshold)
        if over_bar and len(apps) < over_bar:
            print(f"  {over_bar - len(apps)} finals cleared the bar without producing an "
                  "Application.")
            print("  Candidate causes, in the order _shortlist_user checks them:")
            print("    - an Application already existed for that job (a re-score)")
            print("    - daily_shortlist_limit reached "
                  f"({settings.daily_shortlist_limit}/user/day)")
            print("    - company cap: 3 active apps/company, 40d cooldown")
            print("    - degraded/local scorer held to the provisional bar "
                  f"({settings.degraded_shortlist_threshold})")

        # ── P1D: per-user budget picture ─────────────────────────────────────
        print("\n── P1D: per-user queue and spend ──────────────────────────────")
        owners = s.exec(select(Job.user_id, func.count())
                        .where(Job.first_seen >= since,
                               Job.user_id != SHARED_POOL_USER)
                        .group_by(Job.user_id)).all()
        print(f"  {'user':<38} {'intake':>7} {'queued':>7} {'drain':>7} "
              f"{'final':>7} {'short':>6}")
        for uid, n_intake in sorted(owners, key=lambda r: -r[1])[:25]:
            base = select(func.count()).select_from(Job).where(
                Job.first_seen >= since,
                Job.user_id.is_(None) if uid is None else Job.user_id == uid)
            q_n = s.exec(base.where(Job.rerank_score.is_(None),
                                    Job.is_closed == False)).one()  # noqa: E712
            d_n = s.exec(base.where(tier1_drain_expr())).one()
            t_n = s.exec(base.where(terminal_verdict_expr())).one()
            aq = select(func.count()).select_from(Application).where(
                Application.created_at >= since,
                Application.user_id.is_(None) if uid is None
                else Application.user_id == uid)
            s_n = s.exec(aq).one()
            print(f"  {str(uid)[:38]:<38} {n_intake:>7} {q_n:>7} {d_n:>7} "
                  f"{t_n - d_n:>7} {s_n:>6}")

        # ── P0: alert eligibility, gate by gate ──────────────────────────────
        print("\n── P0: alert eligibility of shortlisted jobs ──────────────────")
        job_ids = [a.job_id for a in shortlisted]
        alert_jobs = []
        if job_ids:
            alert_jobs = s.exec(select(Job).where(Job.id.in_(job_ids[:5000]))).all()
        cutoff = now - timedelta(hours=24)
        posted_days = int(getattr(settings, "fresh_alert_max_posted_age_days", 30) or 0)
        posted_cutoff = now - timedelta(days=posted_days) if posted_days > 0 else None
        gates = {"scored below alert_min": 0, "known age > 24h": 0,
                 "posted age past loose bound": 0, "eligible": 0}
        for j in alert_jobs:
            fit = max(int(j.rerank_score or 0), int(j.blended_score or 0))
            known = j.first_seen or j.discovered_at
            if fit < settings.fresh_alert_min_score:
                gates["scored below alert_min"] += 1
            elif not known or known < cutoff:
                gates["known age > 24h"] += 1
            elif (j.posted_at is not None and posted_cutoff is not None
                  and j.posted_at < posted_cutoff):
                gates["posted age past loose bound"] += 1
            else:
                gates["eligible"] += 1
        for k, v in gates.items():
            print(f"  {k:<34} {v:>8}  {_pct(v, len(alert_jobs))}")

        # What the OLD single-bound gate would have done, so the P0 fix's effect
        # is visible in production rather than argued from code.
        old_blocked = 0
        for j in alert_jobs:
            fit = max(int(j.rerank_score or 0), int(j.blended_score or 0))
            if fit < settings.fresh_alert_min_score:
                continue
            ref = j.posted_at or j.first_seen
            if not ref or ref < cutoff:
                old_blocked += 1
        print(f"  would have been blocked by the pre-fix posted_at-first gate: "
              f"{old_blocked} ({_pct(old_blocked, len(alert_jobs))})")

        nq = select(func.count()).select_from(UserNotification).where(
            UserNotification.type == "fresh_job",
            UserNotification.created_at >= since)
        if args.user:
            nq = nq.where(UserNotification.user_id == args.user)
        print(f"  fresh_job notifications actually created: {s.exec(nq).one()}")

        # ── P1B/P1E samples ──────────────────────────────────────────────────
        if args.sample:
            print("\n── P1B: reject samples (judge these by hand) ──────────────────")
            for bucket, rowset in sorted(examples.items(),
                                         key=lambda kv: -hist.get(kv[0], 0)):
                print(f"\n  [{bucket}]  ({hist.get(bucket, 0)} total)")
                for jid, title, company, score, reason in rowset:
                    print(f"    #{jid} {str(score):>5}  {str(title)[:48]:<48} "
                          f"@ {str(company)[:24]}")
                    print(f"          {reason}")


if __name__ == "__main__":
    main()
