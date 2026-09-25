"""Posting identity: two employers must never become one job.

PRODUCTION DEFECT, 2026-09-25. A Workday requisition id is unique inside one
TENANT. Two employers both had `R29845`, and because every posting-keyed table
is keyed ``(source, external_id)`` they merged into a single job:

    CrowdStrike  "Sr. Full Stack Engineer, Cloud Native - AIDR"  Sunnyvale CA
    GN           "Aud Fitting Support Advisor"                   Shakopee MN

The damage was not only a mislabelled row. `_inherit_shared_first_seen` looks a
posting up by ``(source, external_id)``, so the CrowdStrike job inherited GN's
sighting: `first_seen` four days before CrowdStrike's posting was discovered.
`first_seen` is the column the 5-day scoring window and the "be first to apply"
promise are built on, so the collision AGED A FRESH POSTING.

Every test below pins one half of the fix: the qualifier itself, or the
behaviour that must survive it (same-employer deduplication, idempotent
re-ingestion, missing ids).

All rows are prefixed `jid-` and deleted by that prefix — a wholesale
delete(Job) takes out fixtures other files already built.
"""
from __future__ import annotations

import pytest

from app.discovery import job_identity as jid
from app.discovery.base import RawJob


# ── the qualifier ────────────────────────────────────────────────────────────

def test_two_employers_sharing_a_requisition_get_different_identities():
    """The production case, at the unit the defect lived in."""
    crowdstrike = jid.scoped_external_id("workday", "crowdstrike", "R29845")
    gn = jid.scoped_external_id("workday", "gn", "R29845")
    assert crowdstrike != gn
    assert crowdstrike == "crowdstrike:R29845"
    assert gn == "gn:R29845"
    # And the employer-facing requisition is still recoverable from both.
    assert jid.raw_requisition(crowdstrike) == "R29845"
    assert jid.raw_requisition(gn) == "R29845"


def test_scoping_is_idempotent():
    """Re-ingesting an already-scoped id must not double the prefix, or every
    pass would mint a new posting."""
    once = jid.scoped_external_id("workday", "crowdstrike", "R29845")
    twice = jid.scoped_external_id("workday", "crowdstrike", once)
    assert once == twice == "crowdstrike:R29845"


def test_a_globally_unique_source_is_left_alone():
    """Rewriting identity where the id is ALREADY unique churns every row for
    no gain and breaks existing deduplication. The allowlist is the point."""
    for source, ext in (("ashby", "a9fda5e3-60a8-4fa1-927f-9702fe7c7f61"),
                        ("lever", "d1f9f30a-e3a6-4bba-9f30-0ae3a6ba3f00"),
                        ("greenhouse", "7778289"),
                        ("workable", "ABC123DEF4")):
        assert jid.scoped_external_id(source, "acme", ext) == ext
        assert not jid.is_tenant_scoped(source)


def test_a_missing_tenant_does_not_produce_a_colliding_prefix():
    """`":R29845"` would collide exactly as before while looking fixed. When the
    tenant is unknown the id stays bare and the row remains detectable as
    unscoped."""
    for tenant in (None, "", "   "):
        got = jid.scoped_external_id("workday", tenant, "R29845")
        assert got == "R29845", got
        assert jid.looks_unscoped("workday", got)


def test_a_missing_requisition_id_yields_no_identity():
    """An empty id must not become the string `"crowdstrike:"`, which would make
    every id-less posting at one employer the same posting."""
    for raw in (None, "", "  "):
        assert jid.scoped_external_id("workday", "crowdstrike", raw) == ""


def test_a_raw_id_containing_the_separator_round_trips():
    scoped = jid.scoped_external_id("workday", "acme", "REQ:42")
    assert scoped == "acme:REQ:42"
    assert jid.parse_scoped(scoped) == ("acme", "REQ:42")
    assert jid.raw_requisition(scoped) == "REQ:42"


def test_parse_tolerates_unscoped_and_malformed_ids():
    assert jid.parse_scoped("R29845") == (None, "R29845")
    assert jid.parse_scoped("") == (None, "")
    # Malformed (empty side) is treated as an opaque raw id, never as a tenant.
    assert jid.parse_scoped(":R29845") == (None, ":R29845")
    assert jid.parse_scoped("acme:") == (None, "acme:")


# ── deriving the tenant from a stored URL (what makes the repair reversible) ──

