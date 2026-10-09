"""A closed posting is detected when a user opens it (live test 2026-10-09).

Two closed Greenhouse postings, Hasbro "Applied AI Engineer" and Accela
"Senior Software Engineer - AI Platform", stayed on the owner's board after he
opened them. Production rows (job_liveness): both filed WRONG_PAGE,
"redirected_to_index", HTTP 200. Greenhouse answers a closed posting by
redirecting to the board's own index (``/hasbro?error=true``, "The job you are
looking for is no longer open.") and draws that message in the browser, so the
page body never said it; and WRONG_PAGE is not a death, so /verify said
"active". A Riot Games posting shown on the employer's own site (``?gh_jid=``)
went the same way: the employer page cannot speak for the vacancy at all.

Pinned here:
- a hosted Greenhouse posting that redirects to its own board index is REMOVED
  (classify, the delivery gate, /api/jobs/{id}/verify and the HEAD check);
- a posting the page cannot speak for (``gh_jid`` on the employer's site,
  stored or reached by a redirect from a hosted URL, or a redirect the page
  check could not place) is asked of the ATS's own API: 200 is LIVE, 404 is
  REMOVED only when the board itself answers 200;
- 429 / 403 / a timeout / an unknown board token close nothing;
- no DB session is open during any request, and metrics carry no identifiers;
- the Removed tab counts every removed job, not the 20 the pane renders.

Synthetic rows only: job external ids start with ``99002610`` (Greenhouse ids
are numeric, and the gh_jid in a URL must equal the id), every other key with
``cpdtest-``, and each is removed by that prefix.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

import pytest
from sqlmodel import delete, select

from app.db import init_db
from app.db.init_db import get_session
from app.db.models import (
    Application, ApplicationStatus, CompanyRegistry, FunnelEvent, Job, JobLiveness,
    JobLivenessState, JobSource, PlatformCounter, UserProfile,
)
from app.discovery import liveness as lv
from app.matching.preference_learning import _is_user_dismissal
from app.strategy import delivery_gate as gate

_P = "cpdtest-"
_N = "99002610"          # numeric prefix: Greenhouse posting ids are digits
LIVE, REMOVED, WRONG = (JobLivenessState.LIVE.value, JobLivenessState.REMOVED.value,
                        JobLivenessState.WRONG_PAGE.value)

HASBRO = "https://job-boards.greenhouse.io/hasbro/jobs/4250645009"
HASBRO_INDEX = "https://job-boards.greenhouse.io/hasbro?error=true"
BOARD_BODY = ("<html><title>Jobs at Hasbro</title><h1>Current openings at Hasbro</h1>"
              "<a href='/hasbro/jobs/1'>Senior Engineer</a></html>")


def _user_tables():
    """Every table with a text ``user_id`` column, children first: loading
    /dashboard as a test user writes that user's UserProfile, and whatever a
    page or route starts writing per user later is covered the same way."""
    from sqlalchemy import String
    from sqlmodel import SQLModel

    def _text(col) -> bool:     # sqlmodel's AutoString decorates a VARCHAR
        return isinstance(getattr(col.type, "impl", col.type), String)
    return [t for t in reversed(SQLModel.metadata.sorted_tables)
            if "user_id" in t.c and _text(t.c.user_id)]


def _wipe():
    with get_session() as s:
        jids = list(s.exec(select(Job.id).where(
            Job.external_id.like(f"{_N}%") | Job.external_id.like(f"{_P}%"))).all())
        if jids:
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(Job).where(Job.id.in_(jids)))
        s.exec(delete(JobLiveness).where(
            JobLiveness.external_id.like(f"{_N}%") | JobLiveness.external_id.like(f"{_P}%")))
        # "_" is LIKE's one-character wildcard: this also takes "cpdtest_two".
        s.exec(delete(CompanyRegistry).where(CompanyRegistry.slug.like("cpdtest_%")))
        # Rows our test users own outright (UserProfile from /dashboard, ...).
        # "cpdtest-" has no LIKE wildcard in it, so nobody else's rows match.
        for table in _user_tables():
            s.execute(table.delete().where(table.c.user_id.like(f"{_P}%")))
        # The report allowance, whatever day it was reserved on.
        s.exec(delete(PlatformCounter).where(
            PlatformCounter.name.like(f"liveness_report:user:{_P}%")))
        s.commit()


@pytest.fixture(autouse=True)
def _clean():
    _wipe()
    gate.metrics_snapshot(reset=True)
    yield
    _wipe()
    gate.metrics_snapshot(reset=True)


def _job(ext, user_id, *, url, company="Acme", source=JobSource.GREENHOUSE,
         status=ApplicationStatus.SHORTLISTED, title=None):
    with get_session() as s:
        j = Job(source=source, external_id=ext, company=company, url=url,
                title=title or f"Engineer {user_id or 'local'} {ext}", user_id=user_id,
                description="d", rerank_score=80.0)
        s.add(j)
        s.commit()
        s.refresh(j)
        aid = None
        if status is not None:
            a = Application(job_id=j.id, user_id=user_id, status=status)
            s.add(a)
            s.commit()
            s.refresh(a)
            aid = a.id
        return j.id, aid


def _board(slug):
    with get_session() as s:
        s.add(CompanyRegistry(slug=slug, ats=JobSource.GREENHOUSE, is_active=False,
                              source="test"))
        s.commit()


@pytest.fixture
def sessions(monkeypatch):
    """Sessions open at any moment (route, gate and liveness all use
    init_db.get_session)."""
    from app.api import server
    real = init_db.get_session
    count = [0]

    @contextmanager
    def counting():
        count[0] += 1
        try:
            with real() as s:
                yield s
        finally:
            count[0] -= 1
    monkeypatch.setattr(init_db, "get_session", counting)
    monkeypatch.setattr(server, "get_session", counting)
    return count


def _web(monkeypatch, routes: dict, sessions=None) -> list:
    """A fake internet: url -> (status, final_url, body, error). Records each
    request with the number of sessions open while it was made."""
    calls = []

    def _fake(url, timeout):
        calls.append((url, sessions[0] if sessions is not None else None))
        if url not in routes:
            raise AssertionError(f"unexpected request: {url}")
        status, final, body, error = routes[url]
        return status, (final or url), body, error
    monkeypatch.setattr(gate, "_fetch", _fake)
    return calls


def _verify(jid):
    from fastapi.testclient import TestClient
    from app.api.server import app
    return TestClient(app).post(f"/api/jobs/{jid}/verify")


def _app(aid):
    with get_session() as s:
        return s.get(Application, aid)


def _liveness(ext):
    with get_session() as s:
        return s.exec(select(JobLiveness).where(JobLiveness.external_id == ext)).first()


# ══════════════════════════════════════════════════════════════════════════
# 1. Greenhouse's "no longer open" redirect
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("requested,final", [
    # Hasbro, as the tester saw it.
    (HASBRO, HASBRO_INDEX),
    # Accela: the same redirect, no error flag kept.
    ("https://job-boards.greenhouse.io/accela/jobs/8109291",
     "https://job-boards.greenhouse.io/accela"),
    # The legacy host hops to job-boards first.
    ("https://boards.greenhouse.io/hasbro/jobs/4250645009", HASBRO_INDEX),
    ("https://job-boards.greenhouse.io/Hasbro/jobs/4250645009/",
     "https://job-boards.greenhouse.io/hasbro/"),
    ("https://job-boards.eu.greenhouse.io/acme/jobs/123",
     "https://job-boards.eu.greenhouse.io/acme?error=true"),
])
def test_a_redirect_to_the_greenhouse_board_index_is_removed(requested, final):
    state, reason = lv.classify(200, requested_url=requested, final_url=final, body=BOARD_BODY)
    assert (state, reason) == (REMOVED, "greenhouse_redirect_to_board")


@pytest.mark.parametrize("requested,final,expected", [
    # (A redirect to the employer's own `gh_jid` page is NOT here: the page
    # alone cannot say the posting is open. See
    # test_a_hosted_posting_sent_to_the_employers_page_is_asked_of_greenhouse.)
    # A different posting on the same board is not the board index.
    (HASBRO, "https://job-boards.greenhouse.io/hasbro/jobs/4250699999", WRONG),
    # Somebody else's error flag is not Greenhouse's.
    (HASBRO, "https://example.com/hasbro?error=true", WRONG),
    # Not a Greenhouse posting at all: the generic rule still applies.
    ("https://acme.com/careers/12345", "https://acme.com/careers", WRONG),
    # No redirect: the posting page itself.
    (HASBRO, HASBRO, LIVE),
])
def test_other_redirects_are_not_read_as_greenhouse_closing(requested, final, expected):
    state, _ = lv.classify(200, requested_url=requested, final_url=final,
                           body="<h1>Senior Engineer</h1> Apply now")
    assert state == expected


def test_opening_the_closed_hasbro_posting_takes_it_off_every_waiting_board(monkeypatch, sessions):
    """The tester's case end to end, with the response Greenhouse gives."""
    calls = _web(monkeypatch, {HASBRO: (200, HASBRO_INDEX, BOARD_BODY, None)}, sessions)
    ext = _N + "01"
    jid, aid = _job(ext, None, url=HASBRO, company="Hasbro")
    _other, other_aid = _job(ext, _P + "other", url=HASBRO, company="Hasbro")
    _tailored, t_aid = _job(ext, _P + "busy", url=HASBRO, company="Hasbro",
                            status=ApplicationStatus.TAILORED)

    d = _verify(jid).json()

    assert d["active"] is False and d["removed"] is True
    assert calls == [(HASBRO, 0)], "one request, and no DB session open during it"
    for a in (aid, other_aid):
        assert _app(a).status == ApplicationStatus.SKIPPED
        assert not _is_user_dismissal(_app(a)), "a closed job is not 'not interested'"
    assert _app(t_aid).status == ApplicationStatus.TAILORED, "work in progress keeps its place"
    row = _liveness(ext)
    assert (row.state, row.reason) == (REMOVED, "greenhouse_redirect_to_board")


