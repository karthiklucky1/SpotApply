"""One role reaches a board once, however its title is spelled or whoever posts it.

Audit of delivered jobs, 2026-11-20 (48 deliveries read one by one). Three
roles were on one board twice:

  * "Virtual Assistant - Houston" at 78 and "Virtual Assistant - Detroit" at 82:
    one remote role, posted once per city, the city in each title;
  * "Scheduling & Operations Assistant | Remote" beside "Scheduling &
    Operations Assistant": the same remote role, one title carrying its mode;
  * "System Software / Embedded Engineer" from its employer and again from
    Jobgether, a recruiting board that republishes other employers' openings
    on its own Lever board and does not name them.

The slate already refused a second copy of a role (`same_role`), but it only
asked about rows whose company and WHOLE title matched, so a city, the word
Remote, a company spelled "Freddie Mac" by one source and "Freddiemac" by
another, or a recruiting board's name each made the copy a different role.

Pinned here:
  * `title_key` leaves out the parts of a title that only say where or how,
    and believes "where" only from the row's own location;
  * a title part that names a team, a product or another place is kept, and
    two on-site jobs in two cities are still two jobs;
  * one employer under two spellings is one employer;
  * a role the user dismissed does not come back as its next copy, while our
    own housekeeping skips never block one;
  * a recruiting board's copy yields to an employer's posting with the same
    title, in either order, and never costs that posting its place;
  * the check is one read, and never sees another user's rows.

Rows are prefixed ``sro_`` and cleaned by that prefix.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import event
from sqlmodel import delete, select

from app.common.apply_link import reposter_board
from app.db.init_db import engine, get_session
from app.db.models import Application, ApplicationStatus, FunnelEvent, Job, JobSource
from app.matching.preference_learning import USER_DISMISS_MARKER
from app.strategy import slate
from app.strategy.slate import role_key, title_key, titled_apart

_P = "sro_"
UID = "sro_user"
OTHER = "sro_other_user"


# ── the title ────────────────────────────────────────────────────────────────

SAME_ROLE_TITLES = [
    # (title, location) pairs that name ONE role
    (("Virtual Assistant - Houston", "Houston, Texas, United States"),
     ("Virtual Assistant - Detroit", "Detroit, Michigan, United States")),
    (("Scheduling & Operations Assistant | Remote", "Pennsylvania"),
     ("Scheduling & Operations Assistant", "Arlington, VA")),
    (("Scheduling & Operations Assistant", "Arlington, VA"),
     ("Scheduling and Operations Assistant", "Arlington, VA")),
    (("AI Engineer - Remote", "United States"), ("AI Engineer", "United States")),
    (("(Work From Home) | Scheduling Coordinator", "Georgia"),
     ("Scheduling Coordinator", "Georgia")),
    (("Software Engineer (Remote - US)", "US"), ("Software Engineer", "New York, NY")),
    (("Software Engineer, Backend (Remote)", "Remote – USA"),
     ("Software Engineer, Backend", "Remote – USA")),
    (("Customer Success Manager, 100% Remote (US Only)", "United States"),
     ("Customer Success Manager", "Denver, CO")),
    (("Account Executive - New York", "New York, NY"), ("Account Executive", "Boston, MA")),
    (("Account Executive - WA", "Seattle, Washington"),
     ("Account Executive (Washington)", "Seattle, WA")),
    (("Store Manager - Winston-Salem", "Winston-Salem, NC"), ("Store Manager", "Raleigh, NC")),
    (("Sr. Software Engineer – AI and Automation", ""),
     ("Senior Software Engineer - AI & Automation", "")),
    (("Backend  Engineer", "Remote"), ("Backend Engineer", "Remote")),
    (("Nurse - Home Based", "Ohio"), ("Nurse", "Ohio")),
]

DIFFERENT_ROLE_TITLES = [
    # A part that names a team, a product, a level or a job type is the role.
    (("Software Engineer - Payments", "New York, NY"), ("Software Engineer - Search", "New York, NY")),
    (("Software Engineer - Payments", "New York, NY"), ("Software Engineer", "New York, NY")),
    (("Data Analyst (Full Time)", "Dallas, TX"), ("Data Analyst (Part Time)", "Dallas, TX")),
    (("Software Engineer I", "Austin, TX"), ("Software Engineer II", "Austin, TX")),
    (("Sales Associate, Home", "Dallas, TX"), ("Sales Associate, Shoes", "Dallas, TX")),
    # A place the row's own location does not name is not believed.
    (("Virtual Assistant - Houston", "United States"), ("Virtual Assistant", "United States")),
    (("Software Engineer - TX", "New York, NY"), ("Software Engineer", "New York, NY")),
    (("Deployed Engineer, Professional Services (NYC)", "New York, NY"),
     ("Deployed Engineer, Professional Services", "New York, NY")),
    # The FIRST part is never read as a place: Mobile is a team in Mobile, AL.
    (("Mobile - Software Engineer", "Mobile, AL"), ("Software Engineer", "Mobile, AL")),
    # A work mode inside a part that says more is kept with it.
    (("Hybrid Cloud Engineer", "Austin, TX"), ("Cloud Engineer", "Austin, TX")),
    (("Remote Sensing Scientist", "Boulder, CO"), ("Sensing Scientist", "Boulder, CO")),
    (("Remote Office Assistant", "Worldwide"), ("Office Assistant", "Worldwide")),
]


@pytest.mark.parametrize("a,b", SAME_ROLE_TITLES)
def test_where_and_how_are_not_part_of_the_role(a, b):
    assert title_key(*a) == title_key(*b), (title_key(*a), title_key(*b))


@pytest.mark.parametrize("a,b", DIFFERENT_ROLE_TITLES)
def test_a_team_a_level_or_an_unstated_place_stays_in_the_title(a, b):
    assert title_key(*a) != title_key(*b), title_key(*a)


@pytest.mark.parametrize("title", [
    "Software Engineer", "Member of Technical Staff", "System Software / Embedded Engineer",
    "Full Stack Java Engineer — Workforce Management (WFM)",
    "Software Engineer- AI Agents / Generative AI", "2027 University Graduate - Machine Learning Engineer",
])
def test_a_title_with_nothing_to_leave_out_keeps_every_word(title):
    assert title_key(title, "Seattle, WA") == slate._norm_key(title)


def test_a_title_that_is_only_a_mode_is_not_emptied():
    assert title_key("Remote", "") == "remote"
    assert title_key("Remote | Hybrid", "Austin, TX") == "remotehybrid"
    assert title_key("", "Austin, TX") == "" and title_key(None, None) == ""


@pytest.mark.parametrize("a,b,apart", [
    (("Store Manager - Houston", "Houston, TX"), ("Store Manager - Detroit", "Detroit, MI"), True),
    (("Store Manager (Texas)", "Houston, TX"), ("Store Manager (Ohio)", "Columbus, OH"), True),
    # The same place, one place, or only the country: nothing sets them apart.
    (("Store Manager - Houston", "Houston, TX"), ("Store Manager | Houston", "Houston, Texas"), False),
    (("Store Manager - Houston", "Houston, TX"), ("Store Manager", "Detroit, MI"), False),
    (("Engineer (US)", "Austin, TX"), ("Engineer - Denver", "Denver, CO"), False),
    (("Engineer | Remote", "Austin, TX"), ("Engineer | Hybrid", "Denver, CO"), False),
    (("Engineer", "Austin, TX"), ("Engineer", "Denver, CO"), False),
])
def test_titles_that_each_name_their_own_place(a, b, apart):
    assert titled_apart(*a, *b) is apart and titled_apart(*b, *a) is apart


def test_one_employer_under_two_spellings_is_one_employer():
    """An ATS slug gives "Freddiemac"; an aggregator writes "Freddie Mac"."""
    assert role_key("Freddie Mac", "Engineer") == role_key("Freddiemac", "Engineer")
    assert role_key("AT&T", "Engineer") == role_key("AT & T", "Engineer")
    assert role_key("Acme", "Engineer") != role_key("Acme Robotics", "Engineer")
    # A blank company or title is never grouped.
    assert role_key("", "Engineer") is None and role_key("Acme", "") is None


def test_the_recruiting_board_is_known_by_name_or_by_its_board_link():
    assert reposter_board("Jobgether") == "jobgether"
    assert reposter_board("JobGether ", "") == "jobgether"
    assert reposter_board("Some Client", "https://jobs.lever.co/jobgether/9a03-0b32") == "jobgether"
    assert reposter_board("Some Client", "https://jobs.eu.lever.co/Jobgether/1?x=1") == "jobgether"
    # An employer's own board, and a path that only contains the word.
    assert reposter_board("Tensordyne", "https://jobs.lever.co/tensordyne/1") is None
    assert reposter_board("Acme", "https://acme.com/jobgether/1") is None
    assert reposter_board("Acme", "https://jobs.lever.co/acme/jobgether") is None
    assert reposter_board("", "") is None and reposter_board(None, None) is None


# ── the board ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    import app.common.plan_limits as pl
    monkeypatch.setattr(pl, "shortlist_daily_limit", lambda uid: 40)
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


def _job(ext, *, company, title, location="Remote, US", remote=True,
         source=JobSource.WORKABLE, url=None, text=None, user_id=UID, score=80.0) -> int:
    import hashlib
    text = text if text is not None else f"The posting text of {ext}."
    with get_session() as s:
        j = Job(user_id=user_id, source=source, external_id=_P + ext, company=company,
                title=title, location=location, remote=remote,
                url=url or f"https://apply.workable.com/{_P}co/j/{ext}",
                description=text, content_hash=hashlib.sha256(text.encode()).hexdigest(),
                rerank_score=score, first_seen=datetime.utcnow())
        s.add(j)
        s.commit()
        s.refresh(j)
        return j.id


def _place(jid, score=80.0, user_id=UID):
    with get_session() as s:
        p = slate.place(s, s.get(Job, jid), score, user_id=user_id)
        s.commit()
        return p


def _app(jid) -> Application:
    with get_session() as s:
        return s.exec(select(Application).where(Application.job_id == jid)).first()


def _set_app(jid, **fields):
    with get_session() as s:
        row = s.exec(select(Application).where(Application.job_id == jid)).first()
        for k, v in fields.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


def _reasons(jid) -> list:
    with get_session() as s:
        return [e.reason for e in s.exec(select(FunnelEvent).where(
            FunnelEvent.job_id == jid, FunnelEvent.stage == slate.PLACEMENT_STAGE)).all()]


def test_one_remote_role_posted_per_city_with_the_city_in_its_title():
    """The production pair: Workable, both remote, each with its own text
    (the city is in it), the city in each title."""
    detroit = _job("va-det", company="The Boutique COO", title="Virtual Assistant - Detroit",
                   location="Detroit, Michigan, United States")
    houston = _job("va-hou", company="The Boutique COO", title="Virtual Assistant - Houston",
                   location="Houston, Texas, United States")
    assert _place(detroit, 82.0).created
    p = _place(houston, 78.0)
    assert not p.created and p.outcome == "duplicate"
    assert _app(houston) is None and _reasons(houston) == ["duplicate"]


def test_the_same_role_with_its_work_mode_in_the_title():
    first = _job("soa-pa", company="Aisle And Abroad", source=JobSource.LEVER,
                 title="Scheduling & Operations Assistant | Remote", location="Pennsylvania")
    again = _job("soa-va", company="Aisle And Abroad", source=JobSource.LEVER,
                 title="Scheduling & Operations Assistant", location="Arlington, VA")
    assert _place(first, 78.0).created
    assert _place(again, 72.0).outcome == "duplicate"


def test_two_onsite_jobs_in_two_cities_are_still_two_jobs():
    """The title key says the titles name one role; `same_role` still wants
    evidence it is one opening, and two on-site sites with their own text
    are not."""
    a = _job("sm-hou", company="Sro Retail", title="Store Manager - Houston",
             location="Houston, TX", remote=False)
    b = _job("sm-det", company="Sro Retail", title="Store Manager - Detroit",
             location="Detroit, MI", remote=False)
    assert _place(a).created and _place(b).created


def test_another_door_does_not_make_two_cities_one_job():
    """Titles that only compare equal because each one's city was left out:
    an ATS row and a search result are then two jobs, not one seen twice."""
    a = _job("xs-hou", company="Sro Retail", title="Store Manager - Houston",
             location="Houston, TX", remote=False, source=JobSource.WORKABLE)
    b = _job("xs-det", company="Sro Retail", title="Store Manager - Detroit",
             location="Detroit, MI", remote=False, source=JobSource.SERPAPI,
             url="https://careers.sroretail.example/jobs/det")
    assert _place(a).created and _place(b).created
    # …but the same title from another door, with no city of its own, still is
    # the one already on the board (the rule before this one, unchanged).
    c = _job("xs-any", company="Sro Retail", title="Store Manager",
             location="Houston, TX", remote=False, source=JobSource.SERPAPI,
             url="https://careers.sroretail.example/jobs/hou")
    assert _place(c).outcome == "duplicate"


def test_remote_copies_from_two_doors_with_a_city_each_are_one_job():
    a = _job("xr-hou", company="Sro Assist", title="Virtual Assistant - Houston",
             location="Houston, Texas, United States", source=JobSource.WORKABLE)
    b = _job("xr-det", company="Sro Assist", title="Virtual Assistant - Detroit",
             location="Detroit, Michigan, United States", source=JobSource.SERPAPI,
             url="https://careers.sroassist.example/jobs/det")
    assert _place(a).created
    assert not _place(b).created


def test_two_teams_under_one_title_are_two_jobs():
    a = _job("se-pay", company="Sro Software", title="Software Engineer - Payments")
    b = _job("se-sea", company="Sro Software", title="Software Engineer - Search")
    assert _place(a).created and _place(b).created


def test_one_employer_spelled_two_ways_by_two_sources():
    """The ATS row is "Freddiemac" (its slug); the search result says "Freddie
    Mac". Another door, the same title: one role, the employer's kept."""
    found = _job("fm-serp", company="Freddie Mac", source=JobSource.SERPAPI,
                 title="Software Engineer Professional, Financial Engineering",
                 location="McLean, VA", remote=False,
                 url="https://careers.freddiemac.com/us/en/job/JR1")
    own = _job("fm-wd", company="Freddiemac", source=JobSource.WORKDAY,
               title="Software Engineer Professional, Financial Engineering",
               location="McLean, VA", remote=False,
               url="https://freddiemac.wd5.myworkdayjobs.com/External/job/JR1")
    assert _place(found).created
    assert _place(own).created, "the employer's own posting takes the unopened copy's place"
    assert _app(found).status == ApplicationStatus.SKIPPED
    assert "employer's own posting" in _app(found).notes
    # …and the other way round the second door is refused.
    again = _job("fm-serp2", company="Freddie Mac", source=JobSource.SERPAPI,
                 title="Software Engineer Professional, Financial Engineering",
                 location="McLean, VA", remote=False,
                 url="https://careers.freddiemac.com/us/en/job/JR1b")
    assert _place(again).outcome == "duplicate"


