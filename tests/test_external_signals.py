"""Tests for app/matching/external_signals.py — GitHub hiring + Crunchbase funding."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from app.matching.external_signals import (
    _days_since,
    _best_name_match,
    check_github_hiring,
    check_crunchbase_funding,
    _CB_BOOST_6M,
    _CB_BOOST_12M,
    _CB_BOOST_18M,
    _GH_BOOST_FRESH,
    _GH_BOOST_STALE,
    _GH_RECENT_DAYS,
    _GH_STALE_DAYS,
)


class TestDaysSince:
    def test_iso_date(self):
        recent = (datetime.now(tz=timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
        assert abs(_days_since(recent) - 10) <= 1

    def test_epoch_int(self):
        epoch = int((datetime.now(tz=timezone.utc) - timedelta(days=5)).timestamp())
        assert abs(_days_since(epoch) - 5) <= 1

    def test_iso_datetime_z(self):
        s = (datetime.now(tz=timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert abs(_days_since(s) - 3) <= 1

    def test_unparseable_returns_none(self):
        assert _days_since("not-a-date") is None

    def test_partial_year_month(self):
        # "2025-01" parses to 2025-01-01
        days = _days_since("2025-01")
        assert days is not None and days > 0


class TestBestNameMatch:
    def _make(self, name):
        return {"properties": {"name": name}}

    def test_exact_match(self):
        entities = [self._make("OpenAI"), self._make("SomeOtherCo")]
        result = _best_name_match("OpenAI", entities)
        assert result["properties"]["name"] == "OpenAI"

    def test_partial_match(self):
        entities = [self._make("Anthropic Inc"), self._make("Random Co")]
        result = _best_name_match("Anthropic", entities)
        assert "Anthropic" in result["properties"]["name"]

    def test_falls_back_to_first(self):
        entities = [self._make("XYZ Corp")]
        result = _best_name_match("completely different", entities)
        assert result is not None


class TestCheckGitHubHiring:
    def test_no_token_returns_zero(self):
        with patch.dict("os.environ", {}, clear=True):
            boost, signal = check_github_hiring("SomeCompany")
        assert boost == 0.0
        assert signal == ""

    def test_fresh_hiring_file_returns_full_boost(self):
        recent_date = (datetime.now(tz=timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = {"items": [{"login": "acme-ai"}]}

        file_resp = MagicMock(status_code=200)
        file_resp.json.return_value = {
            "commit": {"committer": {"date": recent_date}}
        }

        not_found = MagicMock(status_code=404)

        call_count = [0]

        def _get(url, **kwargs):
            if "search/users" in url:
                return search_resp
            call_count[0] += 1
            if call_count[0] == 1:
                return file_resp
            return not_found

        with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
            with patch("app.matching.external_signals.httpx.get", side_effect=_get):
                boost, signal = check_github_hiring("Acme AI")

        assert boost == _GH_BOOST_FRESH
        assert "fresh" in signal

    def test_stale_hiring_file_returns_stale_boost(self):
        old_date = (datetime.now(tz=timezone.utc) - timedelta(days=_GH_RECENT_DAYS + 10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = {"items": [{"login": "old-corp"}]}

        file_resp = MagicMock(status_code=200)
        file_resp.json.return_value = {
            "commit": {"committer": {"date": old_date}}
        }

        call_count = [0]

        def _get(url, **kwargs):
            if "search/users" in url:
                return search_resp
            call_count[0] += 1
            if call_count[0] == 1:
                return file_resp
            return MagicMock(status_code=404)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
            with patch("app.matching.external_signals.httpx.get", side_effect=_get):
                boost, signal = check_github_hiring("Old Corp")

        assert boost == _GH_BOOST_STALE
        assert "stale" in signal

    def test_no_org_found_returns_zero(self):
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = {"items": []}

        with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
            with patch("app.matching.external_signals.httpx.get", return_value=search_resp):
                boost, signal = check_github_hiring("Unknown Corp")

        assert boost == 0.0

    def test_network_error_returns_zero(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
            with patch("app.matching.external_signals.httpx.get", side_effect=Exception("timeout")):
                boost, signal = check_github_hiring("Any Corp")
        assert boost == 0.0


class TestCheckCrunchbaseFunding:
    def _cb_resp(self, company_name: str, funded_days_ago: int):
        funded_date = (datetime.now(tz=timezone.utc) - timedelta(days=funded_days_ago)).strftime(
            "%Y-%m-%d"
        )
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "entities": [
                {
                    "properties": {
                        "name": company_name,
                        "last_funding_at": funded_date,
                    }
                }
            ]
        }
        return resp

    def test_funded_within_6_months(self):
        with patch("app.matching.external_signals.httpx.get", return_value=self._cb_resp("BetaAI", 60)):
            boost, signal = check_crunchbase_funding("BetaAI")
        assert boost == _CB_BOOST_6M
        assert "crunchbase_funded" in signal

    def test_funded_within_12_months(self):
        with patch("app.matching.external_signals.httpx.get", return_value=self._cb_resp("MidCo", 280)):
            boost, signal = check_crunchbase_funding("MidCo")
        assert boost == _CB_BOOST_12M

    def test_funded_within_18_months(self):
        with patch("app.matching.external_signals.httpx.get", return_value=self._cb_resp("OldFund", 400)):
            boost, signal = check_crunchbase_funding("OldFund")
        assert boost == _CB_BOOST_18M

    def test_funded_too_long_ago_returns_zero(self):
        with patch("app.matching.external_signals.httpx.get", return_value=self._cb_resp("Stale Inc", 600)):
            boost, signal = check_crunchbase_funding("Stale Inc")
        assert boost == 0.0

    def test_no_entities_returns_zero(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"entities": []}
        with patch("app.matching.external_signals.httpx.get", return_value=resp):
            boost, signal = check_crunchbase_funding("Ghost Corp")
        assert boost == 0.0

    def test_http_error_returns_zero(self):
        resp = MagicMock(status_code=429)
        with patch("app.matching.external_signals.httpx.get", return_value=resp):
            boost, signal = check_crunchbase_funding("Any Corp")
        assert boost == 0.0

    def test_network_exception_returns_zero(self):
        with patch("app.matching.external_signals.httpx.get", side_effect=Exception("timeout")):
            boost, signal = check_crunchbase_funding("Boom Corp")
        assert boost == 0.0