def test_the_report_recheck_confirms_it_too(monkeypatch):
    """"Job closed?" on Hasbro: the re-check used to read WRONG_PAGE and leave
    the other copies on their boards."""
    _web(monkeypatch, {HASBRO: (200, HASBRO_INDEX, BOARD_BODY, None)})
    ext = _N + "02"
    _jid, other_aid = _job(ext, _P + "other", url=HASBRO, company="Hasbro")
    assert gate.verify_reported(JobSource.GREENHOUSE, ext, HASBRO) == "confirmed_dead"
    assert _app(other_aid).status == ApplicationStatus.SKIPPED


def test_the_head_check_sees_the_board_redirect(monkeypatch):
    """`check_job_alive` (aggregator copies of an ATS link) followed the same
    redirect and only knew "/careers"."""
    from app.discovery import verify as v

    class _R:
        status_code = 200
        url = HASBRO_INDEX

    monkeypatch.setattr(v, "guarded_request", lambda client, method, url: (_R(), None))
    alive, why = v.check_job_alive(HASBRO)
    assert alive is False and "closed" in why and "—" not in why
    _R.url = HASBRO          # the posting itself answered
    assert v.check_job_alive(HASBRO) == (True, "")


# ══════════════════════════════════════════════════════════════════════════
# 2. The ATS's own API, where the page cannot speak
# ══════════════════════════════════════════════════════════════════════════

