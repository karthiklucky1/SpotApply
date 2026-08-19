"""Guards for the pre-beta hardening pass.

Each test here pins a bug that shipped and would have hit real beta users:
a 500 on the onboarding path, a résumé that failed the honesty check being
downloadable anyway, an unbounded upload on an OOM-prone container, and a
partial board fetch mass-closing live jobs.
"""
from __future__ import annotations

import pytest
from sqlmodel import SQLModel, select

from app.api import server


# ── Grounding gate ───────────────────────────────────────────────────────────

def test_grounding_failed_resume_cannot_be_downloaded(monkeypatch):
    """An ERROR application means grounding (or the doctor) rejected the doc.

    Tailoring still records tailored_resume_path on that path, so a download
    route that only checks "does the file exist" handed the user exactly the
    résumé the anti-hallucination check refused to approve.
    """
    from fastapi import HTTPException
    from app.db.models import Application, ApplicationStatus, Job
    from app.db.init_db import get_session

    with get_session() as s:
        job = Job(source="greenhouse", external_id="ground-1", company="G",
                  title="T", url="http://x/g1", description="d")
        s.add(job); s.commit(); s.refresh(job)
        app_row = Application(job_id=job.id, status=ApplicationStatus.ERROR,
                              apply_track="manual",
                              tailored_resume_path="/tmp/does-not-matter.docx",
                              notes="Grounding failed: invented an employer")
        s.add(app_row); s.commit(); s.refresh(app_row)
        app_id = app_row.id

    monkeypatch.setattr(server, "_require_owned_application",
                        lambda request, application_id: "local")
    with pytest.raises(HTTPException) as e:
        server.download_tailored_resume(application_id=app_id, request=None)
    assert e.value.status_code == 409
    assert "grounding" in str(e.value.detail).lower()


# ── Upload ceilings ──────────────────────────────────────────────────────────

def test_upload_ceilings_match_what_the_ui_promises():
    """The dashboard's dropzone says "max 5MB"; the server must agree.

    It previously accepted any size and read the whole body into memory on a
    container that has already been OOM-killed once.
    """
    assert server._MAX_RESUME_BYTES == 5 * 1024 * 1024
    assert server._MAX_LINKEDIN_PDF_BYTES >= server._MAX_RESUME_BYTES


# ── Pagination arithmetic ────────────────────────────────────────────────────

@pytest.mark.parametrize("page,limit", [(0, 50), (-5, 50), (1, 0), (1, -3)])
def test_api_jobs_clamps_hostile_pagination(page, limit, monkeypatch):
    """limit=0 divided by zero; a negative page produced a negative OFFSET."""
    monkeypatch.setattr(server, "_get_user_id", lambda request: "local")
    out = server.api_jobs(request=None, page=page, limit=limit)
    assert out["page"] >= 1
    assert out["limit"] >= 1
    assert out["pages"] >= 0


# ── Ghost-closing only on complete fetches ───────────────────────────────────

def test_partial_board_fetch_does_not_ghost_close(monkeypatch):
    """A truncated or mid-pagination-failed fetch is a SUBSET of the board.

    Closing everything absent from a subset permanently killed live postings
    and SKIPped the applications attached to them.
    """
    import inspect
    from app.discovery import pipeline as dp

    src = inspect.getsource(dp.run_discovery)
    call_line = next((ln for ln in src.splitlines()
                      if "mark_ghost_jobs(scraper.name" in ln), None)
    assert call_line is not None, "ghost-close call site moved — re-point this guard"

    # The call must sit under a completeness check, not merely a non-empty one.
    guard = next((ln for ln in src.splitlines()
                  if "fetch_complete" in ln and "if " in ln), None)
    assert guard is not None, (
        "run_discovery must gate mark_ghost_jobs on the scraper reporting a "
        "COMPLETE fetch; without it a truncated board closes every posting "
        "the scraper never managed to read")
    assert 'getattr(scraper, "fetch_complete", True)' in guard, (
        "default must be True so scrapers that always return whole boards "
        "keep ghost-closing")


def test_workday_marks_truncated_fetches_incomplete():
    """The scraper must expose the flag the pipeline reads."""
    from app.discovery.workday import WorkdayScraper
    import inspect
    src = inspect.getsource(WorkdayScraper.fetch)
    assert "self.fetch_complete = False" in src, (
        "Workday must flag partial fetches, or the pipeline will ghost-close "
        "every posting it did not manage to read")


# ── Account-deletion schema coupling ─────────────────────────────────────────