def test_a_role_the_user_dismissed_does_not_come_back_as_its_next_copy():
    first = _job("dis-a", company="Sro Assist", title="Virtual Assistant - Austin",
                 location="Austin, Texas, United States")
    nxt = _job("dis-b", company="Sro Assist", title="Virtual Assistant - Tampa",
               location="Tampa, Florida, United States")
    assert _place(first).created
    _set_app(first, status=ApplicationStatus.SKIPPED, notes=USER_DISMISS_MARKER)
    p = _place(nxt)
    assert not p.created and p.outcome == "duplicate"


@pytest.mark.parametrize("note", [
    f"{slate.SLATE_CHALLENGER_NOTE} ('X' at 90 vs 78) and this one had not been opened.",
    "Auto-removed from shortlist: older than the board's window",
    "Displaced by higher-scoring job under the per-company cap",
    "",
])
def test_our_own_housekeeping_never_blocks_the_next_copy(note):
    first = _job("hk-a", company="Sro Assist", title="Virtual Assistant - Austin",
                 location="Austin, Texas, United States")
    nxt = _job("hk-b", company="Sro Assist", title="Virtual Assistant - Tampa",
               location="Tampa, Florida, United States")
    assert _place(first).created
    _set_app(first, status=ApplicationStatus.SKIPPED, notes=note)
    assert _place(nxt).created