def _riot(ext):
    return f"https://www.riotgames.com/en/work-with-us/job/{ext}?gh_jid={ext}"


def _api(slug, ext=None):
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
    return f"{base}/jobs/{ext}" if ext else base


def test_an_employer_site_posting_is_asked_of_greenhouse(monkeypatch, sessions):
    """Riot Games shows its Greenhouse postings on its own site. That page
    cannot say whether the job is open; Greenhouse's posting API can."""
    slug, ext = _P + "riot", _N + "03"
    _board(slug)
    url = _riot(ext)
    calls = _web(monkeypatch, {
        _api(slug, ext): (404, None, "", None),
        _api(slug): (200, None, "", None),
        url: (200, "https://www.riotgames.com/en/work-with-us", "<h1>Work with us</h1>", None),
    }, sessions)
    jid, aid = _job(ext, None, url=url, company="Cpdtest Riot")

    d = _verify(jid).json()

    assert d["active"] is False and d["removed"] is True
    assert [u for u, _ in calls] == [_api(slug, ext), _api(slug)], \
        "the API answered, so the employer page is never fetched"
    assert all(n == 0 for _u, n in calls), "a DB session was open during a request"
    assert _app(aid).status == ApplicationStatus.SKIPPED
    assert (_liveness(ext).state, _liveness(ext).reason) == (REMOVED, "ats_api_404")


