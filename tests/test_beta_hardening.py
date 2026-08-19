"""Guards for the pre-beta hardening pass.

Each test here pins a bug that shipped and would have hit real beta users:
a 500 on the onboarding path, a résumé that failed the honesty check being
downloadable anyway, an unbounded upload on an OOM-prone container, and a
partial board fetch mass-closing live jobs.
"""
from __future__ import annotations

import pytest
from sqlmodel import SQLModel

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
