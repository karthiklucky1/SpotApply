#!/usr/bin/env python
"""Location-verification report: accuracy proxies, missed jobs, delay, spend.

Reads only. Answers, for the postings first seen in the window:

  * how many new postings were resolved at intake, by the page, by the model,
    or never (and why they stayed unresolved);
  * what verification cost — model tokens and USD from the geography rows and
    the spend ledger (kind=geo_verify), per new posting and per delivered
    eligible job;
  * how long the held copies waited (first_seen → verified_at), p50/p95;
  * the per-user verdict mix, and how many copies were held past the
    scoring window without ever resolving (the "missed valid job" risk);
  * how many ineligible verdicts a user overrode by shortlisting manually —
    the cheapest accuracy signal we have until a labelled sample exists.

    python scripts/geo_verification_report.py --days 7
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import select

from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus, Job, JobGeography, LlmSpend
from app.discovery.pipeline import SHARED_POOL_USER


def _pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1))))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    since = datetime.utcnow() - timedelta(days=args.days)
    out: dict = {"window_days": args.days, "since": since.isoformat()}

    with get_session() as s:
        rows = s.exec(select(JobGeography).where(JobGeography.created_at >= since)).all()
        by_status: dict = {}
        by_source: dict = {}
        cost = 0.0
        tokens_in = tokens_out = 0
        llm_rows = 0
        unresolved_reasons: dict = {}
        for r in rows:
            by_status[r.status] = by_status.get(r.status, 0) + 1
            by_source[r.evidence_source] = by_source.get(r.evidence_source, 0) + 1
            cost += float(r.est_cost_usd or 0.0)
            tokens_in += int(r.input_tokens or 0)
            tokens_out += int(r.output_tokens or 0)
            if r.provider:
                llm_rows += 1
            if r.status != "resolved":
                key = r.last_error or r.last_step or "none"
                unresolved_reasons[key] = unresolved_reasons.get(key, 0) + 1
        out["new_postings"] = len(rows)
        out["by_status"] = by_status
        out["by_evidence_source"] = by_source
        out["unresolved_reasons"] = unresolved_reasons
        out["model_calls"] = llm_rows
        out["model_tokens"] = {"input": tokens_in, "output": tokens_out}
        out["est_cost_usd_from_rows"] = round(cost, 4)

        ledger = s.exec(
            select(func.sum(LlmSpend.est_cost_usd), func.sum(LlmSpend.calls))
            .where(LlmSpend.kind == "geo_verify", LlmSpend.day >= since.date())
        ).first()
        out["ledger_geo_verify"] = {"usd": round(float(ledger[0] or 0.0), 4),
                                    "calls": int(ledger[1] or 0)} if ledger else None

        # Per-user verdict mix on copies of NEW postings.
        keys = {(r.source, r.external_id) for r in rows}
        verdicts: dict = {}
        waits: list = []
        held_past_window = 0
        window = timedelta(days=5)
        for r in rows:
            copies = s.exec(select(Job.eligibility, Job.first_seen, Job.rerank_score, Job.is_closed)
                            .where(Job.external_id == r.external_id, Job.user_id != SHARED_POOL_USER)).all()
            for elig, fs, score, closed in copies:
                verdicts[elig or "legacy"] = verdicts.get(elig or "legacy", 0) + 1
                if elig == "unknown" and fs and datetime.utcnow() - fs > window and score is None:
                    held_past_window += 1
            if r.verified_at and r.last_step in ("page", "llm") and r.created_at:
                waits.append((r.verified_at - r.created_at).total_seconds())
        out["copy_verdicts"] = verdicts
        out["held_past_scoring_window"] = held_past_window
        out["verification_delay_seconds"] = {
            "n": len(waits), "p50": _pct(waits, 50), "p95": _pct(waits, 95),
            "mean": round(statistics.mean(waits), 1) if waits else None}

        delivered = 0
        overrides = 0
        if keys:
            ext_ids = [k[1] for k in keys]
            for start in range(0, len(ext_ids), 500):
                chunk = ext_ids[start:start + 500]
                q = (select(Job.eligibility, Application.status)
                     .join(Application, Application.job_id == Job.id)
                     .where(Job.external_id.in_(chunk)))
                for elig, status in s.exec(q).all():
                    if status in (ApplicationStatus.SHORTLISTED, ApplicationStatus.TAILORED,
                                  ApplicationStatus.SUBMITTED, ApplicationStatus.INTERVIEWING):
                        if elig == "eligible":
                            delivered += 1
                        elif elig == "ineligible":
                            overrides += 1
        out["delivered_eligible_jobs"] = delivered
        out["ineligible_shortlisted_by_user"] = overrides
        out["cost_per_new_posting_usd"] = round(cost / len(rows), 6) if rows else None
        out["cost_per_delivered_eligible_job_usd"] = round(cost / delivered, 6) if delivered else None

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    print(f"Geo verification — last {args.days} days (since {since:%Y-%m-%d %H:%M} UTC)")
    print(f"  new postings recorded: {out['new_postings']}  by status: {out['by_status']}")
    print(f"  evidence source: {out['by_evidence_source']}")
    print(f"  unresolved reasons: {out['unresolved_reasons']}")
    print(f"  model calls: {out['model_calls']}  tokens: {out['model_tokens']}  "
          f"cost from rows: ${out['est_cost_usd_from_rows']}  ledger: {out['ledger_geo_verify']}")
    print(f"  copy verdicts: {out['copy_verdicts']}  held past scoring window: {out['held_past_scoring_window']}")
    print(f"  verification delay: {out['verification_delay_seconds']}")
    print(f"  delivered eligible jobs: {out['delivered_eligible_jobs']}  "
          f"ineligible verdicts the user shortlisted anyway: {out['ineligible_shortlisted_by_user']}")
    print(f"  cost per new posting: {out['cost_per_new_posting_usd']}  "
          f"per delivered eligible job: {out['cost_per_delivered_eligible_job_usd']}")


if __name__ == "__main__":
    main()
