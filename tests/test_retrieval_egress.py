"""Retrieval must not stream full job descriptions out of the database.

Retrieval and the FAISS rebuild both used `select(Job)` (SELECT *), shipping
every candidate's full multi-KB description over the wire and then reading at
most 800 characters of it. At corpus_cap=2000 rows that is ~16 MB per matching
pass, per user, every 5 minutes — which took the Supabase project to 205% of a
250 GB egress quota with 14 users and 2 MB of stored data, and was draining the
Disk IO budget.

These tests pin the two properties that keep it fixed: the SELECT list stays
narrow with the description truncated IN SQL, and `_Candidate`'s field order
stays aligned with that list (rows are unpacked positionally, so a drift would
silently swap fields rather than raise).
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import delete, select

import app.matching.matcher as mm
from app.config import settings
from app.db.init_db import get_session
from app.db.models import Job, JobSource

LONG_DESC = "Build LLM systems in Python. " * 4000  # ~112 KB


def _seed(session, **kw) -> int:
    job = Job(
        title=kw.get("title", "Senior ML Engineer"),
        company=kw.get("company", "Acme"),
        location=kw.get("location", "Remote"),
        remote=kw.get("remote", True),
        description=kw.get("description", LONG_DESC),
        source=JobSource.GREENHOUSE,
        external_id=kw.get("external_id", "egress-1"),
        url="https://x/egress-1",
        user_id=kw.get("user_id", "egress-user"),
        first_seen=datetime.utcnow(),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job.id


def _clean(session):
    session.exec(delete(Job).where(Job.user_id == "egress-user"))
    session.commit()


# ── The SELECT list ──────────────────────────────────────────────────────────

def test_candidate_columns_never_select_the_raw_description():
    """The whole point: the full column must never appear in the SELECT list."""
    cols = mm._candidate_columns()
    assert Job.description not in cols
    # The description slot is a SQL expression, not the bare column.
    assert any(getattr(c, "name", None) == "substr" for c in cols), cols


def test_candidate_columns_match_candidate_field_order():
    """Rows are unpacked positionally via `_Candidate(*row)`. If the SELECT list
    and the NamedTuple drift apart, title and company silently swap — no error,
    just wrong matches. Pin the alignment."""
    cols = mm._candidate_columns()
    assert len(cols) == len(mm._Candidate._fields)
    # Every column except the truncated description is a plain Job attribute
    # whose key must equal the corresponding field name.
    for col, field in zip(cols[:-1], mm._Candidate._fields[:-1]):
        assert col.key == field, f"{col.key} != {field}"
    assert mm._Candidate._fields[-1] == "description"


def test_truncation_covers_every_reader():
    """`_job_text` slices 800 chars and `_job_text_ce` slices
    cross_encoder_text_chars — the SQL truncation must not be shorter than
    either, or a stage silently gets starved input."""
    assert mm._retrieval_desc_chars() >= 800
    assert mm._retrieval_desc_chars() >= settings.cross_encoder_text_chars


def test_truncation_tracks_a_raised_cross_encoder_setting(monkeypatch):
    monkeypatch.setattr(settings, "cross_encoder_text_chars", 5000)
    assert mm._retrieval_desc_chars() == 5000


# ── End-to-end against a real row ────────────────────────────────────────────

def test_query_returns_a_truncated_description():
    """Seed a ~112 KB description and confirm what comes back over the wire is
    bounded, not the whole column."""
    with get_session() as session:
        _clean(session)
        jid = _seed(session)
        rows = session.exec(
            select(*mm._candidate_columns()).where(Job.id == jid)
        ).all()
        cand = mm._Candidate(*rows[0])

        assert cand.id == jid
        assert cand.title == "Senior ML Engineer"
        assert cand.company == "Acme"       # not swapped with title
        assert cand.location == "Remote"
        assert cand.remote is True
        assert len(cand.description) == mm._retrieval_desc_chars()
        assert len(LONG_DESC) > 100_000     # the row really was huge
        _clean(session)


def test_candidate_still_feeds_both_text_builders():
    """`_job_text` / `_job_text_ce` were typed for Job; they must keep working on
    the lightweight tuple, since that is all retrieval passes them now."""
    cand = mm._Candidate(
        id=1, title="ML Engineer", company="Acme", location="Remote",
        remote=True, description="Build LLM systems.",
    )
    text = mm.Matcher._job_text(cand)
    assert "ML Engineer" in text and "Acme" in text and "Build LLM systems." in text

    ce = mm.Matcher._job_text_ce(cand, settings.cross_encoder_text_chars)
    assert ce.startswith("ML Engineer")
    assert len(ce) <= settings.cross_encoder_text_chars + len("\n\n")


def test_null_description_does_not_crash_the_text_builders():
    """A posting with no description must not break the corpus build."""
    cand = mm._Candidate(id=1, title="ML Engineer", company="Acme",
                         location=None, remote=False, description=None)
    assert "ML Engineer" in mm.Matcher._job_text(cand)
    assert "ML Engineer" in mm.Matcher._job_text_ce(cand, 700)