@pytest.mark.parametrize("url,expected", [
    ("https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers/job/X_R29845",
     "crowdstrike"),
    ("https://gn.wd3.myworkdayjobs.com/gn-careers/job/MN-Shakopee/Y_R29845-1", "gn"),
    ("https://relx.wd3.myworkdayjobs.com/relx/job/Philadelphia-PA/Z_R118682-2", "relx"),
    ("https://fis.wd5.myworkdayjobs.com/searchjobs/job/US-GA-ATL/Q_JR0309198", "fis"),
    ("https://ms.wd5.myworkdayjobs.com/external/job/New-York/W_JR036259", "ms"),
])
def test_tenant_is_derivable_from_the_stored_workday_url(url, expected):
    """The repair of existing rows reads the tenant out of the URL each row
    already stores, so it is deterministic rather than guessed, and stripping
    the prefix restores the original value exactly."""
    assert jid.tenant_from_url("workday", url) == expected


def test_a_generic_host_label_is_not_the_employer():
    assert jid.tenant_from_url("bamboohr", "https://careers.acme.com/jobs/12") == "acme"
    assert jid.tenant_from_url("bamboohr", "https://acme.bamboohr.com/careers/12") == "acme"
    # A two-label host IS the employer's own domain — don't walk past it.
    assert jid.tenant_from_url("teamtailor", "https://jobs.io/x") == "jobs"


def test_tenant_from_url_is_empty_for_unscoped_sources_and_junk():
    assert jid.tenant_from_url("greenhouse", "https://boards.greenhouse.io/a/jobs/1") == ""
    assert jid.tenant_from_url("workday", "") == ""
    assert jid.tenant_from_url("workday", "not-a-url") == ""


# ── the scrapers actually emit it ────────────────────────────────────────────

def test_the_workday_scraper_emits_a_tenant_scoped_id(monkeypatch):
    """Guards the call site, not just the helper — the helper was always
    correct; the defect was that the scraper did not call it."""
    import app.discovery.workday as wd
    src = open(wd.__file__).read()
    assert 'external_id=scoped_external_id(' in src, \
        "workday must scope its external_id"
    assert '"workday", tenant, req_id' in src, \
        "workday must scope by TENANT, not by anything else"


@pytest.mark.parametrize("module,args", [
    ("app.discovery.bamboohr", '"bamboohr", self.board_slug, ext_id'),
    ("app.discovery.teamtailor", '"teamtailor", self.board_slug, ext_id'),
])
def test_the_other_per_board_sources_scope_too(module, args):
    import importlib
    mod = importlib.import_module(module)
    src = open(mod.__file__).read()
    assert 'external_id=scoped_external_id(' in src, f"{module} must scope"
    assert args in src, f"{module} must scope by its board slug"


# ── end to end through the real upsert ───────────────────────────────────────

PREFIX = "jid-"


def _raw(tenant: str, req: str, *, company: str, title: str,
         first_seen=None) -> RawJob:
    ext = jid.scoped_external_id("workday", f"{PREFIX}{tenant}", req)
    return RawJob(
        source="workday", external_id=ext, company=company, title=title,
        location="Remote", remote=True,
        url=f"https://{PREFIX}{tenant}.wd5.myworkdayjobs.com/x/job/Y_{req}",
        description="Backend engineer building services.", posted_at=None,
        first_seen=first_seen,
    )


def _cleanup():
    """Delete only OUR rows, by prefix — including the posting-keyed side
    tables. Leaving those behind made the second test in this file violate
    uq_jhc_source_external_id, which is a suite that fails differently
    depending on which tests ran."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import (Application, Job, JobGeography, JobHiringContext,
                               JobLiveness)
    with get_session() as s:
        rows = s.exec(select(Job).where(Job.external_id.like(f"{PREFIX}%"))).all()
        for j in rows:
            for a in s.exec(select(Application).where(Application.job_id == j.id)).all():
                s.delete(a)
            s.delete(j)
        for model in (JobHiringContext, JobGeography, JobLiveness):
            for r in s.exec(select(model).where(
                    model.external_id.like(f"%{PREFIX}%"))).all():
                s.delete(r)
        s.commit()


@pytest.fixture
def clean():
    _cleanup()
    yield
    _cleanup()


def _count(ext: str) -> int:
    from sqlalchemy import func
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job
    with get_session() as s:
        v = s.exec(select(func.count(Job.id)).where(
            Job.external_id == ext, Job.user_id == "local")).one()
    # SQLModel returns a bare scalar here, SQLAlchemy a 1-tuple — the repo's own
    # server._scalar exists for the same reason.
    return int(v[0] if isinstance(v, (list, tuple)) else v)


def test_upsert_keeps_two_employers_with_one_requisition_apart(clean):
    """The whole point, through `_upsert`: same req id, two tenants, two jobs,
    each keeping its OWN employer, title and URL."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job
    from app.discovery import pipeline as P

    a = _raw("crowdstrike", "R29845", company="CrowdStrike",
             title="Sr. Full Stack Engineer, Cloud Native")
    b = _raw("gn", "R29845", company="GN",
             title="Aud Fitting Support Advisor")
    P._upsert([a, b], user_id="local")

    with get_session() as s:
        rows = s.exec(select(Job).where(
            Job.external_id.like(f"{PREFIX}%"), Job.user_id == "local")).all()
        by_company = {r.company: r for r in rows}
    assert len(rows) == 2, f"expected two jobs, got {[r.external_id for r in rows]}"
    assert set(by_company) == {"CrowdStrike", "GN"}
    # Each keeps its own posting's fields — the merge used to cross them.
    assert "Full Stack" in by_company["CrowdStrike"].title
    assert "Aud Fitting" in by_company["GN"].title
    assert "crowdstrike" in by_company["CrowdStrike"].url
    assert f"{PREFIX}gn." in by_company["GN"].url


