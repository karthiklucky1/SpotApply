"""Index declarations must not fight each other.

Jul 29 incident chain: a query on job(user_id, company, title) was measured at
93% of all database time and drained the Disk IO budget; the fix was a new
composite index plus dropping a redundant one. But index DDL lives in THREE
places in this repo, and they disagreed:

  1. models.py __table_args__      — only applied when create_all() builds a
                                     table FRESH (never on the live DB)
  2. init_db._PERF_INDEXES         — applied at EVERY startup
  3. db_maintenance.LEFTOVER_INDEXES — dropped by the --fix-indexes runbook

`ix_job_user_closed` was in both (2) and (3): every deploy re-created the index
the runbook had just dropped, re-running a multi-minute CREATE INDEX
CONCURRENTLY on a 764k-row table. And the two indexes that fixed the incident
were in (1) only, so any restore or fresh environment reinstated the incident.

These tests make that class of disagreement impossible to reintroduce.
"""
from __future__ import annotations

from app.db.init_db import _PERF_INDEXES
from app.db.models import Job

# Indexes on hot paths that MUST exist in production, not merely on a fresh DB.
CRITICAL_JOB_INDEXES = {
    # ghost-detector repost check + mark_ghost_jobs board close
    "ix_job_user_company_title",
    # adoption / retention / analytics recency filters
    "ix_job_user_discovered",
}


def _perf_index_names() -> set[str]:
    return {name for name, _table, _cols in _PERF_INDEXES}


def test_perf_indexes_and_runbook_drops_never_overlap():
    """An index the runbook drops must not be re-created at every startup."""
    from scripts.db_maintenance import LEFTOVER_INDEXES

    overlap = _perf_index_names() & set(LEFTOVER_INDEXES)
    assert not overlap, (
        f"{sorted(overlap)} is dropped by db_maintenance --fix-indexes but "
        f"re-created by _PERF_INDEXES on every startup — each deploy would "
        f"rebuild it and undo the runbook."
    )


def test_critical_hot_path_indexes_are_created_on_existing_databases():
    """models.py __table_args__ is NOT enough: create_all only builds indexes
    on a fresh table, so hot-path indexes must also be in _PERF_INDEXES or they
    will silently not exist in production (or after a restore)."""
    missing = CRITICAL_JOB_INDEXES - _perf_index_names()
    assert not missing, (
        f"{sorted(missing)} declared in models.py but absent from "
        f"init_db._PERF_INDEXES — they would never be created on the live "
        f"database. Add them to _PERF_INDEXES."
    )


def test_critical_indexes_also_declared_on_the_model():
    """Kept in sync the other way, so a fresh database gets them too."""
    declared = {
        ix.name for ix in Job.__table__.indexes  # type: ignore[attr-defined]
    }
    missing = CRITICAL_JOB_INDEXES - declared
    assert not missing, (
        f"{sorted(missing)} in _PERF_INDEXES but not declared on the Job model "
        f"— a freshly created database would lack them."
    )


def test_perf_index_declarations_are_well_formed():
    """Each entry is (name, table, columns) with a parenthesized column list —
    the strings are interpolated straight into CREATE INDEX DDL."""
    seen: set[str] = set()
    for entry in _PERF_INDEXES:
        assert len(entry) == 3, f"malformed entry: {entry!r}"
        name, table, cols = entry
        assert name and table and cols, f"empty field in {entry!r}"
        assert name not in seen, f"duplicate index name {name!r} in _PERF_INDEXES"
        seen.add(name)
        assert cols.strip().startswith("("), (
            f"{name}: column list must be parenthesized for the DDL, got {cols!r}"
        )
        # Guard against SQL being smuggled through these constants.
        assert ";" not in cols and "--" not in cols, f"{name}: suspicious DDL {cols!r}"


def test_no_duplicate_column_lists_on_job():
    """Two indexes over the identical column list is pure write amplification —
    exactly the ix_job_user_closed / ix_job_user_open redundancy we removed."""
    by_cols: dict[str, list[str]] = {}
    for name, table, cols in _PERF_INDEXES:
        if table != "job":
            continue
        key = cols.replace(" ", "").lower()
        by_cols.setdefault(key, []).append(name)
    dupes = {c: n for c, n in by_cols.items() if len(n) > 1}
    assert not dupes, f"duplicate index column lists on job: {dupes}"
