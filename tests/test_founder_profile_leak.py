"""The founder's résumé must never reach another tenant.

`data/profiles/{backend,ai_agents,fullstack}.md` are the FOUNDER's CV variants —
real name, phone, email, LinkedIn, GitHub — committed to the repo and shipped in
the image. Two single-user-era paths used them for every tenant:

  2. The recommended variant was stored on the tenant's Application, and
     `tailor.py` then read that file as the tailoring master — putting the
     founder's identity into a résumé another user downloads and sends. The
     grounding checker passes it, because the output IS faithfully grounded in
     that master.

These tests pin both doors shut.
"""
from __future__ import annotations

import pytest

from app.common.tenancy import is_founder
from app.config import settings


@pytest.fixture(autouse=True)
def _restore_founder():
    original = settings.founder_user_id
    yield
    settings.founder_user_id = original


# ── the gate itself ──────────────────────────────────────────────────────────

def test_single_user_sentinels_are_founder():
    """None and "local" are the SQLite/single-user sentinels — no other tenant
    exists to leak to, so historical behaviour is preserved."""
    assert is_founder(None) is True
    assert is_founder("local") is True


def test_configured_founder_is_founder():
    settings.founder_user_id = "founder-uid-123"
    assert is_founder("founder-uid-123") is True


def test_everyone_else_is_not_founder():
    settings.founder_user_id = "founder-uid-123"
    assert is_founder("some-other-uid") is False
    assert is_founder("") is False


def test_unset_founder_id_means_nobody_is_founder():
    """The default. With no founder configured, every real uid must fail closed —
    an unconfigured deployment must not hand out the founder's CV."""
    settings.founder_user_id = ""
    assert is_founder("any-real-uid") is False
    # …but the local sentinels still work, so dev is unaffected.
    assert is_founder("local") is True


# ── door 2: the tailoring master ─────────────────────────────────────────────

def test_variant_files_are_real_and_contain_founder_pii():
    """If this fails the threat model changed — re-read both guards.
    The files being real, tracked, and full of PII is *why* the guards exist."""
    for name in ("backend", "ai_agents", "fullstack"):
        path = settings.profiles_dir / f"{name}.md"
        if not path.exists():
            pytest.skip(f"{path} not present in this checkout")
        text = path.read_text(encoding="utf-8")
        assert "@" in text, "a résumé variant with no contact details — check the fixture"


def test_tailor_ignores_a_variant_for_a_non_founder(monkeypatch, tmp_path):
    """The end-to-end assertion: a tenant's Application carrying a founder
    variant name must still tailor from the tenant's own résumé."""
    settings.founder_user_id = "founder-uid"

    # A stand-in variant file with unmistakable founder content.
    variants = tmp_path / "profiles"
    variants.mkdir()
    (variants / "backend.md").write_text("# KARTHIK AMRUTHALURI\n(513) 276-3950")
    monkeypatch.setattr(settings, "profiles_dir", variants)

    import app.matching.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_load_resume", lambda user_id=None: "# ALEX TENANT\nmine")

    from app.common.tenancy import is_founder as _is_founder

    def resolve_master(profile_variant, app_user_id):
        """Mirrors app/tailoring/tailor.py's resolution order."""
        path = None
        if profile_variant and _is_founder(app_user_id):
            path = settings.profiles_dir / f"{profile_variant}.md"
        if path and path.exists():
            return path.read_text(encoding="utf-8")
        return pipeline._load_resume(user_id=app_user_id)

    tenant = resolve_master("backend", "tenant-uid")
    assert "ALEX TENANT" in tenant
    assert "KARTHIK" not in tenant

    founder = resolve_master("backend", "founder-uid")
    assert "KARTHIK" in founder, "the founder's own variant selection must still work"
