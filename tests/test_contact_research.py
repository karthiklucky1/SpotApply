""""We have no data" must never render as "0ms, 0 failures, worked fine".

Phase 8 asked for observed elapsed time and cost per stage of contact research,
with successful, empty, failed and timed-out runs reported SEPARATELY. On
2026-09-25 no log could support that: `linkedin_xray.find_champions` logged only
on exception, `referral.generate_referral_drafts` logged nothing at all, and no
counter recorded how many searches ran, how many came back empty, or how long
any of it took. A report built on that would have been inventing numbers.

FOUR STATES THAT ARE NOT THE SAME, and this file keeps them apart:

    implemented: false        the stage was never built — `relevance_verification`
    observed_runs>0, no timing  attempted, but no provider was called (no key)
    by_outcome.empty          ran, succeeded, found nobody — common, not a failure
    by_outcome.ok             returned at least one candidate

And one fabrication. The alumni draft told a REAL, NAMED person "I noticed you
also went to <university> and now work at <company> as a <role>" — where `role`
is the title of the job THE USER IS APPLYING FOR, not anything about the
recipient, and the university came from a substring match against a Google
snippet. Two invented claims about a real person, in a message the product then
invited the user to send.

No test here sends anything, and nothing in this module may.
"""
from __future__ import annotations

import pytest

from app.intelligence import contact_research as cr


@pytest.fixture(autouse=True)
def _clean_counters():
    cr.snapshot(reset=True)
    yield
    cr.snapshot(reset=True)


# ── the four states ──────────────────────────────────────────────────────────

def test_a_stage_that_was_never_built_says_so():
    """`relevance_verification` reporting "0 runs, 0 failures" would read as a
    stage that ran perfectly. Nothing checks that a person found still holds the
    role their snippet implied, and the report has to say that."""
    stage = cr.snapshot()["stages"][cr.RELEVANCE_VERIFICATION]
    assert stage["implemented"] is False
    assert stage["measured"] is False
    assert "not implemented" in stage["note"]


def test_the_implemented_stages_are_marked_implemented():
    stages = cr.snapshot()["stages"]
    for name in (cr.JOB_VALIDATION, cr.CONTACT_SEARCH, cr.OUTPUT_GENERATION):
        assert stages[name]["implemented"] is True


def test_every_stage_phase_8_names_is_present():
    assert set(cr.snapshot()["stages"]) == {
        "job_validation", "contact_search", "relevance_verification",
        "output_generation"}


def test_an_attempt_with_no_provider_call_yields_no_timing():
    """THE CONFUSION PHASE 8 NAMES. Fifty runs that all returned "no API key"
    made no call and took no measurable time. Reporting that as measured, at
    0ms, claims a capability the product does not have."""
    for _ in range(50):
        cr.record(cr.CONTACT_SEARCH, cr.NOT_CONFIGURED)
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["observed_runs"] == 50
    assert s["by_outcome"]["not_configured"] == 50
    assert s["measured"] is False
    assert s["timed_runs"] == 0
    assert s["elapsed_ms_avg"] is None, "None is not 0.0 — we have no timing"
    assert s["elapsed_ms_max"] is None
    assert s["provider_calls"] == 0


def test_empty_is_not_failure_and_not_success():
    """"Ran fine, found nobody" is the most common real result of a contact
    search. Filing it under either neighbour makes the report useless."""
    cr.record(cr.CONTACT_SEARCH, cr.EMPTY, elapsed_ms=340, provider_call=True)
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["by_outcome"] == {"ok": 0, "empty": 1, "failed": 0, "timeout": 0,
                               "quota": 0, "not_configured": 0}
    assert s["measured"] is True and s["elapsed_ms_avg"] == 340.0
    assert s["provider_calls"] == 1


@pytest.mark.parametrize("outcome", list(cr.OUTCOMES))
def test_each_outcome_is_counted_on_its_own_line(outcome):
    cr.record(cr.CONTACT_SEARCH, outcome, elapsed_ms=10)
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["by_outcome"][outcome] == 1
    assert sum(s["by_outcome"].values()) == 1