def test_an_open_employer_site_posting_stays(monkeypatch):
    slug, ext = _P + "riot", _N + "04"
    _board(slug)
    calls = _web(monkeypatch, {_api(slug, ext): (200, None, "", None)})
    jid, aid = _job(ext, None, url=_riot(ext), company="Cpdtest Riot")
    assert _verify(jid).json() == {"active": True}
    assert len(calls) == 1 and _app(aid).status == ApplicationStatus.SHORTLISTED
    assert _liveness(ext).state == LIVE


@pytest.mark.parametrize("api_status,board_status,error", [
    (429, None, None),          # rate limited
    (403, None, None),          # blocked
    (503, None, None),
    (None, None, "ReadTimeout"),
    (404, 404, None),           # the board token is wrong or moved: no evidence
    (404, 429, None),
])
def test_an_api_that_cannot_answer_closes_nothing(monkeypatch, api_status, board_status, error):
    slug, ext = _P + "riot", _N + "05"
    _board(slug)
    url = _riot(ext)
    calls = _web(monkeypatch, {
        _api(slug, ext): (api_status, None, "", error),
        _api(slug): (board_status, None, "", None),
        url: (200, None, "<h1>Engineer</h1> Apply", None),
    })
    jid, aid = _job(ext, None, url=url, company="Cpdtest Riot")
    assert _verify(jid).json() == {"active": True}
    assert calls[-1][0] == url, "no answer from the API: the page decides, as before"
    assert _app(aid).status == ApplicationStatus.SHORTLISTED


def test_without_one_known_board_the_api_is_not_asked(monkeypatch):
    """Two registered slugs name the same company, or none does: guessing a
    board would let a 404 from the WRONG board close a live job."""
    ext = _N + "06"
    _board("cpdtest-two")
    _board("cpdtest_two")
    url = _riot(ext)
    calls = _web(monkeypatch, {url: (200, None, "<h1>Engineer</h1>", None)})
    jid, _aid = _job(ext, None, url=url, company="Cpdtest Two")
    assert _verify(jid).json() == {"active": True}
    jid2, _ = _job(_N + "07", None, url=_riot(_N + "07"), company="Cpdtest Nobody")
    _web(monkeypatch, {_riot(_N + "07"): (200, None, "<h1>Engineer</h1>", None)})
    assert _verify(jid2).json() == {"active": True}
    assert [u for u, _ in calls] == [url]


def test_a_gh_jid_for_another_posting_is_not_asked_about(monkeypatch):
    slug, ext = _P + "riot", _N + "08"
    _board(slug)
    url = _riot(_N + "99")                        # the URL names a different id
    calls = _web(monkeypatch, {url: (200, None, "<h1>Engineer</h1>", None)})
    jid, _aid = _job(ext, None, url=url, company="Cpdtest Riot")
    assert _verify(jid).json() == {"active": True}
    assert [u for u, _ in calls] == [url]


def test_the_board_token_lookup_reverses_the_scrapers_naming():
    """The lookup works only while the scraper names companies this way."""
    from app.discovery.greenhouse import board_company_name
    _board(_P + "riot-games")
    with get_session() as s:
        s.add(Job(source=JobSource.GREENHOUSE, external_id=_N + "09",
                  company=board_company_name(_P + "riot-games"), title="t", url="u"))
        s.commit()
    assert gate._greenhouse_token_for(_N + "09") == _P + "riot-games"


