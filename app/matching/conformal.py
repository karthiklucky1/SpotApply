"""CardRace v2 banding — certified AUTO-IN / BAND / AUTO-OUT (docs/CARDRACE_DESIGN.md §3.4).

The quality guarantee lives HERE, as code, not as intention:

- With NO calibration file, every pair is BAND — i.e. Claude decides everything,
  exactly like today. The deterministic score can never auto-decide anything
  until a fitted, held-out calibration exists (scripts/build_calibration.py).
- AUTO-IN requires the calibrated expanded score to clear t_hi (a threshold
  certified >= AUTO_IN_PRECISION on held-out data) AND a narrow
  direct-vs-expanded spread (a score built on inference never auto-admits).
- AUTO-OUT is decided on the EXPANDED (optimistic) score: if even the generous
  reading is certified below t_lo, dropping is safe.
- Any low-confidence card field forces BAND — never guess on missing data.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import List, Optional

from app.config import settings

log = logging.getLogger(__name__)

AUTO_IN = "auto_in"
AUTO_OUT = "auto_out"
BAND = "band"


@dataclass
class BandDecision:
    band: str
    calibrated: Optional[float]   # calibrated expanded score (None without calibration)
    reason: str


# ── Calibration file (written by scripts/build_calibration.py) ───────────────
_CAL: Optional[dict] = None
_CAL_MTIME: float = -1.0
_LOCK = threading.Lock()


def load_calibration(path: Optional[str] = None) -> Optional[dict]:
    """Cached load; None when the file is missing/invalid → all-BAND regime."""
    global _CAL, _CAL_MTIME
    p = path or settings.card_calibration_path
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return None
    with _LOCK:
        if _CAL is not None and mtime == _CAL_MTIME:
            return _CAL
        try:
            with open(p, "r", encoding="utf-8") as f:
                cal = json.load(f)
            pts = cal.get("isotonic") or []
            ok = (isinstance(cal.get("t_hi"), (int, float))
                  and isinstance(cal.get("t_lo"), (int, float))
                  and isinstance(pts, list))
            if not ok:
                raise ValueError("calibration file missing t_hi/t_lo/isotonic")
            _CAL, _CAL_MTIME = cal, mtime
            log.info("CardRace calibration loaded: v%s t_hi=%.1f t_lo=%.1f (n_holdout=%s)",
                     cal.get("version"), cal["t_hi"], cal["t_lo"], cal.get("n_holdout"))
        except Exception as e:
            log.warning("CardRace calibration load failed (%s): %s — all-BAND regime", p, e)
            _CAL, _CAL_MTIME = None, mtime
        return _CAL


def calibrate(raw: float, cal: Optional[dict]) -> float:
    """Monotone piecewise-linear map raw→calibrated. Identity without a table."""
    if not cal:
        return raw
    pts = cal.get("isotonic") or []
    if len(pts) < 2:
        return raw
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    if raw <= xs[0]:
        return ys[0]
    if raw >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if raw <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (raw - x0) / (x1 - x0)
    return ys[-1]


def assign_band(direct: float, expanded: float, spread: float,
                low_confidence: List[str],
                cal: Optional[dict] = None) -> BandDecision:
    """Route one pair. Every branch defaults toward BAND — Claude decides doubt."""
    if cal is None:
        cal = load_calibration()
    if cal is None:
        return BandDecision(BAND, None, "no calibration fitted — Claude decides")

    c = calibrate(expanded, cal)
    if low_confidence:
        return BandDecision(BAND, c, f"low-confidence card fields: {','.join(low_confidence)}")

    if c <= float(cal["t_lo"]):
        return BandDecision(AUTO_OUT, c,
                            f"even the optimistic score ({c:.0f}) is certified below the bar")
    if c >= float(cal["t_hi"]):
        max_spread = float(getattr(settings, "card_max_auto_in_spread", 12.0))
        if spread > max_spread:
            return BandDecision(BAND, c,
                                f"score {c:.0f} clears t_hi but spread {spread:.0f} > {max_spread:.0f} — mostly inference, Claude checks")
        return BandDecision(AUTO_IN, c, f"certified above t_hi ({cal['t_hi']:.0f})")
    return BandDecision(BAND, c, "inside the doubt band — Claude decides")