def test_latency_is_an_average_over_timed_runs_only():
    cr.record(cr.CONTACT_SEARCH, cr.OK, elapsed_ms=100, provider_call=True)
    cr.record(cr.CONTACT_SEARCH, cr.OK, elapsed_ms=300, provider_call=True)
    cr.record(cr.CONTACT_SEARCH, cr.NOT_CONFIGURED)          # no timing
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["observed_runs"] == 3 and s["timed_runs"] == 2
    assert s["elapsed_ms_avg"] == 200.0
    assert s["elapsed_ms_max"] == 300


def test_one_run_is_not_reported_as_a_delivery_estimate():
    """Phase 8: do not turn one run into a reliable estimate. The snapshot
    reports the SAMPLE SIZE beside every number so a reader cannot miss it."""
    cr.record(cr.CONTACT_SEARCH, cr.OK, elapsed_ms=5000, provider_call=True)
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["timed_runs"] == 1
    assert s["elapsed_ms_avg"] == 5000.0
    assert s["elapsed_ms_max"] == 5000


def test_any_measurement_is_false_until_something_is_actually_timed():
    cr.record(cr.CONTACT_SEARCH, cr.NOT_CONFIGURED)
    snap = cr.snapshot()
    assert snap["any_observation"] is True
    assert snap["any_measurement"] is False


# ── reason → outcome mapping ─────────────────────────────────────────────────

@pytest.mark.parametrize("reason,expected", [
    ("serpapi_key_not_set", cr.NOT_CONFIGURED),
    ("no_company", cr.NOT_CONFIGURED),
    ("serpapi_invalid_key", cr.NOT_CONFIGURED),
    ("serpapi_quota", cr.QUOTA),
    ("ReadTimeout", cr.TIMEOUT),
    ("ConnectTimeout", cr.TIMEOUT),
    ("http_503", cr.FAILED),
    ("", cr.FAILED),
    ("something nobody has seen before", cr.FAILED),
])
def test_reasons_map_to_the_right_outcome(reason, expected):
    assert cr.outcome_for(reason) == expected


def test_an_unknown_reason_is_a_failure_not_a_success():
    """The safe direction: an unrecognised reason is not evidence it worked."""
    assert cr.outcome_for("¯\\_(ツ)_/¯") == cr.FAILED


# ── the timed context manager ────────────────────────────────────────────────

def test_the_timer_records_an_exception_as_a_failure():
    with pytest.raises(ValueError):
        with cr.timed(cr.CONTACT_SEARCH):
            raise ValueError("boom")
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["by_outcome"]["failed"] == 1
    assert s["measured"] is True, "a failed call still took time"


def test_the_timer_never_swallows_an_exception():
    with pytest.raises(KeyError):
        with cr.timed(cr.OUTPUT_GENERATION):
            raise KeyError("x")


def test_the_timer_defaults_to_failure_not_success():
    """A body that returns without setting an outcome has not proved anything."""
    with cr.timed(cr.CONTACT_SEARCH):
        pass
    assert cr.snapshot()["stages"][cr.CONTACT_SEARCH]["by_outcome"]["failed"] == 1


def test_recording_never_raises():
    cr.record("not_a_stage", cr.OK)
    cr.record(cr.CONTACT_SEARCH, "not_an_outcome")
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["by_outcome"]["failed"] == 1, "an unknown outcome files as failed"


# ── the search itself ────────────────────────────────────────────────────────

def test_an_unconfigured_search_is_recorded_as_not_configured(monkeypatch):
    from app.config import settings
    from app.intelligence.linkedin_xray import find_champions
    monkeypatch.setattr(settings, "serpapi_key", "", raising=False)
    res = find_champions("Acme", "Backend Engineer")
    assert res["ok"] is False and res["reason"] == "serpapi_key_not_set"
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["by_outcome"]["not_configured"] == 1
    assert s["measured"] is False