def test_a_redirect_the_page_could_not_place_is_settled_by_the_api(monkeypatch):
    """Lever: the posting redirected to the board's home. The page check says
    WRONG_PAGE; Lever's own API says whether the posting still exists."""
    ext = _P + "lever1"
    uuid = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
    url = f"https://jobs.lever.co/{_P}acme/{uuid}"
    api = "https://api.lever.co/v0/postings"
    calls = _web(monkeypatch, {
        url: (200, f"https://jobs.lever.co/{_P}acme", "<h1>Open roles</h1>", None),
        f"{api}/{_P}acme/{uuid}": (404, None, "", None),
        f"{api}/{_P}acme?limit=1&mode=json": (200, None, "", None),
    })
    state, how = gate.verify_for_delivery(JobSource.LEVER, ext, url)
    assert (state, how) == (REMOVED, "checked") and len(calls) == 3

    _web(monkeypatch, {
        url: (200, f"https://jobs.lever.co/{_P}acme", "<h1>Open roles</h1>", None),
        f"{api}/{_P}acme/{uuid}": (200, None, "", None),
    })
    state, _ = gate.verify_for_delivery(JobSource.LEVER, _P + "lever2", url)
    assert state == LIVE


HASBRO_ON_SITE = "https://careers.hasbro.com/job?gh_jid=4250645009"
HASBRO_API = "https://boards-api.greenhouse.io/v1/boards/hasbro/jobs/4250645009"
HASBRO_BOARD_API = "https://boards-api.greenhouse.io/v1/boards/hasbro"


@pytest.mark.parametrize("api,board,expected,reason", [
    # Closed: the employer's shell answered 200, Greenhouse says 404, and the
    # board itself answers, so the 404 is about this posting.
    ((404, None, "", None), (200, None, "", None), REMOVED, "ats_api_404"),
    ((410, None, "", None), (200, None, "", None), JobLivenessState.EXPIRED.value,
     "ats_api_410"),
    # Open: Greenhouse still publishes it.
    ((200, None, "{}", None), None, LIVE, "ats_api_200"),
    # No answer: the page's verdict stands, exactly as before.
    ((404, None, "", None), (404, None, "", None), LIVE, "http_200"),   # token moved
    ((429, None, "", None), None, LIVE, "http_200"),
    ((403, None, "", None), None, LIVE, "http_200"),
    ((None, None, "", "ReadTimeout"), None, LIVE, "http_200"),
])
def test_a_hosted_posting_sent_to_the_employers_page_is_asked_of_greenhouse(
        monkeypatch, sessions, api, board, expected, reason):
    """Reviewer's edge case: a hosted Greenhouse URL that redirects to the
    employer's own ``gh_jid`` page keeps the id, so the page reads LIVE. That
    page is the same employer shell `_ats_target` refuses to trust when it is
    the stored URL, so it must not settle LIVE here either."""
    page_says, _ = lv.classify(200, requested_url=HASBRO, final_url=HASBRO_ON_SITE,
                               body="<h1>Applied AI Engineer</h1> Apply now")
    assert page_says == LIVE, "the page alone reads it open; the gate must not stop there"

    routes = {HASBRO: (200, HASBRO_ON_SITE, "<h1>Applied AI Engineer</h1> Apply now", None),
              HASBRO_API: api}
    if board is not None:
        routes[HASBRO_BOARD_API] = board
    calls = _web(monkeypatch, routes, sessions)
    ext = _N + "13"

    state, how = gate.verify_for_delivery(JobSource.GREENHOUSE, ext, HASBRO)

    assert (state, how) == (expected, "checked")
    assert [u for u, _ in calls][:2] == [HASBRO, HASBRO_API], "page first, then the ATS"
    assert len(calls) <= gate._MAX_REQUESTS_PER_CHECK
    assert all(n == 0 for _u, n in calls), "a DB session was open during a request"
    row = _liveness(ext)
    assert (row.state, row.reason) == (expected, reason)


def test_a_hop_between_greenhouse_hosts_still_costs_one_request(monkeypatch):
    """boards.greenhouse.io → job-boards.greenhouse.io ends on the posting page
    itself, which does speak (a closed one goes to the board index): no API."""
    legacy = "https://boards.greenhouse.io/hasbro/jobs/4250645009"
    calls = _web(monkeypatch, {legacy: (200, HASBRO, "<h1>Applied AI Engineer</h1>", None)})
    assert gate.verify_for_delivery(JobSource.GREENHOUSE, _N + "14", legacy)[0] == LIVE
    assert [u for u, _ in calls] == [legacy]