# ── a recruiting board's copy ────────────────────────────────────────────────

TITLE = "System Software / Embedded Engineer"


def _employer(ext="emp", title=TITLE, company="Tensordyne"):
    return _job(ext, company=company, title=title, source=JobSource.ASHBY,
                location="Remote, United States", url=f"https://jobs.ashbyhq.com/{_P}tensordyne/{ext}")


def _board_copy(ext="jg", title=TITLE):
    return _job(ext, company="Jobgether", title=title, source=JobSource.LEVER, location="US",
                url=f"https://jobs.lever.co/jobgether/{_P}{ext}",
                text="This position is posted by Jobgether on behalf of a partner company.")


def test_a_board_copy_is_refused_while_the_employers_role_is_on_record():
    own, copy = _employer(), _board_copy(title=TITLE + " - Remote")
    assert _place(own, 82.0).created
    p = _place(copy, 82.0)
    assert not p.created and p.outcome == slate.OUTCOME_REPOSTED_ROLE
    assert _app(copy) is None and _reasons(copy) == ["reposted_role"]
    # Refusing is not closing: it keeps its score for All Jobs.
    with get_session() as s:
        j = s.get(Job, copy)
        assert not j.is_closed and j.rerank_score == 80.0


def test_an_unopened_board_copy_gives_way_to_the_employers_posting():
    copy, own = _board_copy(), _employer()
    assert _place(copy, 82.0).created
    p = _place(own, 82.0)
    assert p.created and p.outcome == "placed"
    gone = _app(copy)
    assert gone.status == ApplicationStatus.SKIPPED and slate.SLATE_REPOST_NOTE in gone.notes
    # It no longer holds a slot on the board…
    with get_session() as s:
        assert copy not in {a.job_id for a in slate.todays_entries(s, UID)}
    # …and All Jobs still lists it as a strong match: the title is all that matched.
    from app.api import server
    with get_session() as s:
        listed = s.exec(select(Job.id).select_from(Job)
                        .outerjoin(Application, Application.job_id == Job.id)
                        .where(Job.external_id.like(f"{_P}%"), server._not_on_board_cond())).all()
        assert [r[0] if isinstance(r, tuple) else r for r in listed] == [copy]


