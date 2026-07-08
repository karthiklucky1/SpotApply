"""Registry maintenance must run in prod: seed when empty, validate every
cycle, harvest only on the weekly (every-7th) cycle."""
import asyncio

import pytest


@pytest.fixture
def calls(monkeypatch):
    calls = {"seed": 0, "validate": 0, "harvest": 0}

    def fake_seed():
        calls["seed"] += 1
        return 0

    async def fake_validate(limit=100):
        calls["validate"] += 1
        return 0

    async def fake_harvest(limit=15):
        calls["harvest"] += 1
        return 0

    import app.discovery.registry as registry
    import app.discovery.registry_harvester as harvester
    monkeypatch.setattr(registry, "seed_registry", fake_seed)
    monkeypatch.setattr(registry, "run_validation_loop", fake_validate)
    monkeypatch.setattr(harvester, "run_harvester", fake_harvest)
    return calls


def test_cycle_zero_validates_and_harvests(calls):
    from app.api.server import _registry_maintenance_once
    asyncio.run(_registry_maintenance_once(0))
    assert calls["validate"] == 1
    assert calls["harvest"] == 1


def test_mid_week_cycle_skips_harvester(calls):
    from app.api.server import _registry_maintenance_once
    asyncio.run(_registry_maintenance_once(3))
    assert calls["validate"] == 1
    assert calls["harvest"] == 0