def test_repeated_ingestion_does_not_duplicate(clean):
    from app.discovery import pipeline as P
    r = _raw("crowdstrike", "R29845", company="CrowdStrike", title="Engineer")
    P._upsert([r], user_id="local")
    P._upsert([_raw("crowdstrike", "R29845", company="CrowdStrike",
                    title="Engineer")], user_id="local")
    assert _count(r.external_id) == 1


def test_a_legitimate_same_employer_duplicate_still_dedupes(clean):
    """Scoping must not turn deduplication off: the same employer re-posting the
    same requisition is still ONE job."""
    from app.discovery import pipeline as P
    r1 = _raw("crowdstrike", "R30000", company="CrowdStrike", title="Engineer II")
    r2 = _raw("crowdstrike", "R30000", company="CrowdStrike", title="Engineer II")
    P._upsert([r1, r2], user_id="local")
    assert _count(r1.external_id) == 1


def test_a_posting_does_not_inherit_another_employers_first_seen(clean):
    """THE TIMESTAMP DEFECT. `_inherit_shared_first_seen` looks a posting up by
    (source, external_id); under the bare req id the second employer's posting
    inherited the first's sighting and was born days old. Distinct identities
    mean distinct sightings."""
    from datetime import datetime, timedelta

    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Job
    from app.discovery import pipeline as P

    old = datetime.utcnow() - timedelta(days=4)
    # GN's posting was sighted four days ago…
    P._upsert([_raw("gn", "R29845", company="GN", title="Aud Fitting Support Advisor",
                    first_seen=old)], user_id="local")
    # …CrowdStrike's identical requisition is sighted now.
    P._upsert([_raw("crowdstrike", "R29845", company="CrowdStrike",
                    title="Sr. Full Stack Engineer")], user_id="local")

    with get_session() as s:
        rows = {r.company: r for r in s.exec(select(Job).where(
            Job.external_id.like(f"{PREFIX}%"), Job.user_id == "local")).all()}
    cs = rows["CrowdStrike"]
    gn = rows["GN"]
    assert gn.first_seen is not None and cs.first_seen is not None
    assert cs.first_seen > gn.first_seen, (
        "CrowdStrike inherited GN's sighting — the posting was born aged")
    assert (cs.first_seen - gn.first_seen) > timedelta(days=3)


# ── the repair of rows written BEFORE the qualifier ──────────────────────────
#
# Production already holds merged rows, so the fix is only half a fix without a
# repair that is reversible, non-destructive and preserves user history. These
# pin all three.

def _seed_prod_collision():
    """The real shape of the defect: two employers, ONE bare requisition id, a
    hiring-context row carrying the wrong employer's url, and an Application on
    one of them (the user history that must survive)."""
    from app.db.init_db import get_session
    from app.db.models import (Application, ApplicationStatus, Job,
                               JobHiringContext, JobSource)
    req = f"{PREFIX}R29845"
    with get_session() as s:
        cs = Job(user_id="local", source=JobSource.WORKDAY, external_id=req,
                 company="CrowdStrike", title="Sr. Full Stack Engineer",
                 url=f"https://{PREFIX}crowdstrike.wd5.myworkdayjobs.com/c/job/X_{req}",
                 description="d", location="Sunnyvale, CA")
        s.add(cs)
        s.commit()
        s.refresh(cs)
        # An application on the CrowdStrike job: user history.
        s.add(Application(job_id=cs.id, user_id="local", apply_track="manual",
                          status=ApplicationStatus.SHORTLISTED,
                          notes="user note that must survive"))
        # The merged context row — recorded from GN's posting, attached to the
        # shared key, which is exactly what production showed.
        s.add(JobHiringContext(
            source="workday", external_id=req, requisition_id=req,
            source_url=f"https://{PREFIX}gn.wd3.myworkdayjobs.com/g/job/Y_{req}-1"))
        s.commit()
        cs_id = cs.id
    # GN's own job, same bare id, different user pool so the unique constraint
    # (user_id, source, external_id) permits both to exist as production did.
    with get_session() as s:
        gn = Job(user_id="__shared__", source=JobSource.WORKDAY, external_id=req,
                 company="GN", title="Aud Fitting Support Advisor",
                 url=f"https://{PREFIX}gn.wd3.myworkdayjobs.com/g/job/Y_{req}-1",
                 description="d", location="Shakopee, MN")
        s.add(gn)
        s.commit()
        gn_id = gn.id
    return req, cs_id, gn_id


