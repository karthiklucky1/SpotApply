"""One writer for tailor usage and tailor spend, and one generation at a time.

Audit 2026-09-16: user_usage.tailor_count summed to 15 all-time while the spend
ledger held 3 tailor calls — record_llm_spend(uid, "tailor") sat after
_increment_tailor at two of the four tailor entry points and was missing from
the others. And one application was tailored twice within three minutes, two
credits charged: no door asked whether a generation was already running.

Rows this file writes carry the `tailortest-` prefix and are removed by it.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlmodel import delete, select

from app.api import server
from app.db.init_db import get_session
from app.db.models import LlmSpend, UserUsage

_UID = "tailortest-u1"


@pytest.fixture(autouse=True)
def _clean():
    def _wipe():
        with get_session() as s:
            s.exec(delete(LlmSpend).where(LlmSpend.user_id.like("tailortest-%")))
            s.exec(delete(UserUsage).where(UserUsage.user_id.like("tailortest-%")))
            s.commit()
        server._release_tailor(9_990_001)
    _wipe()
    yield
    _wipe()


def _usage() -> int:
    with get_session() as s:
        row = s.exec(select(UserUsage).where(UserUsage.user_id == _UID,
                                             UserUsage.usage_date == date.today())).first()
        return row.tailor_count if row else 0


def _ledger() -> int:
    with get_session() as s:
        rows = s.exec(select(LlmSpend).where(LlmSpend.user_id == _UID,
                                             LlmSpend.kind == "tailor",
                                             LlmSpend.day == date.today())).all()
        return sum(r.calls for r in rows)


def test_usage_and_ledger_move_together():
    assert (_usage(), _ledger()) == (0, 0)
    server._increment_tailor(_UID)
    assert (_usage(), _ledger()) == (1, 1)
    server._increment_tailor(_UID)
    assert (_usage(), _ledger()) == (2, 2)


def test_a_failing_ledger_never_fails_the_delivered_tailor(monkeypatch):
    import app.analytics.spend as spend

    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(spend, "record_llm_spend", boom)
    server._increment_tailor(_UID)          # must not raise
    assert _usage() == 1


def test_the_claim_admits_one_generation_per_application():
    app_id = 9_990_001
    assert server._claim_tailor(app_id) is True
    assert server._claim_tailor(app_id) is False
    assert server._tailor_in_flight(app_id) is True
    server._release_tailor(app_id)
    assert server._claim_tailor(app_id) is True
    server._release_tailor(app_id)


def test_a_second_settle_for_an_application_already_generating_pays_nothing(monkeypatch):
    app_id = 9_990_001
    calls = {"n": 0}

    def fake_tailor(application_id, instruction=None):
        calls["n"] += 1
        return ("/tmp/x.docx", "/tmp/y.docx")

    monkeypatch.setattr("app.tailoring.tailor.tailor_for_application", fake_tailor)
    assert server._claim_tailor(app_id)                 # the first door holds it
    assert server._tailor_and_settle(app_id, _UID) is False
    assert calls["n"] == 0 and _usage() == 0 and _ledger() == 0
    # The loser must not release a claim it never held — the first door's
    # generation is still in flight until IT releases.
    assert server._tailor_in_flight(app_id) is True
    server._release_tailor(app_id)
    assert server._tailor_in_flight(app_id) is False
