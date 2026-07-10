"""Registry health check — is the ~22K company registry actually working?

Usage:
    python scripts/registry_health.py               # DB stats + live probes
    python scripts/registry_health.py --no-probe    # DB stats only (offline)
    python scripts/registry_health.py --probe-n 5   # boards probed per ATS

Three layers of verification:
  1. SEEDED   — how many boards per ATS/source are registered.
  2. REACHABLE— live-probe N random boards per ATS via their public endpoint
                (does the board still exist, how many jobs does it serve).
  3. PRODUCING— jobs actually inserted into the Job table from direct-ATS
                sources in the last 24h / 7d, and how many boards the rotation
                has touched (last_seen) — proves discovery is consuming it.
"""
from __future__ import annotations

import os
import random
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.db.init_db import get_session, init_db  # noqa: E402
from app.db.models import CompanyRegistry, Job, JobSource  # noqa: E402

# Public, unauthenticated list endpoints per ATS (same ones discovery uses).
PROBES = {
    JobSource.GREENHOUSE: lambda s, c: c.get(f"https://api.greenhouse.io/v1/boards/{s}/jobs"),
    JobSource.LEVER: lambda s, c: c.get(f"https://api.lever.co/v0/postings/{s}?mode=json&limit=1"),
    JobSource.ASHBY: lambda s, c: c.get(f"https://api.ashbyhq.com/posting-api/job-board/{s}"),
    JobSource.SMARTRECRUITERS: lambda s, c: c.get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1"),
    JobSource.WORKABLE: lambda s, c: c.get(f"https://apply.workable.com/api/v1/widget/accounts/{s}"),
    JobSource.RECRUITEE: lambda s, c: c.get(f"https://{s}.recruitee.com/api/offers"),
    JobSource.PERSONIO: lambda s, c: c.get(f"https://{s}.jobs.personio.de/xml?language=en"),
}


def _job_count(ats: JobSource, resp) -> str:
    try:
        d = resp.json()
        for key in ("jobs", "offers", "content"):
            if isinstance(d, dict) and isinstance(d.get(key), list):
                return str(len(d[key]))
        if isinstance(d, list):
            return str(len(d))
        if isinstance(d, dict) and "totalFound" in d:
            return str(d["totalFound"])
    except Exception:
        pass
    return "?"


def main() -> int:
    no_probe = "--no-probe" in sys.argv
    probe_n = 3
    if "--probe-n" in sys.argv:
        probe_n = int(sys.argv[sys.argv.index("--probe-n") + 1])

    init_db()

    # ── 1. SEEDED ────────────────────────────────────────────────────────────
    with get_session() as session:
        rows = session.exec(select(CompanyRegistry)).all()
    total = len(rows)
    by_ats = Counter(r.ats for r in rows)
    by_source = Counter(r.source for r in rows)
    active = sum(1 for r in rows if r.is_active)
    seen_7d = sum(1 for r in rows if r.last_seen and r.last_seen > datetime.utcnow() - timedelta(days=7))

    print(f"═══ 1. SEEDED — {total} boards registered ═══")
    for ats, n in by_ats.most_common():
        print(f"  {ats.value:<16} {n}")
    print("  by source:", dict(by_source))
    print(f"  active: {active}/{total}  |  scraped in last 7d: {seen_7d}")
    if total < 1000:
        print("  ⚠ Registry small — run: python scripts/seed_registry.py --open-datasets")

    # ── 2. REACHABLE — live probes ───────────────────────────────────────────
    if not no_probe:
        import httpx
        print(f"\n═══ 2. REACHABLE — probing {probe_n} random boards per ATS ═══")
        ok = bad = 0
        with httpx.Client(timeout=12, follow_redirects=True,
                          headers={"User-Agent": "HirePath registry health"}) as client:
            for ats, probe in PROBES.items():
                pool = [r for r in rows if r.ats == ats]
                for r in random.sample(pool, min(probe_n, len(pool))):
                    try:
                        resp = probe(r.slug, client)
                        live = resp.status_code == 200
                        ok += live
                        bad += (not live)
                        jobs = _job_count(ats, resp) if live else "-"
                        print(f"  [{'OK ' if live else str(resp.status_code)}] {ats.value:<14} {r.slug:<28} jobs={jobs}")
                    except Exception as e:
                        bad += 1
                        print(f"  [ERR] {ats.value:<14} {r.slug:<28} {type(e).__name__}")
        if ok + bad:
            pct = round(100 * ok / (ok + bad))
            print(f"  reachable: {ok}/{ok + bad} ({pct}%) — 70%+ is healthy; the validator deactivates dead boards over time")

    # ── 3. PRODUCING — jobs flowing into the DB ─────────────────────────────
    direct = set(PROBES) | {JobSource.WORKDAY}
    with get_session() as session:
        jobs = session.exec(select(Job)).all()
    now = datetime.utcnow()
    d1 = [j for j in jobs if j.source in direct and j.discovered_at > now - timedelta(days=1)]
    d7 = [j for j in jobs if j.source in direct and j.discovered_at > now - timedelta(days=7)]
    print(f"\n═══ 3. PRODUCING — direct-ATS jobs in the Job table ═══")
    print(f"  last 24h: {len(d1)}   last 7d: {len(d7)}   all-time: {sum(1 for j in jobs if j.source in direct)}")
    for ats, n in Counter(j.source for j in d7).most_common():
        print(f"    {ats.value:<16} {n} (7d)")
    if total and not seen_7d and not d7:
        print("  ⚠ Registry seeded but nothing scraped/produced yet — the 2h fresh lane")
        print("    and 6h scheduler rotate max_boards_per_run boards per pass; a full")
        print(f"    sweep of {total} boards takes ~{max(1, total // 300 // 12)} day(s). Trigger one now:")
        print("    curl -X POST localhost:8000/run/discovery  (or Discover Jobs in the UI)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