def test_a_search_that_finds_nobody_is_recorded_as_empty(monkeypatch):
    from app.config import settings
    import app.intelligence.linkedin_xray as x
    monkeypatch.setattr(settings, "serpapi_key", "key", raising=False)

    class _R:
        status_code = 200
        def json(self): return {"organic_results": []}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _R()

    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        type("m", (), {"Client": lambda *a, **k: _C()}))
    res = x.find_champions("Acme", "Backend Engineer")
    assert res["ok"] is True and res["people"] == []
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["by_outcome"]["empty"] == 1
    assert s["by_outcome"]["ok"] == 0
    assert s["measured"] is True
    assert s["provider_calls"] == 1


def test_a_quota_refusal_is_not_a_failure(monkeypatch):
    """A provider refusing on quota is recoverable and says nothing about
    whether contacts exist — filing it as a failure would overstate breakage."""
    from app.config import settings
    import app.intelligence.linkedin_xray as x
    monkeypatch.setattr(settings, "serpapi_key", "key", raising=False)

    class _R:
        status_code = 429
        def json(self): return {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _R()

    monkeypatch.setitem(__import__("sys").modules, "httpx",
                        type("m", (), {"Client": lambda *a, **k: _C()}))
    x.find_champions("Acme", "Backend Engineer")
    s = cr.snapshot()["stages"][cr.CONTACT_SEARCH]
    assert s["by_outcome"]["quota"] == 1 and s["by_outcome"]["failed"] == 0


def test_the_search_never_logs_a_provider_message_verbatim():
    """A provider's exception text can contain the query, which contains the
    employer. The log names the exception TYPE."""
    import inspect
    import app.intelligence.linkedin_xray as x
    src = inspect.getsource(x)
    assert 'log.warning("X-Ray search failed (%s)", type(e).__name__)' in src


# ── the fabrication ──────────────────────────────────────────────────────────

def _drafts_with_alum(monkeypatch, headline="MS, Ohio State University · Acme"):
    """Build the alumni draft against a stubbed search result."""
    import app.intelligence.referral as ref

    class _P:
        first_name, last_name = "Alex", "Rivera"
        current_title, key_skills = "Engineer", "Python, Go"
        university = "Ohio State University"

    class _Job:
        title, company, url = "Senior Backend Engineer", "Acme", "http://e/1"
        corporate_insights = None

    class _App:
        job_id = 1

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, model, _id):
            return _App() if model.__name__ == "Application" else _Job()

    import app.db.init_db as init_db
    monkeypatch.setattr(init_db, "get_session", lambda *a, **k: _Sess())
    import app.autofill.answer_pack as ap
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda **k: _P())
    monkeypatch.setattr(ref, "get_company_github_repos", lambda c: [])
    import app.intelligence.linkedin_xray as x
    monkeypatch.setattr(x, "find_champions", lambda *a, **k: {
        "ok": True, "people": [{"name": "Jane Doe", "headline": headline,
                                "url": "https://linkedin.com/in/janedoe"}]})
    monkeypatch.setattr(__import__("app.config", fromlist=["settings"]).settings,
                        "anthropic_api_key", "", raising=False)
    out = ref.generate_referral_drafts(1, user_id=None)
    return {d["type"]: d for d in out["drafts"]}


def test_the_alumni_draft_no_longer_asserts_the_recipients_job_title(monkeypatch):
    """THE FABRICATION. It told a real named person they "now work at Acme as a
    Senior Backend Engineer" — which is the title of the job the USER is
    applying for, asserted about someone else from a search snippet."""
    d = _drafts_with_alum(monkeypatch)["university_alumni"]
    assert "now work at" not in d["body"]
    assert "as a Senior Backend Engineer" not in d["body"]
    assert "Jane" in d["body"]


