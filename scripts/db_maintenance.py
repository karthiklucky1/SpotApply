"""One-shot production DB maintenance — run from the Railway service shell
(the app's own direct Postgres connection has NO 2-minute gateway cap, unlike
the Supabase dashboard editor).

Default run is a read-only REPORT. Every mutating action is opt-in:

  python -m scripts.db_maintenance                       # report only
  python -m scripts.db_maintenance --terminate-stale     # kill idle-in-tx > 30 min
  python -m scripts.db_maintenance --set-idle-timeout    # 10-min idle-in-tx cap, permanent
  python -m scripts.db_maintenance --fix-indexes         # drop invalid leftovers + build the real one
  python -m scripts.db_maintenance --purge-funnel 60     # batched funnel_events retention

Recommended order (see docs/CARDRACE_DESIGN.md ops notes):
  1. --terminate-stale --set-idle-timeout   (clears the blocker + prevents recurrence)
  2. wait for the Disk IO budget to recover
  3. --fix-indexes                          (CONCURRENTLY, safe under load, may take minutes)
  4. --purge-funnel 60                      (spaced batches)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db.init_db import engine  # noqa: E402

# The three artifacts from the aborted dashboard attempts + the superseded name.
LEFTOVER_INDEXES = ("ix_job_user_closed", "ix_job_user_source_company")
TARGET_INDEX = "ix_job_user_company_title"
TARGET_INDEX_DDL = f"CREATE INDEX CONCURRENTLY {TARGET_INDEX} ON job (user_id, company, title)"


def _conn():
    if not engine.url.get_backend_name().startswith("postgres"):
        print(f"Refusing to run: engine is {engine.url.get_backend_name()}, not Postgres. "
              f"This script is production-DB maintenance only.")
        raise SystemExit(2)
    # CONCURRENTLY and pg_terminate_backend must run outside a transaction.
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    # The index build may legitimately take minutes at a throttled IO budget —
    # don't let the app's default statement timeout kill it half-way (that is
    # exactly how the invalid stubs were made).
    conn.execute(text("SET statement_timeout = '30min'"))
    return conn


def report(c) -> None:
    print("=== Stale transactions (state = 'idle in transaction') ===")
    rows = c.execute(text(
        "SELECT pid, now() - xact_start AS tx_age, usename, application_name, "
        "       left(coalesce(query, ''), 70) AS last_query "
        "FROM pg_stat_activity "
        "WHERE state = 'idle in transaction' AND pid <> pg_backend_pid() "
        "ORDER BY xact_start")).fetchall()
    for r in rows:
        print(f"  pid={r.pid}  age={r.tx_age}  user={r.usename}  q={r.last_query!r}")
    if not rows:
        print("  none")

    print("\n=== job indexes (validity / size / scans) ===")
    for r in c.execute(text(
            "SELECT c.relname, i.indisvalid, i.indisready, "
            "       pg_size_pretty(pg_relation_size(c.oid)) AS size, s.idx_scan "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = c.oid "
            "WHERE t.relname = 'job' ORDER BY c.relname")).fetchall():
        mark = "" if r.indisvalid else "  <-- INVALID"
        print(f"  {r.relname:<38} valid={r.indisvalid} size={r.size} scans={r.idx_scan}{mark}")

    n = c.execute(text(
        "SELECT count(*) FROM funnel_events "
        "WHERE created_at < now() - interval '60 days'")).scalar()
    print(f"\n=== funnel_events older than 60 days: {n} rows ===")

    t = c.execute(text("SHOW idle_in_transaction_session_timeout")).scalar()
    print(f"=== idle_in_transaction_session_timeout: {t} (0 = unlimited — the leak enabler) ===")


def terminate_stale(c, minutes: int) -> None:
    rows = c.execute(text(
        "SELECT pid, now() - xact_start AS tx_age "
        "FROM pg_stat_activity "
        "WHERE state = 'idle in transaction' "
        "  AND xact_start < now() - make_interval(mins => :m) "
        "  AND pid <> pg_backend_pid()"), {"m": minutes}).fetchall()
    if not rows:
        print(f"No idle-in-transaction sessions older than {minutes} min.")
        return
    for r in rows:
        ok = c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": r.pid}).scalar()
        print(f"terminated pid={r.pid} (tx age {r.tx_age}): {ok}")


def set_idle_timeout(c, minutes: int) -> None:
    db = c.execute(text("SELECT current_database()")).scalar()
    c.execute(text(f'ALTER DATABASE "{db}" '
                   f"SET idle_in_transaction_session_timeout = '{int(minutes)}min'"))
    print(f"idle_in_transaction_session_timeout = {minutes}min set on {db} "
          f"(applies to NEW connections; existing ones keep the old setting).")


def fix_indexes(c) -> None:
    def _state(name: str):
        return c.execute(text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE ic.relname = :n"), {"n": name}).fetchone()

    for name in LEFTOVER_INDEXES:
        if _state(name) is None:
            print(f"{name}: already gone")
            continue
        print(f"dropping {name} (CONCURRENTLY)...")
        c.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
        print(f"{name}: dropped")

    st = _state(TARGET_INDEX)
    if st is not None and st.indisvalid:
        print(f"{TARGET_INDEX}: already exists and is VALID — nothing to do")
        return
    if st is not None and not st.indisvalid:
        print(f"{TARGET_INDEX}: dropping invalid stub first...")
        c.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {TARGET_INDEX}"))
    print(f"building {TARGET_INDEX} (CONCURRENTLY — minutes on a 764k-row table; "
          f"safe under load)...")
    t0 = time.time()
    c.execute(text(TARGET_INDEX_DDL))
    st = _state(TARGET_INDEX)
    print(f"{TARGET_INDEX}: built in {time.time() - t0:.0f}s, valid={bool(st and st.indisvalid)}")


def purge_funnel(c, days: int, batch: int, max_batches: int, sleep_s: float) -> None:
    total = 0
    for i in range(max_batches):
        n = c.execute(text(
            "DELETE FROM funnel_events WHERE id IN ("
            "  SELECT id FROM funnel_events "
            "  WHERE created_at < now() - make_interval(days => :d) LIMIT :b)"),
            {"d": days, "b": batch}).rowcount
        total += max(0, n or 0)
        print(f"batch {i + 1}: deleted {n} rows (total {total})")
        if not n:
            break
        time.sleep(sleep_s)   # be kind to the IO budget
    print(f"done: {total} rows deleted. Space returns via autovacuum "
          f"(needs the stale transaction gone).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--terminate-stale", nargs="?", const=30, type=int, metavar="MIN",
                    help="terminate idle-in-transaction sessions older than MIN minutes (default 30)")
    ap.add_argument("--set-idle-timeout", nargs="?", const=10, type=int, metavar="MIN",
                    help="ALTER DATABASE idle_in_transaction_session_timeout (default 10 min)")
    ap.add_argument("--fix-indexes", action="store_true",
                    help=f"drop invalid leftovers {LEFTOVER_INDEXES} + build {TARGET_INDEX}")
    ap.add_argument("--purge-funnel", nargs="?", const=60, type=int, metavar="DAYS",
                    help="batched delete of funnel_events older than DAYS (default 60)")
    ap.add_argument("--batch", type=int, default=50_000)
    ap.add_argument("--max-batches", type=int, default=10)
    ap.add_argument("--sleep", type=float, default=5.0)
    args = ap.parse_args()

    with _conn() as c:
        report(c)
        if args.terminate_stale is not None:
            print()
            terminate_stale(c, args.terminate_stale)
        if args.set_idle_timeout is not None:
            print()
            set_idle_timeout(c, args.set_idle_timeout)
        if args.fix_indexes:
            print()
            fix_indexes(c)
        if args.purge_funnel is not None:
            print()
            purge_funnel(c, args.purge_funnel, args.batch, args.max_batches, args.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
