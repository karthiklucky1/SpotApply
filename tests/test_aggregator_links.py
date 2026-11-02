"""The board delivers jobs SpotApply can apply to — never a job board's page.

Incident 2026-11-02: a new user's board held Deepgram's "Research Engineer, ML
Systems" as a jobicy.com link (a listing page, no form) while the shared pool
had held Deepgram's own Ashby posting for 24 days. Her one Auto-Fill opened
jobicy.com and filled nothing; her All Jobs was half Adzuna/Jooble/Reed pages
that ask you to register first. 92 of ~500 board deliveries in 14 days were
links like that.

Pinned here:
  * ONE host decision (app/common/apply_link.py), by host LABELS — no substring
    match, an employer's own domain is never a job board;
  * its SQL form (`url_regex`) agrees with it, on SQLite as on Postgres;
  * `slate.place()` refuses an aggregator link (`aggregator_link`), places the
    employer's copy of the same role, and `BOARD_DIRECT_APPLY_ONLY=0` restores
    the old behaviour;
  * All Jobs' "Hide job-board links" and the `opens_on` label read the same list;
  * SerpAPI ranks the newly listed boards below an employer's careers link.

Rows are prefixed ``agl_`` and cleaned by that prefix.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest
from sqlmodel import delete, select

from app.common import apply_link
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Application, FunnelEvent, Job, JobSource
from app.strategy import slate

_P = "agl_"
UID = "agl_user"

AGGREGATOR = [
    "https://jobicy.com/jobs/157855-research-engineer-machine-learning-systems",
    "https://www.adzuna.com/details/5945633571?utm_medium=api",
    "https://www.adzuna.co.uk/jobs/details/5942868858?utm_campaign=google_jobs_apply",
    "https://www.adzuna.com.au/jobs/details/1",
    "https://jooble.org/desc/123",
    "https://uk.linkedin.com/jobs/view/123",
    "https://www.linkedin.com/jobs/view/123",
    "https://de.indeed.com/viewjob?jk=1",
    "https://ca.indeed.com/viewjob?jk=1",
    "https://www.indeed.ca/viewjob?jk=1",
    "https://www.glassdoor.co.uk/job-listing/x",
    "https://jobright.ai/jobs/info/abc123",
    "https://remoteworld.mysmartpros.com/job/manager-forward-deployed-5035792/",
    "https://www.learn4good.com/jobs/hamilton/new-jersey/info_technology/5561645305/e/",
    "https://rekru.ru/en/vacancy/505120/mlops-engineer",
    "https://www.jobleads.com/us/job/x",
    "https://us.trabajo.org/job-1",
    "https://www.reed.co.uk/jobs/data-scientist/1",
    "https://www.arbeitnow.nl/x", "https://www.arbeitnow.co.uk/x",
    "https://uk.talent.com/view?id=1",
    "https://remotive.com/remote-jobs/software-dev/x",
    "https://weworkremotely.com/remote-jobs/x",
    "https://www.builtinla.com/job/x", "https://www.simplyhired.co.uk/job/x",
    "https://www.efinancialcareers.co.uk/jobs-x", "https://www.jobijoba.co.uk/x",
    "HTTPS://WWW.JOBICY.COM/jobs/1",
    "https://jobicy.com",            # no path at all
    "https://jobicy.com:443/jobs/1",
]

DIRECT = [
    "https://jobs.ashbyhq.com/deepgram/9a030b32-d671-43e0-a221-4653bb73ba29",
    "https://job-boards.greenhouse.io/asg/jobs/8716127002",
    "https://boards.greenhouse.io/chime/jobs/8569366002?gh_jid=8569366002",
    "https://fico.wd1.myworkdayjobs.com/external/job/Work-from-Home-United-States/x_32365",
    # A Lever form posted BY a middleman is still a Lever form the extension fills.
    "https://jobs.lever.co/jobgether/2e5536bf-3965-4b6a-aab7-d91a25a7a79b",
    "https://apply.workable.com/j/70594BCF60/apply",
    "https://careers.adobe.com/us/en/job/x",
    "https://lokker.com/careers/openings/data-scientist",
    "http://block.xyz/careers/jobs/5214784008?gh_jid=5214784008",
    # Employers whose names collide with a job board's
    "https://www.reed.edu/human_resources/jobs/1",
    "https://indeedtech.com/careers/1",
    "https://careers.talentsoft.com/x",
    # A host that merely CONTAINS a board's name is not that board
    "https://jobicy.com.evil.example/jobs/1",
    "https://notlinkedin.com/jobs/1",
    # A job board's name in the PATH says nothing about where the link opens
    "https://acme.com/careers?ref=linkedin.com",
    "", "not a url",
]


@pytest.mark.parametrize("url", AGGREGATOR)
def test_job_board_links_are_recognised(url):
    assert apply_link.is_aggregator_url(url), url


@pytest.mark.parametrize("url", DIRECT)
def test_employer_and_ats_links_are_not(url):
    assert not apply_link.is_aggregator_url(url), url


def test_the_host_is_named_without_www():
    assert apply_link.aggregator_host("https://www.jobicy.com/jobs/1") == "jobicy.com"
    assert apply_link.aggregator_host("https://uk.linkedin.com/jobs/1") == "uk.linkedin.com"
    assert apply_link.aggregator_host("https://jobs.ashbyhq.com/x") is None


@pytest.mark.parametrize("url", AGGREGATOR + DIRECT)
def test_the_sql_regex_makes_the_same_decision(url):
    """`url_regex` is what the explorer filter runs in SQL. It must agree with
    the Python decision on every case, or the filter and the label disagree."""
    rx = re.compile(apply_link.url_regex(), re.IGNORECASE)
    assert bool(rx.search(url)) == apply_link.is_aggregator_url(url), url


def test_the_sql_regex_uses_no_python_only_syntax():
    """Postgres reads it too: no non-capturing or lookaround groups."""
    assert "(?" not in apply_link.url_regex()


def test_serpapi_ranks_newly_listed_boards_below_an_employer_link():
    from app.discovery.sources.serpapi import _best_apply_url, _rank_apply_link
    employer = "https://careers.fractal.ai/jobs/123"
    for board in ("https://www.learn4good.com/jobs/x", "https://www.jobleads.com/us/job/x",
                  "https://remoteworld.mysmartpros.com/job/x", "https://rekru.ru/en/vacancy/1",
                  "https://www.adzuna.co.uk/jobs/details/1"):
        assert _rank_apply_link(board) == 1, board
        assert _best_apply_url([{"link": board}, {"link": employer}]) == employer


# ── the board ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    import app.common.plan_limits as pl
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 10)
    _wipe()
    yield
    _wipe()


def _wipe():
    with get_session() as s:
        jids = [r[0] if isinstance(r, tuple) else r for r in s.exec(
            select(Job.id).where(Job.external_id.like(f"{_P}%"))).all()]
        if jids:
            s.exec(delete(Application).where(Application.job_id.in_(jids)))
            s.exec(delete(FunnelEvent).where(FunnelEvent.job_id.in_(jids)))
        s.exec(delete(Job).where(Job.external_id.like(f"{_P}%")))
        s.commit()


def _job(session, ext, url, *, source=JobSource.JOBICY, company=None, title=None,
         user_id=UID, score=86.0):
    j = Job(user_id=user_id, source=source, external_id=_P + ext,
            company=company or f"Co-{ext}", title=title or f"ML Engineer {ext}",
            location="Remote, US", remote=True, url=url, description="d",
            rerank_score=score, first_seen=datetime.utcnow())
    session.add(j)
    session.commit()
    session.refresh(j)
    return j


def _apps(session, job):
    return session.exec(select(Application).where(Application.job_id == job.id)).all()


def test_an_aggregator_link_never_reaches_the_board_and_says_why():
    with get_session() as s:
        j = _job(s, "jobicy", "https://jobicy.com/jobs/157855-x")
        res = slate.place(s, j, 86.0, user_id=UID)
        s.commit()
        assert not res.created and res.outcome == slate.OUTCOME_AGGREGATOR_LINK
        assert _apps(s, j) == []
        ev = s.exec(select(FunnelEvent).where(FunnelEvent.job_id == j.id,
                                              FunnelEvent.stage == slate.PLACEMENT_STAGE)).all()
        assert [e.reason for e in ev] == ["aggregator_link"]
        # Refusing is not closing: the job keeps its score for All Jobs.
        s.refresh(j)
        assert not j.is_closed and j.rerank_score == 86.0


def test_the_link_decides_not_the_source():
    """A SerpAPI row carrying the employer's Greenhouse link is deliverable; a
    'direct' source with a LinkedIn link is not."""
    with get_session() as s:
        ok = _job(s, "serp-gh", "https://job-boards.greenhouse.io/acme/jobs/1",
                  source=JobSource.SERPAPI)
        bad = _job(s, "gh-li", "https://www.linkedin.com/jobs/view/1",
                   source=JobSource.GREENHOUSE)
        assert slate.place(s, ok, 86.0, user_id=UID).created
        assert slate.place(s, bad, 86.0, user_id=UID).outcome == "aggregator_link"
        s.commit()


def test_refusing_the_aggregator_copy_frees_the_role_for_the_employer_posting():
    """The incident's shape: the jobicy copy of a role, then the employer's
    Ashby copy. Before, a delivered aggregator copy made the Ashby one a
    'duplicate'."""
    with get_session() as s:
        agg = _job(s, "dg-jobicy", "https://jobicy.com/jobs/157855-x",
                   company="Deepgram", title="Research Engineer, Machine Learning Systems")
        own = _job(s, "dg-ashby", "https://jobs.ashbyhq.com/deepgram/9a030b32",
                   source=JobSource.ASHBY,
                   company="Deepgram", title="Research Engineer, Machine Learning Systems")
        assert slate.place(s, agg, 86.0, user_id=UID).outcome == "aggregator_link"
        res = slate.place(s, own, 86.0, user_id=UID)
        s.commit()
        assert res.created and res.outcome == "placed"
        assert _apps(s, own)[0].apply_url == "https://jobs.ashbyhq.com/deepgram/9a030b32"


def test_the_switch_restores_delivering_them(monkeypatch):
    monkeypatch.setattr(settings, "board_direct_apply_only", False)
    with get_session() as s:
        j = _job(s, "switch", "https://www.adzuna.com/details/1", source=JobSource.ADZUNA)
        assert slate.place(s, j, 86.0, user_id=UID).created
        s.commit()


def test_the_setting_defaults_on():
    from app.config import Settings
    assert Settings.model_fields["board_direct_apply_only"].default is True


# ── All Jobs ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import server
    from app.common import ttl_cache
    monkeypatch.setattr(server, "_get_user_id", lambda request: "local")
    ttl_cache.invalidate()
    yield TestClient(server.app)
    ttl_cache.invalidate()


def test_all_jobs_labels_the_board_and_hides_it_on_request(client):
    with get_session() as s:
        _job(s, "ex-agg", "https://www.learn4good.com/jobs/x", source=JobSource.SERPAPI,
             user_id=None, title="Agl Cloud Data Engineer")
        _job(s, "ex-own", "https://jobs.lever.co/acme/1", source=JobSource.LEVER,
             user_id=None, title="Agl Platform Engineer")
    rows = client.get("/api/jobs?max_age_days=0&limit=200&search=Agl").json()["jobs"]
    by_title = {r["title"]: r for r in rows}
    assert by_title["Agl Cloud Data Engineer"]["opens_on"] == "learn4good.com"
    assert by_title["Agl Platform Engineer"]["opens_on"] is None

    body = client.get("/api/jobs?max_age_days=0&limit=200&search=Agl&hide_aggregators=1").json()
    titles = {r["title"] for r in body["jobs"]}
    assert titles == {"Agl Platform Engineer"}, \
        "the filter runs the SAME host list in SQL — learn4good was never ghost-flagged"
    assert body["total"] == 1