def test_a_page_that_speaks_for_itself_costs_one_request(monkeypatch):
    """The API is consulted only where the page cannot answer, so the common
    case stays one request per check."""
    calls = _web(monkeypatch, {HASBRO: (200, None, "<h1>Applied AI Engineer</h1>", None)})
    assert gate.verify_for_delivery(JobSource.GREENHOUSE, _N + "10", HASBRO)[0] == LIVE
    assert len(calls) == 1


def test_api_metrics_carry_no_identifiers(monkeypatch):
    slug, ext = _P + "riot", _N + "11"
    _board(slug)
    _web(monkeypatch, {_api(slug, ext): (404, None, "", None), _api(slug): (200, None, "", None)})
    _job(ext, None, url=_riot(ext), company="Cpdtest Riot")
    gate.verify_for_delivery(JobSource.GREENHOUSE, ext, _riot(ext))
    snap = gate.metrics_snapshot()
    assert snap["ats_api_checks"] == 1 and snap[f"ats_api:greenhouse:{REMOVED}"] == 1
    for key in snap:
        assert ext not in key and _P not in key and "http" not in key


def test_an_api_target_never_leaves_the_ats_host():
    """The board token comes from a stored URL; it may only ever become a path
    segment on the ATS's own API host."""
    assert gate._ats_target("https://job-boards.greenhouse.io/a%2F..%2Fx/jobs/1") is None
    t = gate._ats_target("https://boards.greenhouse.io/embed/job_app?for=acme&token=123")
    assert t.posting_url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"
    assert gate._ats_target("https://boards.greenhouse.io/embed/job_app?for=a/b&token=1") is None
    assert gate._ats_target("https://evil.example/jobs?gh_jid=123") is None   # no id given


# ══════════════════════════════════════════════════════════════════════════
# 3. The Removed tab counts what was removed
# ══════════════════════════════════════════════════════════════════════════

_TAB = re.compile(r"switchPipeTab\('pipe-skipped', this\)\">Removed <span class=\"pt-count\">(\d+)</span>")


def test_reporting_a_job_closed_moves_the_removed_count(monkeypatch):
    """The owner reported Hasbro closed; it left the board and "Removed" stayed
    at 20 — the length of the 20-row pane, for an account with 2,700."""
    from fastapi.testclient import TestClient
    from app.api import server
    me = _P + "me"
    monkeypatch.setattr(server, "_get_user_id", lambda request: me)
    _web(monkeypatch, {HASBRO: (200, None, "<h1>Applied AI Engineer</h1>", None)})
    for i in range(21):
        _job(f"{_P}gone{i:02d}", me, url=f"https://x.example/jobs/{i}",
             status=ApplicationStatus.SKIPPED)
    _jid, aid = _job(_N + "12", me, url=HASBRO, company="Hasbro")
    c = TestClient(server.app)

    before = c.get("/dashboard")
    assert before.status_code == 200
    assert int(_TAB.search(before.text).group(1)) == 21
    assert c.post(f"/application/{aid}/unavailable").json()["removed"] is True
    after = c.get("/dashboard").text
    assert int(_TAB.search(after).group(1)) == 22
    assert re.search(r'id="skipped-count">22<', after), "the pane says the same number"
    assert after.count('id="skipped-card-') == 20, "the pane still renders the latest 20"


def test_the_cleanup_takes_what_the_dashboard_wrote(monkeypatch):
    """Reviewer: loading /dashboard as a ``cpdtest-`` user wrote its
    UserProfile, and `_wipe()` left that row in the shared test DB for every
    file that ran after this one."""
    from fastapi.testclient import TestClient
    from sqlalchemy import func
    from app.api import server
    me = _P + "me"
    monkeypatch.setattr(server, "_get_user_id", lambda request: me)
    assert TestClient(server.app).get("/dashboard").status_code == 200
    with get_session() as s:
        assert s.exec(select(UserProfile.user_id).where(UserProfile.user_id == me)).first(), \
            "premise: the dashboard writes the viewer's profile"

    _wipe()

    with get_session() as s:
        assert s.exec(select(UserProfile.user_id).where(
            UserProfile.user_id.like(f"{_P}%"))).first() is None
        for table in _user_tables():
            left = s.execute(select(func.count()).select_from(table).where(
                table.c.user_id.like(f"{_P}%"))).scalar()
            assert left == 0, f"{table.name} still holds a {_P} row"
