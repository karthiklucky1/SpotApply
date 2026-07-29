"""Fit the CardRace v2 calibration from the shadow ledger — docs/CARDRACE_DESIGN.md §3.4.

Reads card_match_shadow (one row per real Claude final scored beside g()),
splits train/holdout DETERMINISTICALLY (job_id % 5 == 0 → holdout, ~20%), fits
a monotone isotonic map (pool-adjacent-violators, no sklearn) from g()'s
expanded score to Claude's score on the TRAIN split only, then places the band
thresholds on the UNTOUCHED holdout:

  t_hi — smallest calibrated score whose holdout precision
         P(claude >= bar | cal >= t) has Wilson lower bound >= 0.95
  t_lo — largest calibrated score whose holdout miss rate
         P(claude >= bar | cal <= t) has Wilson upper bound <= 0.05

No qualifying threshold → the corresponding auto zone is EMPTY (t_hi=101 /
t_lo=-1): the engine simply keeps sending everything to Claude. Quality can
fail only toward "more Claude", never toward "worse shortlists".

Usage:
  python -m scripts.build_calibration            # refuses below --min-rows
  python -m scripts.build_calibration --force    # fit anyway (dev)
  python -m scripts.build_calibration --dry-run  # report, write nothing
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.init_db import get_session, init_db  # noqa: E402
from app.db.models import CardMatchShadow  # noqa: E402

MIN_CELL = 30          # minimum holdout points before a threshold may be placed
Z = 1.959964           # 95% two-sided


def wilson_bounds(k: int, n: int) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + Z * Z / n
    center = (p + Z * Z / (2 * n)) / denom
    half = (Z / denom) * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def pav_isotonic(points: list[tuple[float, float]]) -> list[list[float]]:
    """Pool-adjacent-violators: monotone nondecreasing fit of y on x.
    Returns compact [[x, y], ...] breakpoints for piecewise-linear lookup."""
    if not points:
        return []
    pts = sorted(points)
    # blocks: [x_sum, y_sum, weight, x_min, x_max]
    blocks: list[list[float]] = []
    for x, y in pts:
        blocks.append([x, y, 1.0, x, x])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] > blocks[-1][1] / blocks[-1][2]:
            b = blocks.pop()
            a = blocks[-1]
            a[0] += b[0]
            a[1] += b[1]
            a[2] += b[2]
            a[3] = min(a[3], b[3])
            a[4] = max(a[4], b[4])
    out: list[list[float]] = []
    for b in blocks:
        y = round(b[1] / b[2], 2)
        # one breakpoint per block edge keeps interpolation flat inside a block
        out.append([round(b[3], 2), y])
        if b[4] > b[3]:
            out.append([round(b[4], 2), y])
    return out


def _calibrate(raw: float, iso: list[list[float]]) -> float:
    from app.matching.conformal import calibrate
    return calibrate(raw, {"isotonic": iso, "t_hi": 0, "t_lo": 0})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rows", type=int, default=500)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=settings.card_calibration_path)
    args = ap.parse_args()

    init_db()
    bar = float(settings.shortlist_score_threshold)
    with get_session() as session:
        rows = session.exec(select(CardMatchShadow)).all()

    if not rows:
        print("No shadow rows yet — enable CARD_MATCH_SHADOW and let the scoring "
              "lane run beside real Claude finals first.")
        return 1
    if len(rows) < args.min_rows and not args.force:
        print(f"Only {len(rows)} shadow rows (< {args.min_rows}). The certificates "
              f"would be statistically meaningless — collect more, or --force for dev.")
        return 1

    train = [r for r in rows if r.job_id % 5 != 0]
    hold = [r for r in rows if r.job_id % 5 == 0]
    print(f"rows={len(rows)}  train={len(train)}  holdout={len(hold)}  bar={bar:.0f}")

    iso = pav_isotonic([(r.expanded_score, r.llm_score) for r in train])

    # Holdout, calibrated
    H = [( _calibrate(r.expanded_score, iso), r.llm_score,
           r.spread, bool(r.llm_score >= bar)) for r in hold]
    n = len(H)
    if n < MIN_CELL:
        print(f"holdout n={n} < {MIN_CELL} — writing an all-BAND calibration.")
        t_hi, t_lo = 101.0, -1.0
    else:
        grid = sorted({round(c) for c, *_ in H})
        # t_hi: smallest t whose >=t slice is certified precise
        t_hi = 101.0
        for t in sorted(grid, reverse=False):
            sl = [good for c, _s, _sp, good in H if c >= t]
            if len(sl) < MIN_CELL:
                continue
            lo, _ = wilson_bounds(sum(sl), len(sl))
            if lo >= 0.95:
                t_hi = float(t)
                break
        # t_lo: largest t whose <=t slice is certified safe to drop
        t_lo = -1.0
        for t in sorted(grid, reverse=True):
            sl = [good for c, _s, _sp, good in H if c <= t]
            if len(sl) < MIN_CELL:
                continue
            _, hi = wilson_bounds(sum(sl), len(sl))
            if hi <= 0.05:
                t_lo = float(t)
                break

    # Gate report (docs/CARDRACE_DESIGN.md §3.4 launch gates, measured)
    mae = sum(abs(c - s) for c, s, *_ in H) / max(1, n)
    agree = sum(1 for c, s, *_ in H if (c >= bar) == (s >= bar)) / max(1, n)
    auto_in = [(c, s, sp, g) for c, s, sp, g in H
               if c >= t_hi and sp <= settings.card_max_auto_in_spread]
    auto_out = [(c, s, sp, g) for c, s, sp, g in H if c <= t_lo]
    band_mass = 1.0 - (len(auto_in) + len(auto_out)) / max(1, n)
    print(f"holdout: MAE={mae:.1f}  decision-agree@{bar:.0f}={agree:.1%}")
    if auto_in:
        print(f"AUTO-IN  (t_hi={t_hi:.0f}): n={len(auto_in)}  realized precision="
              f"{sum(g for *_, g in auto_in) / len(auto_in):.1%}")
    else:
        print(f"AUTO-IN  (t_hi={t_hi:.0f}): empty — everything above goes to Claude")
    if auto_out:
        print(f"AUTO-OUT (t_lo={t_lo:.0f}): n={len(auto_out)}  realized miss rate="
              f"{sum(g for *_, g in auto_out) / len(auto_out):.1%}")
    else:
        print(f"AUTO-OUT (t_lo={t_lo:.0f}): empty — everything below goes to Claude")
    print(f"band mass (Claude escalation share): {band_mass:.1%}")

    cal = {
        "version": 1,
        "fitted_at": datetime.utcnow().isoformat() + "Z",
        "bar": bar,
        "n_rows": len(rows), "n_train": len(train), "n_holdout": n,
        "isotonic": iso,
        "t_hi": t_hi, "t_lo": t_lo,
        "holdout_mae": round(mae, 2),
        "holdout_decision_agreement": round(agree, 4),
        "scoring_model": settings.scoring_model,   # a model bump must invalidate this
        "mint_model": settings.card_mint_model,
    }
    if args.dry_run:
        print("--dry-run: nothing written.")
        return 0
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=1)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
