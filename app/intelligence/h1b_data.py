"""USCIS H-1B Employer Data Hub / DOL LCA ingestion.

Loads the *public* employer sponsorship record into the H1BSponsor table and
exposes a fast in-memory lookup so sponsorship scoring can be data-backed
(exact approval rates) instead of curated guesses — without a DB hit per call.

Usage:
    python -m app.intelligence.h1b_data /path/to/h1b_datahubexport.csv

The CSV is public and free to download from:
    https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub
Column names vary by year, so detection is fuzzy. Nothing here is tenant-scoped
— it's public reference data shared by every user.
"""
from __future__ import annotations

import csv
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

_CACHE: Optional[dict] = None   # {employer_key: {"approvals","denials","rate","year","name"}}
_CACHE_AT: float = 0.0          # when the cache was last built (epoch seconds)
_CACHE_TTL: float = 600.0       # reload at most every 10 min so uploads propagate

# Last ingest result, surfaced to the admin upload page so background errors
# (bad columns, wrong file) are visible instead of silently writing 0 rows.
LAST_INGEST: dict = {"rows": 0, "error": "", "headers": [], "at": None}

_SUFFIXES = re.compile(
    r"[,\.]?\s*\b(inc|inc\.|llc|l\.l\.c|ltd|corp|corporation|co|company|"
    r"incorporated|plc|lp|llp|the)\b\.?", re.IGNORECASE
)