@pytest.mark.parametrize("touched", [
    dict(viewed_at=datetime(2026, 11, 20, 12, 0)),
    dict(status=ApplicationStatus.TAILORED),
    dict(status=ApplicationStatus.SUBMITTED),
    dict(status=ApplicationStatus.SKIPPED, notes=USER_DISMISS_MARKER),
])
def test_a_board_copy_the_user_touched_is_left_alone_and_costs_the_employer_nothing(touched):
    """Title alone is not proof the two are one opening, so it never refuses
    an employer's own posting and never undoes what the user did."""
    copy, own = _board_copy(), _employer()
    assert _place(copy, 82.0).created
    _set_app(copy, **touched)
    assert _place(own, 82.0).created
    after = _app(copy)
    assert after.status == touched.get("status", ApplicationStatus.SHORTLISTED)
    assert slate.SLATE_REPOST_NOTE not in (after.notes or "")


def test_a_dismissed_employer_role_also_stops_the_board_copy():
    own, copy = _employer(), _board_copy()
    assert _place(own, 82.0).created
    _set_app(own, status=ApplicationStatus.SKIPPED, notes=USER_DISMISS_MARKER)
    assert _place(copy, 82.0).outcome == slate.OUTCOME_REPOSTED_ROLE


def test_a_board_copy_nobody_else_posted_reaches_the_board():
    other = _employer(title="Site Reliability Engineer")
    copy = _board_copy()
    assert _place(other, 82.0).created
    assert _place(copy, 82.0).created