def test_the_alumni_draft_does_not_assert_where_they_studied(monkeypatch):
    """A snippet mentioning a university is not proof anyone attended it."""
    d = _drafts_with_alum(monkeypatch)["university_alumni"]
    assert "you also went to" not in d["body"]


def test_the_alumni_draft_ships_its_evidence_and_a_verify_step(monkeypatch):
    d = _drafts_with_alum(monkeypatch)["university_alumni"]
    assert d["unverified"] is True
    assert "Confirm this person actually went to" in d["verify_before_sending"]
    contact = d["suggested_contact"]
    assert contact["name"] == "Jane Doe"
    assert contact["evidence_type"] == "public_search_snippet"
    # The snippet VERBATIM, so the user can judge the match themselves.
    assert "Ohio State University" in contact["evidence"]


def test_no_contact_is_invented_when_the_search_returns_nobody(monkeypatch):
    import app.intelligence.referral as ref
    d = _drafts_with_alum(monkeypatch, headline="nothing relevant here")
    alum = d["university_alumni"]
    assert "suggested_contact" not in alum
    assert "{Alumni Name}" in alum["body"], "a placeholder, never a guessed name"
    assert ref  # keep the import meaningful


def test_draft_generation_is_measured(monkeypatch):
    _drafts_with_alum(monkeypatch)
    s = cr.snapshot()["stages"][cr.OUTPUT_GENERATION]
    assert s["by_outcome"]["ok"] == 1
    assert s["measured"] is True and s["results_total"] >= 4


# ── nothing here sends anything ──────────────────────────────────────────────

def test_nothing_in_the_contact_path_sends_a_message():
    """Phase 8: do not send outreach. Drafts are returned to the user, who sends
    them from their own account."""
    import inspect
    import app.intelligence.referral as ref
    src = inspect.getsource(ref)
    for sender in ("smtplib", "sendgrid", "ses.send", "send_email(",
                   "requests.post", "send_message("):
        assert sender not in src


def test_the_drafts_say_plainly_that_the_user_sends_them(monkeypatch):
    import app.intelligence.referral as ref
    class _P:
        first_name = last_name = current_title = key_skills = ""
        university = ""
    class _Job:
        title, company, url, corporate_insights = "Eng", "Acme", "", None
    class _App:
        job_id = 1
    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, model, _id):
            return _App() if model.__name__ == "Application" else _Job()
    import app.db.init_db as init_db
    monkeypatch.setattr(init_db, "get_session", lambda *a, **k: _Sess())
    import app.autofill.answer_pack as ap
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda **k: _P())
    monkeypatch.setattr(ref, "get_company_github_repos", lambda c: [])
    monkeypatch.setattr(__import__("app.config", fromlist=["settings"]).settings,
                        "anthropic_api_key", "", raising=False)
    out = ref.generate_referral_drafts(1, user_id=None)
    assert "never sends these for you" in out["note"]


# ── the admin surface ────────────────────────────────────────────────────────

def test_the_admin_route_is_admin_only_and_names_no_one():
    import inspect
    import app.api.server as server
    src = inspect.getsource(server.admin_contact_research)
    assert "_require_admin_user" in src
    assert "serpapi_configured" in src, "configured-ness, never the key"
    assert "settings.serpapi_key," not in src, "the key itself must never be returned"
    assert '"on_hold"' in src, "Phase 8 keeps the feature on hold"


def test_no_identifier_can_reach_a_metric_label():
    """These are real people who did not ask to be in our metrics — the same
    rule `liveness` follows."""
    import inspect
    src = inspect.getsource(cr)
    for leak in ("user_id", "external_id", "profile_url", "candidate", "email"):
        assert f'record({leak}' not in src
    # The only things that key a counter are the stage and the outcome.
    assert '_counts[f"{stage}.{outcome}"]' in src


def test_the_metrics_module_makes_no_network_or_db_call():
    import inspect
    src = inspect.getsource(cr)
    for forbidden in ("httpx", "requests", "get_session", "urlopen", "socket."):
        assert forbidden not in src