def normalize(name: str) -> str:
    """Normalize an employer name for matching (lowercase, strip legal suffixes)."""
    n = (name or "").lower().strip()
    n = _SUFFIXES.sub("", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _find_col(headers: list[str], *needles: str) -> Optional[str]:
    for h in headers:
        hl = h.lower()
        if all(n in hl for n in needles):
            return h
    return None


def _read_text(path: str) -> str:
    """Read the CSV as text, auto-detecting encoding (USCIS ships UTF-16!)."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
    # Strip stray BOM / null bytes that survive a mis-encoded export.
    return text.replace("\x00", "").replace("﻿", "")


def _clean_header(h: str) -> str:
    return (h or "").lstrip("﻿").replace("\x00", "").strip()


def _open_reader(path: str):
    """Return a DictReader over the decoded text, delimiter auto-sniffed,
    with cleaned header names. (USCIS files are UTF-16 + have a junk leading
    'Line by line' column and trailing spaces in some headers.)"""
    import io
    text = _read_text(path)
    sample = text[:8192]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except Exception:
        first = sample.splitlines()[0] if sample else ""
        delim = max(",\t;|", key=lambda d: first.count(d)) if first else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    orig = reader.fieldnames or []
    reader.fieldnames = [_clean_header(h) for h in orig]
    return reader


def ingest_csv(path: str) -> int:
    """Load a USCIS H-1B Employer Data Hub CSV into the H1BSponsor table.
    Idempotent per fiscal year (replaces that year's rows). Fast bulk insert."""
    import datetime as _dt
    from app.db.init_db import get_session, init_db
    from app.db.models import H1BSponsor
    from sqlmodel import delete
    init_db()
    LAST_INGEST.update(rows=0, error="", headers=[], at=_dt.datetime.utcnow().isoformat())

    reader = _open_reader(path)
    headers = reader.fieldnames or []
    LAST_INGEST["headers"] = headers
    emp_col = (_find_col(headers, "employer") or _find_col(headers, "petitioner")
               or _find_col(headers, "company") or _find_col(headers, "name"))
    if not emp_col:
        raise ValueError("Couldn't find an employer/company column. "
                         f"Columns seen: {headers}")
    year_col = _find_col(headers, "fiscal") or _find_col(headers, "year")
    appr_cols = [h for h in headers if "approval" in h.lower()]
    deny_cols = [h for h in headers if "denial" in h.lower()]
    if not appr_cols:
        appr_cols = [h for h in headers if h.lower().strip() in ("approved", "approvals", "certified")]
    if not deny_cols:
        deny_cols = [h for h in headers if h.lower().strip() in ("denied", "denials")]
    wage_col = _find_col(headers, "wage", "level")

    agg: dict = {}
    for row in reader:
        name = (row.get(emp_col) or "").strip()
        if not name:
            continue
        key = normalize(name)
        if not key:
            continue
        year = None
        if year_col:
            try:
                year = int(re.sub(r"\D", "", row.get(year_col) or "") or 0) or None
            except ValueError:
                year = None
        ap = sum(_to_int(row.get(c)) for c in appr_cols)
        dn = sum(_to_int(row.get(c)) for c in deny_cols)
        cur = agg.setdefault((key, year), {"name": name, "ap": 0, "dn": 0, "wage": ""})
        cur["ap"] += ap
        cur["dn"] += dn
        if wage_col and not cur["wage"]:
            cur["wage"] = (row.get(wage_col) or "").strip()

    if not agg:
        raise ValueError(f"Parsed 0 rows. Detected employer column '{emp_col}'. "
                         f"Columns seen: {headers}")

    now = _dt.datetime.utcnow()
    objs = []
    years_present = set()
    for (key, year), v in agg.items():
        years_present.add(year)
        total = v["ap"] + v["dn"]
        objs.append(H1BSponsor(
            employer_key=key, employer_name=v["name"][:300], fiscal_year=year,
            approvals=v["ap"], denials=v["dn"],
            approval_rate=(v["ap"] / total) if total else 0.0,
            typical_wage_level=v["wage"][:40], updated_at=now,
        ))

    written = 0
    with get_session() as session:
        # Idempotent: clear the fiscal years we're about to (re)load, then bulk add.
        for y in years_present:
            session.exec(delete(H1BSponsor).where(H1BSponsor.fiscal_year == y))
        session.commit()
        for i in range(0, len(objs), 1000):
            session.add_all(objs[i:i + 1000])
            session.commit()
            written += len(objs[i:i + 1000])
    refresh_cache()
    LAST_INGEST.update(rows=written)
    log.info("Ingested %d H-1B employer-year rows from %s", written, path)
    return written


def _to_int(x) -> int:
    try:
        return int(re.sub(r"\D", "", str(x or "")) or 0)
    except ValueError:
        return 0


def refresh_cache() -> None:
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0


def _load_cache() -> dict:
    """Build {employer_key: best-record} from the DB (latest year wins). Cached
    in-process with a short TTL so a fresh upload propagates to every worker
    within minutes — no restart needed."""
    global _CACHE, _CACHE_AT
    import time as _time
    if _CACHE is not None and (_time.time() - _CACHE_AT) < _CACHE_TTL:
        return _CACHE
    cache: dict = {}
    try:
        from app.db.init_db import get_session
        from app.db.models import H1BSponsor
        from sqlmodel import select
        with get_session() as session:
            for row in session.exec(select(H1BSponsor)).all():
                prev = cache.get(row.employer_key)
                if not prev or (row.fiscal_year or 0) >= prev["year"]:
                    cache[row.employer_key] = {
                        "approvals": row.approvals, "denials": row.denials,
                        "rate": row.approval_rate, "year": row.fiscal_year or 0,
                        "name": row.employer_name, "wage": row.typical_wage_level,
                    }
    except Exception as e:
        log.debug("H-1B cache load skipped: %s", e)
    import time as _time
    _CACHE = cache
    _CACHE_AT = _time.time()
    return cache


def lookup(company: str) -> Optional[dict]:
    """O(1) lookup of an employer's public H-1B record (None if absent/empty)."""
    if not company:
        return None
    cache = _load_cache()
    if not cache:
        return None
    key = normalize(company)
    rec = cache.get(key)
    if rec:
        return rec
    # Lenient: match when the normalized name is a prefix of a known employer.
    for k, v in cache.items():
        if k.startswith(key) and len(key) >= 4:
            return v
    return None


if __name__ == "__main__":
    import sys, os
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m app.intelligence.h1b_data <csv_path>")
        raise SystemExit(1)
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"\n❌ File not found: {path}")
        print(f"   Current directory: {os.getcwd()}")
        print("   This shell is the SERVER — it can't see files on your laptop.")
        print("   Either download the CSV onto this box first, e.g.:")
        print('     curl -L -o h1b.csv "<direct USCIS CSV link>"')
        print("   then re-run. Or run this command on your laptop with")
        print("   DATABASE_URL set to your Supabase connection string.\n")
        raise SystemExit(2)
    n = ingest_csv(path)
    print(f"Ingested {n} employer-year rows.")