def test_two_employers_with_one_title_are_never_compared():
    a = _employer("emp-a", title="Senior Site Reliability Engineer", company="Sro Alpha")
    b = _employer("emp-b", title="Senior Site Reliability Engineer", company="Sro Beta")
    assert _place(a).created and _place(b).created


# ── bounds ───────────────────────────────────────────────────────────────────

def test_another_users_rows_are_never_on_record():
    theirs = _job("ten-a", company="Sro Tenant", title="Backend Engineer", user_id=OTHER)
    mine = _job("ten-b", company="Sro Tenant", title="Backend Engineer")
    assert _place(theirs, user_id=OTHER).created
    assert _place(mine).created
    # …the board copy rule included.
    their_own = _employer("ten-emp")
    with get_session() as s:
        row = s.get(Job, their_own)
        row.user_id = OTHER
        s.add(row)
        s.commit()
    assert _place(their_own, user_id=OTHER).created
    assert _place(_board_copy("ten-jg")).created


def test_an_employers_posting_never_takes_another_users_copy_off_their_board():
    """The direction that WRITES: the unopened board copy is someone else's."""
    theirs = _board_copy("wr-jg")
    with get_session() as s:
        row = s.get(Job, theirs)
        row.user_id = OTHER
        s.add(row)
        s.commit()
    assert _place(theirs, user_id=OTHER).created
    assert _place(_employer("wr-emp")).created
    after = _app(theirs)
    assert after.status == ApplicationStatus.SHORTLISTED and not (after.notes or "")


