"""CardRace v2 live demo — the design doc's worked example, executed for real.

No LLM, no network, no DB: this runs the shipped deterministic engine
(app/matching/card_match.py + skill_graph.json + conformal banding) on the
three candidates from docs/CARDRACE_DESIGN.md and prints what each costs.

Usage: python -m scripts.cardrace_demo
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.matching.card_match import match_cards  # noqa: E402
from app.matching.conformal import assign_band  # noqa: E402
from app.matching.skill_graph import load_graph  # noqa: E402

JOB = {
    "role_family": "ml engineer", "seniority": "mid",
    "years_min": 3, "years_max": 7,
    "capabilities": [
        {"name": "python", "importance": 0.9, "evidence_needed": []},
        {"name": "machine learning", "importance": 0.8,
         "evidence_needed": ["deep learning"]},
        {"name": "inference optimization", "importance": 0.9,
         "evidence_needed": ["ml deployment", "gpu programming"]},
    ],
    "nice_to_have": ["docker"],
    "disqualifiers": [],
    "remote_policy": "remote", "remote_scope": "country",
    "country": "united states", "visa": "silent",
    "confidence": {"skills": 0.9, "experience": 0.9, "location": 0.9, "visa": 0.9},
}

PROFILE = {"requires_sponsorship": True, "work_authorization": "F-1 STEM OPT",
           "preferred_country": "united states", "remote_ok": True,
           "open_to_relocation": False, "location": "Austin, TX"}


def candidate(name, skills, years=4, level="mid", conf=0.8, rationale=""):
    return name, {
        "skills": skills, "years_experience": years,
        "effective_level": level, "effective_level_confidence": conf,
        "level_rationale": rationale, "role_families": ["ml engineer"],
        "domains": ["ml"], "_profile": dict(PROFILE),
    }


# A stand-in calibration so banding is demonstrable (production uses the fitted
# file from scripts/build_calibration.py; without it EVERYTHING is BAND).
DEMO_CAL = {"version": 0, "bar": 60,
            "isotonic": [[0, 0], [100, 100]], "t_hi": 78.0, "t_lo": 30.0}

CANDIDATES = [
    candidate("A — Priya (vLLM/CUDA, never says 'inference optimization')", [
        {"name": "python", "evidence": 0.95}, {"name": "pytorch", "evidence": 0.9},
        {"name": "cuda", "evidence": 0.85}, {"name": "vllm", "evidence": 0.9},
        {"name": "transformers", "evidence": 0.8},
    ], rationale="built an LLM serving engine in production"),
    candidate("B — plain backend dev (Python/FastAPI/AWS, no ML)", [
        {"name": "python", "evidence": 0.9}, {"name": "fastapi", "evidence": 0.85},
        {"name": "aws", "evidence": 0.7}, {"name": "docker", "evidence": 0.7},
    ]),
    candidate("C — skills-list-only resume (Python named once)", [
        {"name": "python", "evidence": 0.4},
    ], years=1, level="junior", conf=0.6),
]


def main() -> int:
    graph = load_graph()
    print("JD: ML Engineer — python, machine learning, inference optimization "
          "(remote, US, silent on visa)\n")
    claude_calls = 0
    for name, card in CANDIDATES:
        r = match_cards(card, JOB, graph=graph)
        d = assign_band(r.direct, r.expanded, r.spread, r.low_confidence, cal=DEMO_CAL)
        print(f"{name}")
        print(f"  direct={r.direct:.0f}  expanded={r.expanded:.0f}  "
              f"spread={r.spread:.0f}  ->  {d.band.upper()}  ({d.reason})")
        for k, v in r.breakdown.items():
            print(f"    {k:<10} {v['score']:>5.0f}  {v['note']}")
        if d.band == "band":
            claude_calls += 1
            print("    -> Claude judges this one ($0.0033)")
        else:
            print("    -> decided by arithmetic ($0.000000)")
        print()
    print(f"Claude calls needed: {claude_calls}/{len(CANDIDATES)} "
          f"(the old engine: {len(CANDIDATES)}/{len(CANDIDATES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
