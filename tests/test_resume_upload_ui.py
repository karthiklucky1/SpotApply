"""A new user is told to upload a resume, and sees the upload happening.

2026-09-29 (counts only): one of three new sign-ups opened the dashboard twice
on a phone and never uploaded a resume — the only upload prompt sat in the
empty job list below the fold, and the onboarding modal opens only on
Discover. Another said nothing on screen showed that the upload was still
working while the resume was read. Static checks on the template: every path
that uploads a resume must show progress, clear it on success AND failure, and
a user without a resume must see the Start-here card.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "app/templates/dashboard.html").read_text()


def _function(name: str) -> str:
    m = re.search(r"(async\s+)?function\s+" + name + r"\s*\(", HTML)
    assert m, name
    depth, i = 0, HTML.index("{", m.end())
    for j in range(i, len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[j], 0)
        if depth == 0:
            return HTML[m.start():j + 1]
    raise AssertionError(name)


def test_every_upload_path_shows_and_clears_progress():
    uploaders = [n for n in ("uploadResume", "obUploadResume", "checkAndApplyTempData")]
    for name in uploaders:
        body = _function(name)
        assert "/api/resume/upload" in body, name
        assert body.index("setResumeBusy(") < body.index("/api/resume/upload"), \
            f"{name}: progress must show before the upload starts"
        assert "clearResumeBusy(" in body, f"{name}: progress must clear"
    # No other code path uploads a resume without the indicator.
    assert HTML.count("fetch('/api/resume/upload'") == len(uploaders)


def test_a_failed_upload_clears_the_indicator():
    body = _function("uploadResume")
    fail = body[body.index("if (!res.ok)"):body.index("return", body.index("if (!res.ok)"))]
    assert "clearResumeBusy()" in fail


def test_the_indicator_is_in_the_sidebar_and_on_phones():
    assert 'id="sb-resume-label"' in HTML and 'id="sb-resume-status"' in HTML
    assert 'id="resume-busy-pill"' in HTML and 'aria-live="polite"' in HTML
    busy = _function("setResumeBusy")
    for el in ("sb-resume-status", "sb-resume-label", "resume-busy-pill"):
        assert el in busy


def test_leaving_mid_upload_warns_but_our_own_reload_does_not():
    assert "addEventListener('beforeunload'" in HTML
    body = _function("checkAndApplyTempData")
    reload_at = body.index("window.location.reload()")
    assert "_resumeBusy = false" in body[:reload_at]


def test_a_user_without_a_resume_sees_the_start_here_card():
    assert 'id="start-here-card"' in HTML
    status = _function("loadResumeStatus")
    assert "_renderStartHere(" in status
    card = _function("_renderStartHere")
    assert "openResumeUpload()" in card and "hasResume" in card
    # One upload button, not two: the empty job list only points at the card.
    empty_copy = HTML[HTML.index("empty.innerHTML ="):HTML.index("empty.innerHTML =") + 600]
    assert "openResumeUpload" not in empty_copy
