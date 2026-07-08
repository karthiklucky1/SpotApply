"""Department-agnostic discovery: every user's own Target Roles / department
keywords drive the title filter, the non-tech gate, and the keyed sources —
no hardcoded software/AI terms deciding for them."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.config import settings
from app.discovery.title_filter import keyword_hit, matches_title
from app.discovery.pipeline import is_obvious_non_tech

CIVIL = ["Civil Engineer", "Structural Engineer", "Geotechnical Engineer"]
MECH = ["Mechanical Engineer", "HVAC Engineer"]
FINANCE = ["Financial Analyst", "Staff Accountant"]


# ── keyword_hit ──────────────────────────────────────────────────────────────

def test_keyword_hit_phrase_and_distinctive_token():
    assert keyword_hit("Senior Civil Engineer", CIVIL)
    assert keyword_hit("Graduate Engineer (Civil)", CIVIL)      # token "civil"
    assert keyword_hit("Structural Design Lead", CIVIL)         # token "structural"
    # Generic tokens never match on their own — "engineer" must not
    # wave every engineering title through.
    assert not keyword_hit("Software Engineer", CIVIL)
    assert not keyword_hit("Sales Manager", CIVIL)


# ── matches_title with department keywords ──────────────────────────────────

def test_civil_user_keeps_civil_titles():
    assert matches_title("Civil Engineer II", CIVIL)
    assert matches_title("Graduate Engineer (Civil)", CIVIL)
    assert matches_title("Geotechnical Engineer - Dams", CIVIL)


def test_default_rules_still_reject_other_departments():
    # No keywords → original software/AI behavior is unchanged.
    assert not matches_title("Civil Engineer II")
    assert not matches_title("Mechanical Design Engineer")
    assert matches_title("Machine Learning Engineer")


def test_junk_titles_stay_rejected_even_with_keyword_token():
    # Sales/support/recruiting are junk for EVERY department.
    assert not matches_title("Sales Engineer - HVAC Systems", MECH)
    assert not matches_title("Customer Success Manager, Construction", CIVIL)


def test_finance_user_keeps_finance_titles():
    assert matches_title("Senior Financial Analyst", FINANCE)
    assert matches_title("Staff Accountant", FINANCE)
    assert not matches_title("Staff Accountant")  # default rules reject


# ── non-tech upsert gate override ────────────────────────────────────────────

def test_non_tech_gate_respects_user_keywords():
    title = "Staff Accountant"
    assert is_obvious_non_tech(title) is True
    # The _upsert gate drops only when BOTH: obvious non-tech AND no keyword hit.
    assert not (is_obvious_non_tech(title) and not keyword_hit(title, FINANCE))
    assert (is_obvious_non_tech(title) and not keyword_hit(title, CIVIL))


# ── keyed sources search with the user's keywords ────────────────────────────

def _fake_async_client(json_body):
    """AsyncClient mock whose get/post record calls and return 200 + json_body."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_body
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


def test_adzuna_searches_user_keywords():
    from app.discovery.sources.adzuna import AdzunaSource
    ctx, client = _fake_async_client({"results": []})
    with patch.object(settings, "adzuna_app_id", "id"), \
         patch.object(settings, "adzuna_app_key", "key"), \
         patch("httpx.AsyncClient", return_value=ctx):
        asyncio.run(AdzunaSource(keywords=CIVIL, country="United States").fetch_jobs())
    searched = [c.kwargs["params"]["what"] for c in client.get.call_args_list]
    assert "civil engineer" in searched
    assert all("machine learning" not in s for s in searched)


def test_jooble_searches_user_keywords():
    from app.discovery.sources.jooble import JoobleSource
    ctx, client = _fake_async_client({"jobs": []})
    with patch.object(settings, "jooble_api_key", "key"), \
         patch("httpx.AsyncClient", return_value=ctx):
        asyncio.run(JoobleSource(keywords=MECH, country="Germany").fetch_jobs())
    searched = [c.kwargs["json"]["keywords"] for c in client.post.call_args_list]
    assert "mechanical engineer" in searched


def test_reed_searches_user_keywords():
    from app.discovery.sources.reed import ReedSource
    ctx, client = _fake_async_client({"results": []})
    with patch.object(settings, "reed_api_key", "key"), \
         patch("httpx.AsyncClient", return_value=ctx):
        asyncio.run(ReedSource(keywords=FINANCE, country="United Kingdom").fetch_jobs())
    searched = [c.kwargs["params"]["keywords"] for c in client.get.call_args_list]
    assert "financial analyst" in searched