def test_every_user_scoped_table_is_reachable_in_reverse_fk_order():
    """The deletion loop iterates reversed(sorted_tables); it must cover them all."""
    reachable = {t.name for t in SQLModel.metadata.sorted_tables}
    scoped = {name for name, t in SQLModel.metadata.tables.items()
              if "user_id" in t.columns
              or any(c in t.columns for c in server._EXTRA_OWNER_COLUMNS.get(name, ()))}
    assert scoped <= reachable


# ── Target roles follow the résumé ───────────────────────────────────────────

def _run_extract_profile(monkeypatch, uid, resume_text, extracted):
    """Drive the real /api/resume/extract-profile route with the LLM switched off."""
    from app.config import Settings, settings as _settings
    monkeypatch.setattr(Settings, "use_supabase", property(lambda self: False))
    monkeypatch.setattr(_settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(server, "_require_user", lambda request: uid)
    monkeypatch.setattr("app.matching.pipeline._load_resume",
                        lambda user_id=None: resume_text)
    monkeypatch.setattr("app.intelligence.resume_basic_extract.basic_extract_profile",
                        lambda text: extracted)
    monkeypatch.setattr(server, "analyze_resume_text",
                        lambda text, uid_: {"findings": []}, raising=False)

    class _BG:
        def add_task(self, *a, **k):
            pass

    # A real Request: the route is wrapped by slowapi's rate limiter, which
    # rejects anything else.
    from starlette.requests import Request as _Request
    req = _Request({"type": "http", "method": "POST", "path": "/api/resume/extract-profile",
                    "headers": [], "query_string": b"", "client": ("test", 1),
                    "app": server.app})
    return server.extract_profile_from_resume(request=req, background_tasks=_BG())


def test_target_roles_are_re_derived_when_a_new_resume_is_uploaded(monkeypatch):
    """Uploading a new résumé must move the target roles with it.

    Someone whose CV goes from "AI Engineer" to "Software Developer" was left
    matching against the roles of the résumé they replaced, because roles were
    seeded ONLY when the list was empty.
    """
    from app.db.init_db import get_session
    from app.db.models import UserProfile

    uid = "roles-refresh-user"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="AI Engineer",
                          target_roles_auto=True))
        s.commit()

    _run_extract_profile(monkeypatch, uid, "Software Developer, 5 years of Java.", {
        "current_title": "Software Developer",
        "suggested_target_roles": ["Software Developer", "Backend Engineer"],
    })

    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        assert "AI Engineer" not in p.target_roles, (
            "the roles of the REPLACED résumé must not survive the new upload")
        assert "Software Developer" in p.target_roles
        assert p.target_roles_auto is True


def test_a_new_resume_does_not_touch_roles_the_user_typed(monkeypatch):
    """The other half: hand-edited roles survive every later upload."""
    from app.db.init_db import get_session
    from app.db.models import UserProfile

    uid = "roles-protected-user"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="Developer Advocate",
                          target_roles_auto=False))
        s.commit()

    _run_extract_profile(monkeypatch, uid, "Software Developer, 5 years of Java.", {
        "current_title": "Software Developer",
        "suggested_target_roles": ["Software Developer"],
    })

    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        assert p.target_roles == "Developer Advocate"
        assert p.target_roles_auto is False


def test_manually_edited_roles_are_not_overwritten_by_a_later_upload(monkeypatch):
    """PUT /api/target-roles hands the list to the user for good."""
    from app.db.init_db import get_session
    from app.db.models import UserProfile

    uid = "roles-manual-user"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="AI Engineer",
                          target_roles_auto=True))
        s.commit()

    monkeypatch.setattr(server, "_get_user_id", lambda request: uid)

    class _Body:
        roles = ["Staff Platform Engineer"]

    server.update_target_roles(request=None, body=_Body(), background_tasks=None)

    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        assert p.target_roles == "Staff Platform Engineer"
        assert p.target_roles_auto is False, (
            "a hand-edited list must survive the next résumé upload")


def test_clearing_roles_hands_them_back_to_the_resume(monkeypatch):
    from app.db.init_db import get_session
    from app.db.models import UserProfile

    uid = "roles-cleared-user"
    with get_session() as s:
        s.add(UserProfile(user_id=uid, target_roles="Something",
                          target_roles_auto=False))
        s.commit()

    monkeypatch.setattr(server, "_get_user_id", lambda request: uid)

    class _Body:
        roles = []

    server.update_target_roles(request=None, body=_Body(), background_tasks=None)

    with get_session() as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == uid)).first()
        assert p.target_roles_auto is True
