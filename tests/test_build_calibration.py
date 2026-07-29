"""End-to-end: shadow ledger → fitted calibration → certified band routing.

Simulates what production will produce (g() scores that track Claude's with
noise), runs the real fitting script, and verifies the certificates it writes
actually hold — plus the safety property that thin/garbage data produces an
all-BAND calibration instead of a confident wrong one.
"""
from __future__ import annotations

import json
import random
import sys

import pytest

from app.db.init_db import get_session, init_db
from app.db.models import CardMatchShadow
from app.matching.conformal import AUTO_IN, AUTO_OUT, BAND, assign_band, load_calibration


def _seed_shadow(n: int, seed: int = 7) -> None:
    """Synthetic but realistic: expanded ≈ llm + noise(σ=6), spread mostly small."""
    rng = random.Random(seed)
    with get_session() as session:
        for i in range(n):
            llm = rng.uniform(5, 95)
            g = max(0.0, min(100.0, llm + rng.gauss(0, 6)))
            spread = abs(rng.gauss(0, 4))
            session.add(CardMatchShadow(
                job_id=i + 1, user_id="u1",
                llm_score=llm,
                direct_score=max(0.0, g - spread), expanded_score=g, spread=spread,
                band="band", card_key=f"hash:{i}",
            ))
        session.commit()


def _run_build(tmp_path, monkeypatch, extra=()):
    from scripts import build_calibration
    out = tmp_path / "calibration.json"
    monkeypatch.setattr(sys, "argv",
                        ["build_calibration", "--out", str(out), "--force", *extra])
    rc = build_calibration.main()
    return rc, out


@pytest.fixture(autouse=True)
def _clean_shadow():
    from sqlmodel import delete
    init_db()
    with get_session() as session:
        session.exec(delete(CardMatchShadow))
        session.commit()
    yield
    with get_session() as session:
        session.exec(delete(CardMatchShadow))
        session.commit()


def test_full_loop_fit_certify_route(tmp_path, monkeypatch):
    _seed_shadow(1500)
    rc, out = _run_build(tmp_path, monkeypatch)
    assert rc == 0 and out.exists()

    cal = json.loads(out.read_text())
    # thresholds exist and are ordered sanely around the 60 bar
    assert cal["t_lo"] < cal["bar"] < cal["t_hi"] <= 101.0
    assert cal["n_holdout"] > 200
    assert cal["holdout_decision_agreement"] > 0.85

    # the isotonic table is monotone
    ys = [y for _x, y in cal["isotonic"]]
    assert ys == sorted(ys)

    # routing with the fitted file behaves as certified
    cal_loaded = load_calibration(str(out))
    assert cal_loaded is not None
    top = assign_band(90.0, 92.0, 2.0, [], cal=cal_loaded)
    bottom = assign_band(3.0, 5.0, 2.0, [], cal=cal_loaded)
    assert top.band == AUTO_IN
    assert bottom.band == AUTO_OUT
    # a certified-high score built mostly on inference still goes to Claude
    wide = assign_band(60.0, 92.0, 32.0, [], cal=cal_loaded)
    assert wide.band == BAND


def test_thin_data_yields_all_band_not_false_confidence(tmp_path, monkeypatch):
    _seed_shadow(40)
    rc, out = _run_build(tmp_path, monkeypatch)
    assert rc == 0 and out.exists()
    cal = json.loads(out.read_text())
    # holdout too thin for certificates → both auto zones empty
    assert cal["t_hi"] == 101.0 and cal["t_lo"] == -1.0
    d = assign_band(95.0, 96.0, 1.0, [], cal=json.loads(out.read_text()))
    assert d.band == BAND


def test_uncorrelated_garbage_never_certifies_auto_in(tmp_path, monkeypatch):
    """If g() were junk (no relation to Claude), the Wilson bound must refuse
    to certify an AUTO-IN zone — quality can only fail toward 'more Claude'."""
    rng = random.Random(3)
    with get_session() as session:
        for i in range(1500):
            session.add(CardMatchShadow(
                job_id=i + 1, user_id="u1",
                llm_score=rng.uniform(5, 95),          # unrelated
                direct_score=rng.uniform(5, 95),
                expanded_score=rng.uniform(5, 95),
                spread=2.0, band="band", card_key=f"hash:{i}",
            ))
        session.commit()
    rc, out = _run_build(tmp_path, monkeypatch)
    assert rc == 0
    cal = json.loads(out.read_text())
    # ~17% of random pairs clear the bar — nowhere near a 95% Wilson floor
    assert cal["t_hi"] == 101.0


def test_refuses_below_min_rows_without_force(tmp_path, monkeypatch):
    _seed_shadow(50)
    from scripts import build_calibration
    out = tmp_path / "calibration.json"
    monkeypatch.setattr(sys, "argv", ["build_calibration", "--out", str(out)])
    assert build_calibration.main() == 1
    assert not out.exists()