def test_the_read_is_narrowed_to_rows_that_could_be_the_role():
    """It is paid on every placement, a refused re-offer included: other
    employers' rows do not come back at all."""
    for i in range(6):
        assert _place(_job(f"nr-x{i}", company=f"Sro Other {i}", title="Backend Engineer")).created
    mine = _job("nr-a", company="Sro Reads", title="Backend Engineer")
    assert _place(mine).created
    again = _job("nr-b", company="Sro  Reads!", title="Backend Engineer | Remote")
    seen: list = []
    real = slate.title_key

    def _counting(title, location=""):
        seen.append(title)
        return real(title, location)

    with get_session() as s:
        job = s.get(Job, again)
        slate.title_key = _counting
        try:
            found = slate._role_on_record(s, job, UID)
        finally:
            slate.title_key = real
    assert found is not None and found.kind == slate.SAME_ROLE
    # The job in hand, and the one row of its own employer: none of the six others.
    assert sorted(seen) == ["Backend Engineer", "Backend Engineer | Remote"]


@pytest.mark.parametrize("on_record,board_title", [
    ("Sr. Site Reliability Engineer", "Senior Site Reliability Engineer - Remote"),
    ("Data Base Engineer", "Database Engineer"),
    ("Site Reliability Engineer (Remote)", "Site-Reliability Engineer"),
])
def test_narrowing_the_read_loses_no_spelling_of_the_title(on_record, board_title):
    own = _employer("sp-emp", title=on_record)
    copy = _board_copy("sp-jg", title=board_title)
    assert _place(own, 82.0).created
    assert _place(copy, 82.0).outcome == slate.OUTCOME_REPOSTED_ROLE


def test_any_spelling_is_the_same_decision_as_the_key():
    import re
    rx = slate._any_spelling("freddiemac")
    for name in ("freddiemac", "freddie mac", "freddie-mac", " freddie  mac. "):
        assert re.search(rx, name), name
    for name in ("freddie mac inc", "the freddie mac", "freddiemae", "fredie mac"):
        assert not re.search(rx, name), name
    assert re.search(slate._any_spelling("reliability", whole=False), "site-reliability engineer")
    # No flags and no group syntax: one reading on Postgres and on SQLite.
    assert "(?" not in rx and rx == rx.lower()
    # A very long name is matched by its start; the exact comparison is made in Python.
    long_key = "a" * 200
    assert not slate._any_spelling(long_key).endswith("$")
    assert len(slate._any_spelling(long_key)) < 1200


