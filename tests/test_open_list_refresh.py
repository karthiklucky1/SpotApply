"""New boards arrive a bounded batch at a time, and never "due now".

The open company list keeps growing, and the registry only read it while it was
small: on 2026-11-15 about 33,000 companies on job systems we already scrape
were in the list and not in the registry. Loading them is the cheapest way to
more jobs — and the easiest way to hurt the fresh ones: the pulse lane polls a
board that was never scheduled FIRST, so a few thousand boards inserted with no
schedule are all due at once and take every polling slot, for hours, from the
boards that are posting.

So one run adds at most a cap (per 24 hours, counted in the registry, so
several deploys in a day do not each add a batch), shared round-robin across
the systems, and every new board gets a first poll time spread over a window.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete, select

from app.config import settings
from app.db.init_db import get_session, init_db
from app.db.models import CompanyRegistry, JobSource
from app.discovery import registry

_P = "openlist-"


def _csv(n, tag):
    return "name,slug,url\n" + "".join(
        f"Co {tag}{i},{_P}{tag}{i},https://example.test/{tag}{i}\n" for i in range(n))


class _Client:
    """Serves greenhouse.csv and lever.csv; every other file is empty."""
    files = {"greenhouse.csv": _csv(10, "g"), "lever.csv": _csv(3, "l")}

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, url):
        name = url.rsplit("/", 1)[-1]
        body = self.files.get(name, "name,slug,url\n")

        class _R:
            status_code = 200
            text = body
        return _R()


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    init_db()
    monkeypatch.setattr(registry.httpx, "Client", _Client)
    monkeypatch.setattr(settings, "open_slug_dataset_base", "https://example.test/list", raising=False)
    yield
    with get_session() as s:
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like(f"{_P}%")))
        s.commit()


def _mine():
    with get_session() as s:
        return s.exec(select(CompanyRegistry).where(
            CompanyRegistry.slug.like(f"{_P}%"))).all()


def test_a_run_adds_at_most_its_cap_shared_across_systems():
    assert registry.seed_registry_from_open_datasets(max_new=6, spread_hours=24) == 6
    by_ats: dict = {}
    for r in _mine():
        by_ats[r.ats] = by_ats.get(r.ats, 0) + 1
    assert by_ats == {JobSource.GREENHOUSE: 3, JobSource.LEVER: 3}, (
        "round-robin: the biggest file must not take the whole batch")
    # The next run carries on where this one stopped, and never re-adds.
    assert registry.seed_registry_from_open_datasets(max_new=100, spread_hours=24) == 7
    assert registry.seed_registry_from_open_datasets(max_new=100, spread_hours=24) == 0
    assert len(_mine()) == 13


def test_a_new_board_is_never_due_now():
    """Never-scheduled boards are polled first: thousands of them at once would
    take every slot from the boards that are posting."""
    before = datetime.utcnow()
    registry.seed_registry_from_open_datasets(max_new=13, spread_hours=24)
    rows = _mine()
    assert all(r.next_poll_at is not None for r in rows)
    assert all(before <= r.next_poll_at <= before + timedelta(hours=24, minutes=1) for r in rows)
    span = max(r.next_poll_at for r in rows) - min(r.next_poll_at for r in rows)
    assert span > timedelta(hours=1), "the first polls must be SPREAD, not stacked"
    assert all(r.source == "open_dataset" and r.is_active for r in rows)


def test_without_a_spread_the_old_behaviour_is_unchanged():
    registry.seed_registry_from_open_datasets()
    rows = _mine()
    assert len(rows) == 13 and all(r.next_poll_at is None for r in rows)


def test_a_cap_of_zero_adds_nothing():
    assert registry.seed_registry_from_open_datasets(max_new=0, spread_hours=24) == 0
    assert _mine() == []


def test_the_daily_refresh_counts_what_the_day_already_added(monkeypatch):
    """Maintenance runs five minutes after every deploy. Three deploys in a day
    must not add three batches."""
    import app.api.server as srv
    monkeypatch.setattr(settings, "open_dataset_seed_enabled", True, raising=False)
    monkeypatch.setattr(settings, "open_dataset_spread_hours", 24, raising=False)
    with get_session() as s:
        already = s.exec(select(CompanyRegistry).where(
            CompanyRegistry.source == "open_dataset",
            CompanyRegistry.first_seen >= datetime.utcnow() - timedelta(hours=24))).all()
    base = len(already)                       # other files' rows, if any
    monkeypatch.setattr(settings, "open_dataset_max_new_per_run", base + 5, raising=False)
    assert srv._refresh_open_dataset() == 5
    assert srv._refresh_open_dataset() == 0, "the day's cap is already spent"
    monkeypatch.setattr(settings, "open_dataset_max_new_per_run", base + 8, raising=False)
    assert srv._refresh_open_dataset() == 3
    monkeypatch.setattr(settings, "open_dataset_max_new_per_run", 0, raising=False)
    assert srv._refresh_open_dataset() == 0, "0 = never add boards on a schedule"
