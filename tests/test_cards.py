"""app/matching/cards.py — the module that spends money and caches cross-tenant.

It had no tests, which hid two defects:

  * Neither cache reader compared the stored row's `version` to the current
    JOB_CARD_VERSION / USER_CARD_VERSION. Bumping either constant — which you do
    exactly when the card shape changes — kept serving old-shape payloads
    forever, so the version fields were decorative.
  * `uid = user_id or "local"` mapped every None-owner job onto the local /
    founder UserCard row. In Supabase mode that reads one identity's compiled
    résumé card for another user's match and writes their material back over it —
    the same shape as the founder-résumé leak in ARCHITECTURE §7.1, in a new
    module. NULL-owner Job rows exist, so it was reachable.

Everything here stubs the LLM. A test that mints for real would bill the account.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.config import settings
from app.db.init_db import get_session
from app.db.models import JobCardRow, UserCardRow
from app.matching import cards


def _set_supabase(monkeypatch, on: bool) -> None:
    """settings.use_supabase is a derived property — drive its inputs."""
    monkeypatch.setattr(settings, "database_url",
                        "postgresql://x/y" if on else "", raising=False)
    monkeypatch.setattr(settings, "supabase_url",
                        "https://x.supabase.co" if on else "", raising=False)
    assert settings.use_supabase is on


def _job(**kw):
    base = dict(title="Backend Engineer", company="CardCo", location="Remote",
                remote=True, description="Python, Postgres, Kafka. " * 20,
                cross_source_slug=None, content_hash=None,
                source="greenhouse", external_id="card-1")
    base.update(kw)
    return SimpleNamespace(**base)


def _profile(**kw):
    base = dict(requires_sponsorship=False, work_authorization="US Citizen",
                visa_status="", preferred_country="United States", remote_ok=True,
                open_to_relocation=False, location="Austin, TX")
    base.update(kw)
    return SimpleNamespace(**base)


_JOB_PAYLOAD = {"capabilities": [{"name": "python", "importance": 0.9}],
                "years_min": 3, "country": "united states",
                "remote_policy": "remote", "confidence": {}}
_USER_PAYLOAD = {"skills": [{"name": "python", "evidence": 0.9, "basis": "production"}],
                 "years_experience": 6, "effective_level": "senior"}


@pytest.fixture(autouse=True)
def _clean_cards():
    """Card rows are shared across tenants by design, so leftovers are contagious."""
    def _wipe():
        with get_session() as s:
            for row in s.exec(select(JobCardRow)).all():
                s.delete(row)
            for row in s.exec(select(UserCardRow)).all():
                s.delete(row)
            s.commit()
    _wipe()
    yield
    _wipe()


@pytest.fixture
def stub_llm(monkeypatch):
    """Count calls so 'served from cache' is provable, not inferred."""
    calls = {"n": 0}

    def _fake(system, user, max_tokens=900):
        calls["n"] += 1
        return dict(_USER_PAYLOAD if "UserCard" in system else _JOB_PAYLOAD)

    monkeypatch.setattr(cards, "_haiku_json", _fake)
    monkeypatch.setattr("app.matching.reranker.llm_budget_exhausted", lambda: False)
    return calls


# ── mint caps ────────────────────────────────────────────────────────────────

def test_mint_cap_stops_minting_and_never_calls_the_llm_past_it(stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "card_mint_daily_cap", 2, raising=False)
    assert cards.mint_job_card(_job()) is not None
    assert cards.mint_job_card(_job()) is not None
    assert cards.mint_job_card(_job()) is None, "third mint exceeded the daily cap"
    assert stub_llm["n"] == 2, "the capped call must not reach the LLM at all"


def test_budget_exhaustion_blocks_minting_before_the_llm(stub_llm, monkeypatch):
    """Prescores are cheap, not free — every lane checks the budget before Tier-1."""
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    monkeypatch.setattr("app.matching.reranker.llm_budget_exhausted", lambda: True)
    assert cards._mint_allowed() is False
    assert cards.mint_job_card(_job()) is None
    assert stub_llm["n"] == 0


def test_a_malformed_mint_response_is_rejected_and_not_counted(monkeypatch):
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    monkeypatch.setattr("app.matching.reranker.llm_budget_exhausted", lambda: False)
    monkeypatch.setattr(cards, "_haiku_json", lambda *a, **k: {"capabilities": "not-a-list"})
    assert cards.mint_job_card(_job()) is None
    assert cards._mints_today["count"] == 0, "a rejected card must not consume the cap"


# ── JobCard cache ────────────────────────────────────────────────────────────

def test_job_card_is_served_from_cache_on_the_second_read(stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    job = _job(content_hash="abc123")
    first = cards.get_or_mint_job_card(job)
    assert first is not None and stub_llm["n"] == 1
    second = cards.get_or_mint_job_card(job)
    assert second == first
    assert stub_llm["n"] == 1, "second read re-minted instead of using the shared card"


def test_a_stale_version_job_card_is_re_minted(stub_llm, monkeypatch):
    """THE bug: the reader ignored row.version, so a schema bump changed nothing."""
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    job = _job(content_hash="stale1")
    key = cards.job_card_key(job)
    with get_session() as s:
        s.add(JobCardRow(card_key=key, version=cards.JOB_CARD_VERSION - 1,
                         model="old", payload='{"capabilities": [], "_shape": "old"}'))
        s.commit()

    got = cards.get_or_mint_job_card(job)
    assert got is not None
    assert got.get("_shape") != "old", "an old-schema card was served after a version bump"
    assert stub_llm["n"] == 1
    with get_session() as s:
        row = s.exec(select(JobCardRow).where(JobCardRow.card_key == key)).first()
        assert row.version == cards.JOB_CARD_VERSION


def test_allow_mint_false_never_spends(stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    assert cards.get_or_mint_job_card(_job(content_hash="nope"), allow_mint=False) is None
    assert stub_llm["n"] == 0


def test_job_card_key_precedence_is_slug_then_hash_then_external_id():
    """All tenants' copies of one posting must collapse onto ONE key, or the
    'understand once' premise (and the mint cap) stops holding."""
    assert cards.job_card_key(_job(cross_source_slug="s1", content_hash="h1")) == "slug:s1"
    assert cards.job_card_key(_job(cross_source_slug=None, content_hash="h1")) == "hash:h1"
    assert cards.job_card_key(
        _job(cross_source_slug=None, content_hash=None)) == "ext:greenhouse:card-1"
    # Two tenants' rows for the same posting share a key.
    a = _job(content_hash="same", external_id="tenant-a-row")
    b = _job(content_hash="same", external_id="tenant-b-row")
    assert cards.job_card_key(a) == cards.job_card_key(b)


# ── UserCard cache + identity ────────────────────────────────────────────────

def test_user_card_recompiles_when_the_resume_changes(stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    p = _profile()
    cards.get_or_compile_user_card("tenant-a", p, "resume one")
    assert stub_llm["n"] == 1
    cards.get_or_compile_user_card("tenant-a", p, "resume one")
    assert stub_llm["n"] == 1, "unchanged material should hit the cache"
    cards.get_or_compile_user_card("tenant-a", p, "resume two — different")
    assert stub_llm["n"] == 2, "changed material must recompile"


def test_a_stale_version_user_card_is_recompiled(stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    p, text = _profile(), "resume one"
    with get_session() as s:
        s.add(UserCardRow(user_id="tenant-a", version=cards.USER_CARD_VERSION - 1,
                          model="old", resume_hash=cards.resume_hash(p, text),
                          payload='{"skills": [], "_shape": "old"}'))
        s.commit()
    got = cards.get_or_compile_user_card("tenant-a", p, text)
    assert got is not None and got.get("_shape") != "old"
    assert stub_llm["n"] == 1


def test_a_missing_user_id_never_resolves_to_the_local_identity(stub_llm, monkeypatch):
    """In multi-tenant mode a None uid is a bug, not an invitation to read the
    founder's card. Previously `user_id or "local"` served exactly that."""
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    _set_supabase(monkeypatch, True)
    # Seed a real card for the local identity so a fallback would be detectable.
    p, text = _profile(), "the founder's résumé"
    _set_supabase(monkeypatch, False)
    local_card = cards.get_or_compile_user_card(None, p, text)
    assert local_card is not None

    _set_supabase(monkeypatch, True)
    leaked = cards.get_or_compile_user_card(None, _profile(), "some tenant's résumé")
    assert leaked is None, "a None user_id was served the 'local' identity's UserCard"
    with get_session() as s:
        rows = s.exec(select(UserCardRow)).all()
        assert [r.user_id for r in rows] == ["local"], (
            "a None user_id wrote a card row; it must refuse instead")