def test_the_re_shortlist_backstop_never_fetches_a_whole_row(monkeypatch):
    """`pipeline._reshortlist_scored_jobs` offers every refused job to
    `place()` again on each five-minute pass. Three ways it fetched far more
    than it used, each pinned here by the statements one pass sends:

      * it walked partly loaded rows and committed after every job; a commit
        expires every row the session holds, so each row after the first
        refused candidate — delivered ones too — was re-read WHOLE,
        description included, when the loop touched its id;
      * `place()` read three dates (the freshness check) and the location off
        a row loaded without them: one SELECT per column per re-offer;
      * a check on the description would have fetched it the same way.
    """
    import re

    from app.config import settings
    from app.matching import pipeline
    monkeypatch.setattr(settings, "min_posting_chars", 200)          # the thin check is live
    on_board = _job("bs-a", company="Sro Backstop", title="Backend Engineer - Remote",
                    location="Austin, TX", text="A real posting. " * 60, score=90.0)
    assert _place(on_board, 90.0).created
    refused = _job("bs-b", company="Sro Backstop", title="Backend Engineer",
                   location="Denver, CO", text="Another real posting. " * 60, score=85.0)
    also_refused = _job("bs-c", company="Sro Backstop", title="Backend Engineer | Remote",
                        location="Reno, NV", text="A third real posting. " * 60, score=84.0)
    # Delivered jobs that sort AFTER the refused ones: the rows a commit used
    # to expire and the loop then re-read in full, one by one.
    for i in range(4):
        assert _place(_job(f"bs-d{i}", company=f"Sro Delivered {i}", title="Data Engineer",
                           text="A delivered posting. " * 60, score=80.0 - i), 80.0 - i).created
    statements: list = []

    def _st(conn, cursor, statement, *a_, **k_):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", _st)
    try:
        placed, _count = pipeline._reshortlist_scored_jobs(UID, 5)
    finally:
        event.remove(engine, "before_cursor_execute", _st)
    assert placed == []
    assert _reasons(refused) == ["duplicate"] and _reasons(also_refused) == ["duplicate"]
    assert not any("job.description" in s for s in statements), "a description left the database"
    one_by_one = [s for s in statements
                  if re.match(r"select job\.\w+ as \w+ from job where job\.id = ", s)]
    assert one_by_one == [], one_by_one
    # A job row is read for the two candidates only, never for a delivered one:
    # once with its short columns, once more under the lock with the rest.
    job_reads = [s for s in statements if " from job where job.id = " in s]
    assert len(job_reads) == 4, job_reads
    lock = [s for s in job_reads if s.startswith("select job.id, job.first_seen")]
    assert len(lock) == 2 and "job.location" in lock[0] and "job.posted_at" in lock[0]


def test_the_check_is_one_read():
    a = _job("one-a", company="Sro Reads", title="Backend Engineer - Remote")
    for i in range(6):                                # other employers on record
        assert _place(_job(f"one-x{i}", company=f"Sro Other {i}", title="Backend Engineer")).created
    assert _place(a).created
    b = _job("one-b", company="Sro Reads", title="Backend Engineer", location="Austin, TX")
    statements: list = []

    def _st(conn, cursor, statement, *a_, **k_):
        statements.append(statement)

    with get_session() as s:
        job = s.get(Job, b)
        event.listen(engine, "before_cursor_execute", _st)
        try:
            found = slate._role_on_record(s, job, UID)
        finally:
            event.remove(engine, "before_cursor_execute", _st)
    assert found is not None and found.kind == slate.SAME_ROLE
    assert len(statements) == 1, statements
    sql = statements[0].lower()
    assert "job.description" not in sql and "application.notes," not in sql
