#!/usr/bin/env python3
"""Compare scoring configurations on the SAME jobs, and report what it costs.

Two questions this answers, which the running system cannot:

  1. GATE SAFETY — "is a stricter Tier-1 gate safe?"
     Jobs the gate drains never get a Tier-2 score, so the running system can
     never tell you what a higher gate would have killed. `--mode gate` forces a
     Tier-2 final on jobs REGARDLESS of their prescore, giving the (prescore,
     final) pairs the decision needs.

  2. MODEL CHOICE — "Haiku or GPT for Tier-2?"
     `--mode ab` scores each job with both and reports agreement, disagreement
     shape, and cost per config. Note the repo already tried this once:
     DUAL_SCORE_ENABLED is off because gpt-4o was ~2.5x Haiku's price for no
     quality gain (config.py). Re-testing a newer model is reasonable; expect to
     have to BEAT that prior, not just match it.

Read-only: nothing is written back to the Job rows. Every call is real spend, so
--limit defaults small and the estimated cost is printed before anything runs.

    python -m scripts.eval_scorers --mode gate --limit 200 --user <uid>
    python -m scripts.eval_scorers --mode ab   --limit 100 --model-b gpt-4.1
    python -m scripts.eval_scorers --mode gate --limit 200 --json out.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import List, Optional

from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import Job

# Per-call cost estimates (USD).
#   Anthropic rates are first-party verified: Haiku 4.5 $1.00/$5.00 per MTok,
#   with cache reads at 0.1x and writes at 1.25x.
#   OpenAI rates are ASSUMED — they are not verifiable from this repo, and
#   CAPACITY.md flags every OpenAI figure the same way. Treat the B-side cost
#   as indicative; the AGREEMENT numbers are the trustworthy part of this tool.
COST = {
    "prescore": 0.0002,        # gpt-4o-mini, ~1.7k in / 35 out   (assumed)
    "final_haiku_warm": 0.0033,  # 4.7k cached + 1.3k in + 300 out (verified)
    "final_haiku_cold": 0.0087,  # same, cache WRITE at 1.25x      (verified)
    "final_gpt41": 0.0060,     # ~2x Haiku input, 1.6x output      (ASSUMED)
}


def _fetch_jobs(limit: int, user_id: Optional[str], scored_only: bool) -> List[Job]:
    """Newest jobs to evaluate. `scored_only` reuses rows that already carry a
    real Tier-2 score so the run can be compared against ground truth."""
    with get_session() as session:
        q = select(Job).where(Job.is_closed == False)  # noqa: E712
        if user_id:
            q = q.where(Job.user_id == user_id)
        if scored_only:
            q = q.where(Job.rerank_breakdown.is_not(None))  # real Tier-2 finals only
        rows = session.exec(q.order_by(Job.first_seen.desc()).limit(limit)).all()
        for r in rows:
            session.expunge(r)
        return rows


def _pct(vals: List[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
    return s[k]


def _summary(vals: List[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": round(min(vals), 1),
        "p05": round(_pct(vals, 0.05), 1),
        "median": round(statistics.median(vals), 1),
        "p95": round(_pct(vals, 0.95), 1),
        "max": round(max(vals), 1),
        "mean": round(statistics.fmean(vals), 1),
    }


def run_gate(jobs: List[Job], resume: str, profile) -> dict:
    """Prescore AND final every job, ignoring the gate. Produces the (prescore,
    final) pairs that make gate selection an evidence question."""
    from app.matching.reranker import Reranker
    rk = Reranker(profile=profile)
    rk.prewarm_cache(resume)

    pairs, failures = [], 0
    for i, job in enumerate(jobs, 1):
        try:
            pre = rk.prescore(resume, job)
            if pre is None:
                failures += 1
                continue
            score, reason, _concerns, _bd = rk.score(resume, job)
            pairs.append({"job_id": job.id, "title": job.title, "company": job.company,
                          "prescore": float(pre[0]), "final": float(score),
                          "reason": reason[:120]})
        except Exception as e:
            failures += 1
            print(f"  [{i}/{len(jobs)}] job {job.id} failed: {e}", file=sys.stderr)
        if i % 10 == 0:
            print(f"  scored {i}/{len(jobs)}", file=sys.stderr)

    strong = [p for p in pairs if p["final"] >= 65]
    shortlist = [p for p in pairs if p["final"] >= settings.shortlist_score_threshold]

    # THE headline number: how high can the gate go before it starts eating
    # jobs Tier-2 would have called strong?
    gate_table = []
    for gate in (35, 50, 60, 65, 70, 75, 80):
        advanced = [p for p in pairs if p["prescore"] >= gate]
        kept_strong = [p for p in strong if p["prescore"] >= gate]
        gate_table.append({
            "gate": gate,
            "advance_rate_pct": round(100 * len(advanced) / len(pairs), 1) if pairs else None,
            "strong_kept_pct": round(100 * len(kept_strong) / len(strong), 1) if strong else None,
            "strong_lost": len(strong) - len(kept_strong),
            "finals_per_100_corpus": round(100 * len(advanced) / len(pairs), 1) if pairs else None,
        })

    return {
        "mode": "gate",
        "evaluated": len(pairs),
        "failures": failures,
        "prescore": _summary([p["prescore"] for p in pairs]),
        "final": _summary([p["final"] for p in pairs]),
        "strong_rate_pct": round(100 * len(strong) / len(pairs), 1) if pairs else None,
        "shortlist_rate_pct": round(100 * len(shortlist) / len(pairs), 1) if pairs else None,
        # Lowest prescore among jobs Tier-2 called strong. A gate above this
        # value provably kills at least one strong match.
        "min_prescore_among_strong": min([p["prescore"] for p in strong], default=None),
        "p05_prescore_among_strong": _pct([p["prescore"] for p in strong], 0.05),
        "gate_table": gate_table,
        "pairs": pairs,
    }


def run_ab(jobs: List[Job], resume: str, profile, model_b: str) -> dict:
    """Score every job with Tier-2 config A (Anthropic) and B (OpenAI)."""
    from app.matching.reranker import Reranker
    rk = Reranker(profile=profile)
    rk.prewarm_cache(resume)

    rows, failures = [], 0
    for i, job in enumerate(jobs, 1):
        a = b = None
        try:
            a = rk.score(resume, job, provider="anthropic")[0]
        except Exception as e:
            print(f"  job {job.id} A failed: {e}", file=sys.stderr)
        try:
            _prev, settings.dual_score_openai_model = settings.dual_score_openai_model, model_b
            b = rk.score(resume, job, provider="openai")[0]
            settings.dual_score_openai_model = _prev
        except Exception as e:
            settings.dual_score_openai_model = _prev
            print(f"  job {job.id} B failed: {e}", file=sys.stderr)
        if a is None or b is None:
            failures += 1
            continue
        rows.append({"job_id": job.id, "title": job.title,
                     "a": float(a), "b": float(b), "delta": float(b) - float(a)})
        if i % 10 == 0:
            print(f"  scored {i}/{len(jobs)}", file=sys.stderr)

    bar = settings.shortlist_score_threshold
    agree = [r for r in rows if (r["a"] >= bar) == (r["b"] >= bar)]
    a_only = [r for r in rows if r["a"] >= bar > r["b"]]
    b_only = [r for r in rows if r["b"] >= bar > r["a"]]

    return {
        "mode": "ab",
        "model_a": settings.scoring_model,
        "model_b": model_b,
        "evaluated": len(rows),
        "failures": failures,
        "a": _summary([r["a"] for r in rows]),
        "b": _summary([r["b"] for r in rows]),
        # The number that matters: do they make the same SHORTLIST decision?
        # Score correlation is interesting; decision agreement is what ships.
        "shortlist_agreement_pct": round(100 * len(agree) / len(rows), 1) if rows else None,
        "a_shortlists_b_rejects": len(a_only),
        "b_shortlists_a_rejects": len(b_only),
        "mean_delta_b_minus_a": round(statistics.fmean([r["delta"] for r in rows]), 2) if rows else None,
        "calibration_offset_hint": round(-statistics.fmean([r["delta"] for r in rows]), 1) if rows else None,
        "biggest_disagreements": sorted(rows, key=lambda r: -abs(r["delta"]))[:10],
        "rows": rows,
    }


def run_regress(pairs_path: str, tiers: str) -> dict:
    """Run the fixed regression pairs (tests/regress_pairs.json) through the
    CURRENT prompts and score band accuracy against ground truth. Turns every
    prompt edit into a ~$0.10, 5-minute check instead of a paper argument.

    Band mapping — Tier-1: B<=30 (the prompt's stated blocker band), M 31-59,
    H>=60. Tier-2 reuses the same letters against its own bands (B<=39, M
    40-59, H>=60). Scoring: correct band 2, adjacent 1, wrong 0."""
    with open(pairs_path) as f:
        fixture = json.load(f)

    class _Profile:
        pass
    persona = fixture["persona"]
    prof = _Profile()
    prof.target_roles = persona["target_roles"]
    prof.key_skills = persona["key_skills"]
    prof.years_experience = persona["years_experience"]
    prof.preferred_country = persona["preferred_country"]
    prof.requires_sponsorship = persona["requires_sponsorship"]
    prof.current_title = ""
    prof.user_id = None

    resume = (
        f"# Candidate\n\n## Summary\n{persona['target_roles']} with "
        f"{persona['years_experience']} years of experience.\n\n## Skills\n"
        f"{persona['key_skills']}\n\n## Experience\nBuilt and operated Python "
        f"back-end and ML services on AWS: FastAPI APIs, PyTorch training and "
        f"serving, PostgreSQL data layers. Requires visa sponsorship to work in "
        f"{persona['preferred_country']}."
    )

    from app.db.models import Job as JobModel, JobSource
    from app.matching.reranker import Reranker
    rk = Reranker(profile=prof)

    def band_t1(score: float) -> str:
        return "B" if score <= 30 else ("M" if score < 60 else "H")

    def band_t2(score: float) -> str:
        return "B" if score <= 39 else ("M" if score < 60 else "H")

    _ADJ = {("B", "M"), ("M", "B"), ("M", "H"), ("H", "M")}

    def points(got: Optional[str], want: str) -> int:
        if got is None:
            return 0
        if got == want:
            return 2
        return 1 if (got, want) in _ADJ else 0

    rows = []
    for pair in fixture["pairs"]:
        job = JobModel(title=pair["title"], company=pair["company"],
                       location=pair.get("location", ""), remote=pair.get("remote", False),
                       description=pair["description"], source=JobSource.MANUAL,
                       external_id=f"regress-{pair['id']}", url="https://example.com/x")
        job.id = 0
        row = {"id": pair["id"], "class": pair["class"], "gt": pair["gt_band"],
               "gt_reason": pair["gt_reason"]}
        if tiers in ("t1", "both"):
            try:
                pre = rk.prescore(resume, job)
                row["t1_score"] = float(pre[0]) if pre else None
                row["t1_band"] = band_t1(pre[0]) if pre else None
                row["t1_reason"] = pre[1][:80] if pre else None
            except Exception as e:
                row["t1_score"], row["t1_band"] = None, None
                print(f"  {pair['id']} T1 failed: {e}", file=sys.stderr)
            row["t1_points"] = points(row.get("t1_band"), pair["gt_band"])
        if tiers in ("t2", "both"):
            try:
                score, reason, concerns, bd = rk.score(resume, job)
                row["t2_score"] = float(score)
                row["t2_band"] = band_t2(score)
                row["t2_concerns_n"] = len(concerns)
                row["t2_reason"] = reason[:80]
            except Exception as e:
                row["t2_score"], row["t2_band"] = None, None
                print(f"  {pair['id']} T2 failed: {e}", file=sys.stderr)
            row["t2_points"] = points(row.get("t2_band"), pair["gt_band"])
        rows.append(row)
        print(f"  {pair['id']}: gt={pair['gt_band']}"
              + (f" t1={row.get('t1_band')}({row.get('t1_score')})" if tiers != 't2' else "")
              + (f" t2={row.get('t2_band')}({row.get('t2_score')})" if tiers != 't1' else ""),
              file=sys.stderr)

    out: dict = {"mode": "regress", "pairs": len(rows), "rows": rows}
    for t in (("t1",) if tiers == "t1" else ("t2",) if tiers == "t2" else ("t1", "t2")):
        pts = [r.get(f"{t}_points", 0) for r in rows]
        out[f"{t}_total"] = f"{sum(pts)}/{2 * len(rows)}"
        by_class: dict = {}
        for r in rows:
            c = by_class.setdefault(r["class"], [0, 0])
            c[0] += r.get(f"{t}_points", 0)
            c[1] += 2
        out[f"{t}_by_class"] = {k: f"{v[0]}/{v[1]}" for k, v in by_class.items()}
        out[f"{t}_wrong_band"] = [
            {"id": r["id"], "gt": r["gt"], "got": r.get(f"{t}_band"),
             "score": r.get(f"{t}_score"), "why_gt": r["gt_reason"]}
            for r in rows if r.get(f"{t}_points", 0) == 0
        ]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("gate", "ab", "regress"), required=True)
    ap.add_argument("--limit", type=int, default=100, help="jobs to evaluate (default 100)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--model-b", default="gpt-4.1", help="Tier-2 model for the B side (ab mode)")
    ap.add_argument("--pairs", default="tests/regress_pairs.json",
                    help="regress mode: fixture path")
    ap.add_argument("--tiers", choices=("t1", "t2", "both"), default="both",
                    help="regress mode: which tier(s) to run")
    ap.add_argument("--json", default=None, help="write the full result to this path")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    args = ap.parse_args()

    if args.mode == "regress":
        n_pairs = len(json.load(open(args.pairs))["pairs"])
        calls = n_pairs * (2 if args.tiers == "both" else 1)
        est = n_pairs * ((COST["prescore"] if args.tiers != "t2" else 0)
                         + (COST["final_haiku_warm"] if args.tiers != "t1" else 0))
        print(f"About to run {calls} calls over {n_pairs} fixed pairs (~${est:.2f}).")
        if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 0
        t0 = time.time()
        result = run_regress(args.pairs, args.tiers)
        result["elapsed_sec"] = round(time.time() - t0, 1)
        printable = {k: v for k, v in result.items() if k != "rows"}
        print("\n" + json.dumps(printable, indent=2))
        if args.json:
            with open(args.json, "w") as f:
                json.dump(result, f, indent=2)
            print(f"\nFull result written to {args.json}")
        return 0

    jobs = _fetch_jobs(args.limit, args.user, scored_only=False)
    if not jobs:
        print("No jobs matched. Try a different --user or drop it.", file=sys.stderr)
        return 1

    n = len(jobs)
    if args.mode == "gate":
        est = n * (COST["prescore"] + COST["final_haiku_warm"])
        detail = f"{n} prescores + {n} Haiku finals"
    else:
        est = n * (COST["final_haiku_warm"] + COST["final_gpt41"])
        detail = f"{n} Haiku finals + {n} {args.model_b} finals"

    print(f"About to run {detail}.")
    print(f"Estimated cost: ${est:.2f}  (OpenAI side is an ASSUMED rate — see COST in this file)")
    if not args.yes:
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 0

    from app.matching.pipeline import _load_resume
    uid_arg = None if (not args.user or args.user == "local") else args.user
    resume = _load_resume(user_id=uid_arg)
    profile = None
    try:
        from app.autofill.answer_pack import _get_or_create_profile
        profile = _get_or_create_profile(user_id=uid_arg)
    except Exception as e:
        print(f"(no profile for {args.user}: {e} — using the legacy rubric)", file=sys.stderr)

    t0 = time.time()
    result = (run_gate(jobs, resume, profile) if args.mode == "gate"
              else run_ab(jobs, resume, profile, args.model_b))
    result["elapsed_sec"] = round(time.time() - t0, 1)
    result["estimated_cost_usd"] = round(est, 2)

    printable = {k: v for k, v in result.items() if k not in ("pairs", "rows")}
    print("\n" + json.dumps(printable, indent=2))

    if args.mode == "gate" and result.get("min_prescore_among_strong") is not None:
        print("\n--- READ THIS ---")
        print(f"Lowest prescore among jobs Tier-2 scored >=65: "
              f"{result['min_prescore_among_strong']:.0f}")
        print(f"5th-percentile prescore among those:            "
              f"{result['p05_prescore_among_strong']:.0f}")
        print("A gate above the 5th percentile loses ~5% of your strong matches.")
        print("A gate above the minimum provably loses at least one.")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nFull result (incl. per-job rows) written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