def test_local_mode_still_uses_the_local_identity(stub_llm, monkeypatch):
    """The fallback is legitimate for single-user SQLite dev — keep it working."""
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    _set_supabase(monkeypatch, False)
    assert cards.get_or_compile_user_card(None, _profile(), "resume") is not None
    with get_session() as s:
        assert s.exec(select(UserCardRow)).first().user_id == "local"


def test_two_tenants_get_separate_user_cards(stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    cards.get_or_compile_user_card("tenant-a", _profile(), "résumé A")
    cards.get_or_compile_user_card("tenant-b", _profile(), "résumé B")
    with get_session() as s:
        assert {r.user_id for r in s.exec(select(UserCardRow)).all()} == {"tenant-a", "tenant-b"}


# ── deterministic profile passthrough ────────────────────────────────────────

def test_profile_facts_ride_along_verbatim_and_never_come_from_the_llm(stub_llm, monkeypatch):
    """Work authorization and location are decided by the profile, not the model."""
    monkeypatch.setattr(settings, "card_mint_daily_cap", 100, raising=False)
    p = _profile(work_authorization="Green Card", visa_status="Active Secret clearance",
                 requires_sponsorship=False, preferred_country="United States")
    card = cards.compile_user_card(p, "some résumé")
    assert card is not None
    prof = card["_profile"]
    assert prof["work_authorization"] == "Green Card"
    # card_match reads visa_status alongside work_authorization; a clearance
    # recorded only there was invisible to the matcher before it was passed on.
    assert prof["visa_status"] == "Active Secret clearance"
    assert prof["requires_sponsorship"] is False
    assert prof["preferred_country"] == "united states"


def test_resume_hash_is_stable_and_material_sensitive():
    p = _profile()
    assert cards.resume_hash(p, "abc") == cards.resume_hash(_profile(), "abc")
    assert cards.resume_hash(p, "abc") != cards.resume_hash(p, "abd")


# The cache key covered only the LLM prompt material, so nothing in this list
# invalidated a stored card — yet card_match's work_auth and location factors read
# from these fields and nowhere else. A user who got a green card, or switched
# preferred country, kept being gated on their old facts indefinitely, because
# their résumé text had not changed.
@pytest.mark.parametrize("field,value", [
    ("work_authorization", "Green Card"),
    ("visa_status", "Active Secret clearance"),
    ("requires_sponsorship", True),
    ("preferred_country", "Germany"),
    ("remote_ok", False),
    ("open_to_relocation", True),
    ("location", "Berlin, Germany"),
])
def test_changing_a_deterministic_profile_fact_invalidates_the_card(field, value):
    before = cards.resume_hash(_profile(), "same résumé text")
    after = cards.resume_hash(_profile(**{field: value}), "same résumé text")
    assert before != after, (
        f"changing {field} left the cache key unchanged, so the stored UserCard "
        f"— and the work_auth/location gates that read it — stay stale forever"
    )


def test_the_card_payload_and_the_cache_key_read_the_same_facts():
    """One helper feeds both, so what the card carries and what invalidates it
    cannot drift apart."""
    p = _profile(work_authorization="Green Card", visa_status="clearance")
    facts = cards.profile_facts(p)
    assert facts["work_authorization"] == "Green Card"
    assert facts["visa_status"] == "clearance"
    for field in facts:
        # Every fact in the payload must move the hash.
        moved = cards.resume_hash(p, "t") != cards.resume_hash(
            _profile(**{**vars(p), field: _flip(facts[field])}), "t")
        assert moved, f"{field} is carried on the card but absent from its cache key"


def _flip(v):
    if isinstance(v, bool):
        return not v
    return "something-else" if v != "something-else" else "other"


def test_a_none_profile_is_tolerated():
    """Cards are minted before a profile exists (fresh signup, résumé-only)."""
    assert cards.profile_facts(None) == {}
    assert cards.resume_hash(None, "abc") == cards.resume_hash(None, "abc")
    assert cards.resume_hash(None, "abc") != cards.resume_hash(None, "abd")
