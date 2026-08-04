"""Conformal banding safety — CardRace v2 (docs/CARDRACE_DESIGN.md §3.4).

The quality guarantee is structural: no calibration → everything is BAND
(Claude decides, exactly like today); wide spread never auto-admits; low
confidence never auto-decides; the isotonic map is monotone.
"""
from __future__ import annotations

import json

import pytest

from app.matching.conformal import (AUTO_IN, AUTO_OUT, BAND, assign_band,
                                    calibrate, load_calibration)

CAL = {
    "version": 1, "bar": 60,
    "isotonic": [[0, 10], [40, 45], [70, 68], [95, 92]],
    "t_hi": 75.0, "t_lo": 30.0,
    "n_holdout": 1000,
}


def test_no_calibration_means_everything_is_band():
    d = assign_band(90.0, 92.0, 2.0, [], cal=None) if load_calibration("data/__none__.json") is None else None
    # load_calibration on a missing file returns None; assign_band(cal=None)
    # re-loads and must land in the all-BAND regime.
    assert d is not None
    assert d.band == BAND
    assert d.calibrated is None


def test_auto_in_requires_threshold_and_narrow_spread():
    ok = assign_band(88.0, 90.0, 2.0, [], cal=CAL)
    assert ok.band == AUTO_IN
    wide = assign_band(60.0, 90.0, 30.0, [], cal=CAL)
    assert wide.band == BAND
    assert "spread" in wide.reason


def test_auto_out_uses_the_optimistic_score():
    d = assign_band(5.0, 12.0, 7.0, [], cal=CAL)
    assert d.band == AUTO_OUT
    # optimistic score above t_lo → not safe to drop, even if direct is low
    d2 = assign_band(5.0, 50.0, 45.0, [], cal=CAL)
    assert d2.band == BAND


def test_low_confidence_always_bands():
    d = assign_band(88.0, 90.0, 2.0, ["visa"], cal=CAL)
    assert d.band == BAND
    assert "visa" in d.reason


def test_middle_scores_band():
    d = assign_band(55.0, 58.0, 3.0, [], cal=CAL)
    assert d.band == BAND


def test_calibrate_is_monotone_and_interpolates():
    xs = [0, 10, 40, 55, 70, 80, 95, 100]
    ys = [calibrate(float(x), CAL) for x in xs]
    assert ys == sorted(ys)                       # monotone
    assert calibrate(40.0, CAL) == pytest.approx(45.0)
    assert calibrate(55.0, CAL) == pytest.approx(56.5)  # linear between 45 and 68
    assert calibrate(-5.0, CAL) == 10.0           # clamped to edges
    assert calibrate(200.0, CAL) == 92.0
    assert calibrate(50.0, None) == 50.0          # identity without a table


def test_calibration_file_roundtrip(tmp_path):
    p = tmp_path / "calibration.json"
    p.write_text(json.dumps(CAL))
    cal = load_calibration(str(p))
    assert cal is not None and cal["t_hi"] == 75.0
    d = assign_band(88.0, 90.0, 2.0, [], cal=cal)
    assert d.band == AUTO_IN


def test_broken_calibration_file_degrades_to_band(tmp_path):
    p = tmp_path / "calibration.json"
    p.write_text("{not json")
    assert load_calibration(str(p)) is None


def test_pav_isotonic_fits_monotone():
    from scripts.build_calibration import pav_isotonic, wilson_bounds
    pts = [(10.0, 20.0), (20.0, 15.0), (30.0, 40.0), (40.0, 35.0), (50.0, 80.0)]
    iso = pav_isotonic(pts)
    ys = [y for _x, y in iso]
    assert ys == sorted(ys)                       # PAV output is monotone
    lo, hi = wilson_bounds(95, 100)
    assert 0.87 < lo < 0.95 < 0.99 and lo < hi <= 1.0
    lo0, hi0 = wilson_bounds(0, 0)
    assert (lo0, hi0) == (0.0, 1.0)               # no data → no certificate