def test_the_diagnostic_detects_the_production_collision(clean):
    """Read-only: it must SEE two employers on one id before anything is fixed."""
    from app.db.init_db import get_session
    from scripts.diagnose_job_identity import report_collisions, report_unscoped
    req, _cs, _gn = _seed_prod_collision()
    with get_session() as s:
        collisions = report_collisions(s, 500)
        unscoped = report_unscoped(s)
    hit = [c for c in collisions if c[1] == req]
    assert hit, f"collision on {req} not detected; got {collisions}"
    assert hit[0][2] == 2, "should report two distinct employers"
    assert unscoped["workday"] >= 2


def test_the_repair_separates_the_employers_and_keeps_user_history(clean):
    """Apply it: distinct scoped ids, the context row handed to the employer its
    OWN url names, and the application plus its note untouched."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Application, Job, JobHiringContext
    from scripts.diagnose_job_identity import apply_repair, plan

    req, cs_id, gn_id = _seed_prod_collision()

    with get_session() as s:
        repairable, unresolvable = plan(s, 500)
    mine = [r for r in repairable if r[3] == req]
    assert len(mine) == 2, f"both rows should be repairable, got {mine}"
    assert not [u for u in unresolvable if u[2] == req]

    stats = apply_repair(mine)
    assert stats["jobs"] == 2, stats
    assert stats["skipped_would_collide"] == 0, stats

    with get_session() as s:
        cs = s.get(Job, cs_id)
        gn = s.get(Job, gn_id)
        # Distinct identities, each naming its own employer.
        assert cs.external_id == f"{PREFIX}crowdstrike:{req}", cs.external_id
        assert gn.external_id == f"{PREFIX}gn:{req}", gn.external_id
        assert cs.external_id != gn.external_id
        # Employer, title and url stayed with the right posting.
        assert cs.company == "CrowdStrike" and "Full Stack" in cs.title
        assert gn.company == "GN" and "Aud Fitting" in gn.title

        # The context row followed ITS OWN url to GN, not to CrowdStrike.
        ctx = s.exec(select(JobHiringContext).where(
            JobHiringContext.requisition_id == req)).all()
        assert len(ctx) == 1
        assert ctx[0].external_id == f"{PREFIX}gn:{req}", (
            "merged context must be reassigned by the url it was observed from")

        # USER HISTORY: the application and its note survive untouched.
        apps = s.exec(select(Application).where(Application.job_id == cs_id)).all()
        assert len(apps) == 1
        assert apps[0].notes == "user note that must survive"


def test_the_repair_is_reversible(clean):
    """Stripping the prefix restores the original id exactly — the property that
    makes this safe to run against production."""
    from app.db.init_db import get_session
    from app.db.models import Job
    from scripts.diagnose_job_identity import apply_repair, plan

    req, cs_id, _gn = _seed_prod_collision()
    with get_session() as s:
        repairable, _ = plan(s, 500)
    apply_repair([r for r in repairable if r[3] == req])
    with get_session() as s:
        assert jid.raw_requisition(s.get(Job, cs_id).external_id) == req


def test_the_repair_skips_rather_than_violating_the_unique_constraint(clean):
    """A genuine same-employer duplicate would map two rows onto one id. That is
    reported and skipped, never forced."""
    from app.db.init_db import get_session
    from app.db.models import Job, JobSource
    from scripts.diagnose_job_identity import apply_repair

    bare = f"{PREFIX}R777"
    scoped = jid.scoped_external_id("workday", f"{PREFIX}acme", bare)
    url = f"https://{PREFIX}acme.wd5.myworkdayjobs.com/a/job/Z_{bare}"
    with get_session() as s:
        s.add(Job(user_id="local", source=JobSource.WORKDAY, external_id=scoped,
                  company="Acme", title="Engineer", url=url, description="d"))
        s.commit()
        dup = Job(user_id="local", source=JobSource.WORKDAY, external_id=bare,
                  company="Acme", title="Engineer", url=url, description="d")
        s.add(dup)
        s.commit()
        dup_id = dup.id

    stats = apply_repair([(dup_id, "local", "workday", bare, scoped,
                           f"{PREFIX}acme", url, "Acme")])
    assert stats["jobs"] == 0
    assert stats["skipped_would_collide"] == 1
    with get_session() as s:
        assert s.get(Job, dup_id).external_id == bare, "left untouched"
