"""Unit tests for generate_referral_drafts extensions."""
import pytest
from unittest.mock import patch, MagicMock
from sqlmodel import select
from app.db.init_db import get_session
from app.db.models import Job, JobSource, Application, ApplicationStatus, UserProfile
from app.intelligence.referral import generate_referral_drafts


def _seed():
    with get_session() as s:
        # Clean old test runs
        for j in s.exec(select(Job).where(Job.external_id.like("referral-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        # Create user profile if none exists
        prof = s.exec(select(UserProfile).where(UserProfile.user_id == "local")).first()
        if not prof:
            prof = UserProfile(user_id="local", first_name="Karthik", university="University of Cincinnati")
            s.add(prof)
        else:
            prof.university = "University of Cincinnati"
            s.add(prof)
        
        j = Job(source=JobSource.MANUAL, external_id="referral-test-1",
                company="GitHub", title="Software Engineer",
                url="http://x", description="React, Python")
        s.add(j); s.commit(); s.refresh(j)
        a = Application(job_id=j.id, status=ApplicationStatus.SHORTLISTED, user_id="local")
        s.add(a); s.commit(); s.refresh(a)
        return a.id, j.id


def _cleanup():
    with get_session() as s:
        for j in s.exec(select(Job).where(Job.external_id.like("referral-test-%"))).all():
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        s.commit()


def test_generate_referral_drafts_extensions():
    app_id, _ = _seed()
    try:
        mock_people = [
            {"name": "Alice Smith", "headline": "Alum of University of Cincinnati | Ex-Apple", "url": "https://linkedin.com/in/alice"}
        ]
        mock_github_repos = ["cli", "desktop"]

        import app.matching.reranker as rr
        with patch("app.intelligence.linkedin_xray.find_champions", return_value={"ok": True, "people": mock_people}), \
             patch("app.intelligence.referral.get_company_github_repos", return_value=mock_github_repos), \
             patch("app.config.settings.anthropic_api_key", "dummy_key"), \
             patch("anthropic.Anthropic") as mock_anthropic:

            # Referral drafts go through the process-wide shared client pair —
            # force a cold build under the patch (reset again in _cleanup-safe
            # finally below so the mock never leaks into other tests).
            rr._CLIENTS = None
            mock_client = MagicMock()
            mock_anthropic.return_value = mock_client
            mock_message = MagicMock()
            
            mock_message.content = [MagicMock(text='''[
                {"type": "referral_request", "label": "Referral request", "channel": "LinkedIn", "body": "Referral draft"},
                {"type": "hiring_manager", "label": "Hiring-manager note", "channel": "LinkedIn", "body": "Hiring manager draft"},
                {"type": "university_alumni", "label": "University Alumni connection", "channel": "LinkedIn", "body": "Go Cincinnati!"},
                {"type": "github_outreach", "label": "GitHub outreach note", "channel": "LinkedIn", "body": "GitHub draft"}
            ]''')]
            mock_client.messages.create.return_value = mock_message

            res = generate_referral_drafts(app_id, user_id="local")
            
            draft_types = {d["type"] for d in res["drafts"]}
            assert "university_alumni" in draft_types
            assert "github_outreach" in draft_types
            
            univ_draft = next(d for d in res["drafts"] if d["type"] == "university_alumni")
            assert "Cincinnati" in univ_draft["body"]
    finally:
        import app.matching.reranker as rr
        rr._CLIENTS = None   # drop the mock-built shared clients
        _cleanup()


def test_referral_asks_follow_the_ats_mechanics():
    """Guard: the ask ladder, the req link, and no resume on first contact.

    Two mechanical facts drive this. A referrer must select a SPECIFIC LIVE JOB
    (so every draft carries the req link), and must describe their RELATIONSHIP
    to you in writing on the record (so a cold "will you refer me?" gets silence
    and must never be the lead ask). Offering a resume on first contact is the
    other measured mistake. See docs/research/hiring-machine-2026-08.md §1.8.
    """
    from app.intelligence.referral import _fallback_drafts

    drafts = _fallback_drafts(
        name="Ada Lovelace", title="Data Engineer", company="Globex", role="Analytics Engineer",
        skills="python, sql, dbt", selling="", needs_sponsorship=False,
        job_url="https://boards.example.com/globex/jobs/42",
    )
    by_type = {d["type"]: d["body"] for d in drafts}

    # The ladder exists, in descending order of yes.
    for t in ("referral_request", "referral_intro_call", "referral_who_owns"):
        assert t in by_type, f"missing ask: {t}"

    # The lead ask is forward-the-req, which needs no relationship claim.
    assert "forward the req" in by_type["referral_request"].lower()

    # Every referral ask carries the specific requisition link.
    for t in ("referral_request", "referral_intro_call", "referral_who_owns"):
        assert "https://boards.example.com/globex/jobs/42" in by_type[t], f"{t} lost the req link"

    # No resume offered or mentioned on first contact, in any draft.
    for t, body in by_type.items():
        assert "resume" not in body.lower(), f"{t} mentions a resume on first contact"

    # A low-cost ask with concrete windows, not an open-ended one.
    assert "15 minutes" in by_type["referral_intro_call"]
    assert "Wednesday" in by_type["referral_intro_call"]


# ── the GitHub note: a claim about a real company needs evidence ─────────────
#
# Observed 2026-10-14 with every outbound lookup refused: the built-in note
# still told the recipient "I noticed your profile and also saw <company>'s
# active open-source contributions on GitHub". The lookup had found nothing,
# and even a hit is a NAME search for an org — nothing measured activity or
# contributions, or that the org is the company's.

_COMPANY_CLAIMS = ("open-source", "open source", "contributions", "active ")


def _stub_generate(monkeypatch, repos, *, job_title="Backend Engineer",
                   company="Northwind Traders", job_url="https://e.example/j/1",
                   university="", needs_sponsorship=False):
    """generate_referral_drafts with no network, no AI, and a stubbed lookup."""
    from app.intelligence import referral as ref

    class _P:
        first_name, last_name = "Alex", "Rivera"
        current_title, key_skills = "Engineer", "Python, Go"
        work_authorization = "F-1 STEM OPT" if needs_sponsorship else "US Citizen"
        ead_end_date = "2099-01-01" if needs_sponsorship else ""

    _P.university = university

    class _Job:
        corporate_insights = None

    _Job.title, _Job.company, _Job.url = job_title, company, job_url

    class _App:
        job_id = 1

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, model, _id):
            return _App() if model.__name__ == "Application" else _Job()

    from app.autofill import answer_pack as ap
    from app.db import init_db
    from app.intelligence import linkedin_xray as x
    monkeypatch.setattr(init_db, "get_session", lambda *a, **k: _Sess())
    monkeypatch.setattr(ap, "_get_or_create_profile", lambda **k: _P())
    monkeypatch.setattr(ref, "get_company_github_repos", lambda c: list(repos))
    monkeypatch.setattr(x, "find_champions", lambda *a, **k: {"ok": False, "people": []})
    monkeypatch.setattr(__import__("app.config", fromlist=["settings"]).settings,
                        "anthropic_api_key", "", raising=False)
    return ref.generate_referral_drafts(1, user_id=None)


def test_no_github_note_when_the_lookup_found_nothing(monkeypatch):
    """A refused, failed or empty lookup is "we found nothing" — never a reason
    to write about the company's open-source work."""
    from app.intelligence.referral import _github_draft
    assert _github_draft("Northwind Traders", "Backend Engineer", []) is None
    assert _github_draft("Northwind Traders", "Backend Engineer", ["", "  "]) is None

    out = _stub_generate(monkeypatch, [])
    assert "github_outreach" not in {d["type"] for d in out["drafts"]}
    for d in out["drafts"]:
        text = f"{d.get('body', '')} {d.get('channel', '')} {d.get('label', '')}".lower()
        for claim in _COMPANY_CLAIMS:
            assert claim not in text, f"{d['type']} asserts {claim!r} with no evidence"


def test_the_github_note_says_only_what_the_lookup_found(monkeypatch):
    out = _stub_generate(monkeypatch, ["northwind-oss/ledger", "northwind-oss/cli"])
    d = next(d for d in out["drafts"] if d["type"] == "github_outreach")
    body = d["body"].lower()
    assert "ledger" in body
    for claim in _COMPANY_CLAIMS:
        assert claim not in body, f"the note asserts {claim!r}, which nothing measured"
    # The channel label no longer promises "open-source contributions" either.
    assert "open-source" not in d["channel"].lower()
    assert "https://github.com/northwind-oss/ledger" in d["channel"]
    # A name match is not an identity: where we found it, and a check to make.
    assert d["unverified"] is True
    assert "northwind-oss/ledger" in d["verify_before_sending"]
    assert "have not confirmed it belongs to Northwind Traders" in d["verify_before_sending"]
    assert d["evidence"]["url"] == "https://github.com/northwind-oss/ledger"
    assert d["evidence"]["repositories"] == ["northwind-oss/ledger", "northwind-oss/cli"]


def test_the_github_lookup_reports_full_names_and_skips_forks(monkeypatch):
    import httpx

    from app.intelligence import referral as ref

    class _R:
        def __init__(self, code, payload):
            self.status_code, self._payload = code, payload

        def json(self):
            return self._payload

    def _get(url, **kw):
        if "search/users" in url:
            return _R(200, {"items": [{"login": "northwind-oss"}]})
        return _R(200, [{"name": "linux", "full_name": "northwind-oss/linux", "fork": True},
                        {"name": "ledger", "full_name": "northwind-oss/ledger", "fork": False}])

    monkeypatch.setattr(httpx, "get", _get)
    assert ref.get_company_github_repos("Northwind Traders") == ["northwind-oss/ledger"]

    def _refused(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _refused)
    assert ref.get_company_github_repos("Northwind Traders") == []


def test_an_ai_rewrite_changes_words_never_the_set_of_drafts():
    """The model's copy replaced the list wholesale: it dropped the verify step
    and could hand back a GitHub note for a company where nothing was found."""
    from app.intelligence.referral import _merge_rewrites
    built = [
        {"type": "referral_request", "label": "L", "channel": "LinkedIn", "body": "orig"},
        {"type": "university_alumni", "label": "A", "channel": "LinkedIn", "body": "orig alum",
         "unverified": True, "verify_before_sending": "Confirm this person..."},
    ]
    rewrites = [
        {"type": "referral_request", "body": "rewritten"},
        {"type": "university_alumni", "body": "alum rewritten"},
        {"type": "github_outreach", "body": "I saw your active open-source contributions"},
    ]
    merged = _merge_rewrites(built, rewrites)
    assert [d["type"] for d in merged] == ["referral_request", "university_alumni"]
    assert merged[0]["body"] == "rewritten"
    assert merged[1]["body"] == "alum rewritten"
    assert merged[1]["unverified"] is True
    assert merged[1]["verify_before_sending"] == "Confirm this person..."
    # A missing or empty rewrite keeps the built-in words.
    assert _merge_rewrites(built, [{"type": "referral_request", "body": "  "}])[0]["body"] == "orig"


# ── LinkedIn's 300-character note limit, met with no AI ──────────────────────
#
# Observed 2026-10-14 with no API keys: every built-in LinkedIn note ran 350-415
# characters and the kit marked it in red "over LinkedIn's 300". The built-in
# notes are what a user gets whenever the AI is unavailable, so they must fit on
# their own — for the long titles, company names and ATS links real postings
# have, not only for "Acme" and "Engineer".

_WORKDAY_URL = ("https://northwindtraders.wd5.myworkdayjobs.com/en-US/Northwind_External_Careers"
                "/job/Austin-TX/Senior-Staff-Software-Engineer--Distributed-Systems_R-2024-104857")
_LONG_CASES = [
    pytest.param("Senior Staff Software Engineer, Distributed Systems Infrastructure",
                 "Northwind Traders Global Holdings Corporation", _WORKDAY_URL,
                 "Massachusetts Institute of Technology", id="workday-link"),
    pytest.param("Principal Machine Learning Engineer, Search Ranking and Recommendations (Remote, US)",
                 "International Consolidated Logistics Group",
                 "https://jobs.lever.co/consolidatedlogistics/8f1c2b3a-1234-4abc-9def-0123456789ab",
                 "University of California, Los Angeles", id="lever-link"),
    pytest.param("Registered Nurse, Cardiovascular Intensive Care Unit (CVICU) - Night Shift",
                 "Cincinnati Children's Hospital Medical Center", "",
                 "University of Cincinnati", id="no-link"),
    pytest.param("Senior Backend Engineer", "Lumen Data",
                 "https://boards.greenhouse.io/lumendata/jobs/4821093",
                 "University of Cincinnati", id="short"),
]
_NOTE_TYPES = {"referral_request", "referral_intro_call", "referral_who_owns",
               "visa_alumni", "university_alumni", "github_outreach"}


@pytest.mark.parametrize("job_title,company,job_url,university", _LONG_CASES)
def test_built_in_linkedin_notes_fit_linkedins_limit(monkeypatch, job_title, company,
                                                     job_url, university):
    from app.intelligence import outreach as o
    out = _stub_generate(monkeypatch, ["northwind-oss/ledger-reconciliation-service"],
                         job_title=job_title, company=company, job_url=job_url,
                         university=university, needs_sponsorship=True)
    kit = o.build_kit(out["drafts"], company=company, role=job_title, job_url=job_url)
    notes = [d for d in kit["drafts"] if d["channel_kind"] == "linkedin"]
    assert {d["type"] for d in notes} == _NOTE_TYPES, "every built-in LinkedIn note is checked"
    for d in notes:
        assert d["over_limit"] is False, f"{d['type']} is {d['chars']} chars: {d['body']}"
        assert d["chars"] <= o.LINKEDIN_NOTE_LIMIT
        # ...and still fits once the user types a real first name over the placeholder.
        named = d["body"].replace("{name}", "Alexandra-Marie").replace("{Alumni Name}",
                                                                       "Alexandra-Marie")
        assert len(named) <= o.LINKEDIN_NOTE_LIMIT, d["type"]

    by_type = {d["type"]: d["body"] for d in notes}
    for t in ("referral_request", "referral_intro_call", "referral_who_owns"):
        # A referrer must select a specific live job: the link when there is one,
        # otherwise the role's own name — a shorter note never loses the job.
        assert (job_url in by_type[t]) if job_url else (job_title in by_type[t]), t
    assert "forward the req" in by_type["referral_request"].lower()
    assert "15 minutes" in by_type["referral_intro_call"]
    assert "Wednesday" in by_type["referral_intro_call"]
    for body in by_type.values():
        assert "resume" not in body.lower()


def test_short_inputs_keep_the_full_wording():
    """The ladder only trims what does not fit; a short posting gets the whole note."""
    from app.intelligence.referral import _fallback_drafts
    drafts = _fallback_drafts(name="Ada", title="", company="Globex", role="Data Engineer",
                              skills="", selling="", needs_sponsorship=False, job_url="")
    who = next(d["body"] for d in drafts if d["type"] == "referral_who_owns")
    assert who == ("Hi {name}, quick question if you have a second — do you know who owns "
                   "the Data Engineer req at Globex? Happy to take it from there myself.")


def test_a_link_too_long_for_any_note_is_kept_and_flagged_not_dropped():
    """The req link is load-bearing, so no rung drops it. A link longer than any
    connection note can carry leaves the note over the limit — and the kit's
    counter says so honestly rather than the draft quietly losing the job."""
    from app.intelligence import outreach as o
    from app.intelligence.referral import _fallback_drafts
    url = "https://careers.example.com/jobs/" + "x" * 250
    drafts = _fallback_drafts(name="", title="", company="Globex", role="Data Engineer",
                              skills="", selling="", needs_sponsorship=False, job_url=url)
    kit = o.build_kit(drafts, company="Globex", role="Data Engineer", job_url=url)
    ask = next(d for d in kit["drafts"] if d["type"] == "referral_request")
    assert url in ask["body"]
    assert ask["over_limit"] is True


def test_an_ai_rewrite_that_breaks_a_hard_rule_keeps_the_built_in_words():
    from app.intelligence.referral import _merge_rewrites
    url = "https://boards.example.com/globex/jobs/42"
    built = [{"type": "referral_request", "channel": "LinkedIn / email to a connection",
              "body": f"Hi {{name}}, would you forward the req? {url}"},
             {"type": "hiring_manager", "channel": "LinkedIn DM / email to the hiring manager",
              "body": "Hi {name}, a short email."}]
    too_long = [{"type": "referral_request", "body": "Hi {name}, " + "a" * 300 + f" {url}"}]
    assert _merge_rewrites(built, too_long)[0]["body"] == built[0]["body"]
    lost_link = [{"type": "referral_request", "body": "Hi {name}, would you forward the req?"}]
    assert _merge_rewrites(built, lost_link)[0]["body"] == built[0]["body"]
    # An email has no note limit.
    long_email = [{"type": "hiring_manager", "body": "Hi {name}, " + "word " * 120}]
    assert _merge_rewrites(built, long_email)[1]["body"].startswith("Hi {name}, word")
