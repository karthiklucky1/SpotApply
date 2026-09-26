# Runbook — tenant-scoped job identity repair (production)

Status: **prepared, dry-run only, NOT applied.** Applying it writes to about
half a million production rows and needs an explicit go-ahead.

## What it fixes

Workday, BambooHR and Teamtailor requisition ids are unique only inside one
employer (tenant). Rows written before tenant scoping store the bare id
(`R29845`); newer rows store `acme:R29845`. Read-only dry run on
2026-09-26 (aggregates only, no user data):

| source | unscoped rows | scoped rows | unscoped still open | old/new pairs | pairs w/ app on old | on both |
|---|---:|---:|---:|---:|---:|---:|
| workday | 457,406 | 6,641 | 361,316 | 5,196 | 1 | 1 |
| bamboohr | 5,228 | 13,320 | 3,542 | 13,251 | 4 | 0 |
| teamtailor | 26,303 | 1,885 | 19,529 | 1,201 | 2 | 1 |

A *pair* = the same (user, source, raw id) stored both ways — one posting shown
twice and double-counted. The in-place rewrite skips pairs forever, so they are
resolved separately (`--resolve-pairs`): the copy with an application keeps its
place, the other is **closed** with a marker (never deleted, never re-dated),
and a pair where both carry applications is left for a human (2 in production).

## Steps

1. **Freeze the lanes' view of the rows** — not required (every write is
   idempotent and per-row), but run it off-peak.
2. **Backup (restorable, column-level)** — in the Supabase SQL editor:
   ```sql
   CREATE TABLE backup_job_identity_20260926 AS
     SELECT id, external_id, is_closed, closed_reason
     FROM job WHERE source IN ('WORKDAY','BAMBOOHR','TEAMTAILOR');
   CREATE TABLE backup_geo_identity_20260926 AS
     SELECT id, source, external_id FROM job_geography
     WHERE source IN ('workday','bamboohr','teamtailor');
   CREATE TABLE backup_ctx_identity_20260926 AS
     SELECT id, source, external_id FROM job_hiring_context
     WHERE source IN ('workday','bamboohr','teamtailor');
   CREATE TABLE backup_live_identity_20260926 AS
     SELECT id, source, external_id FROM job_liveness
     WHERE source IN ('workday','bamboohr','teamtailor');
   SELECT (SELECT count(*) FROM backup_job_identity_20260926) AS job_rows;
   ```
   Also confirm a point-in-time-recovery window covers the change
   (Supabase dashboard → Database → Backups).
3. **Dry run** (from a machine with `DATABASE_URL` set; writes nothing):
   ```
   python -m scripts.diagnose_job_identity --resolve-pairs --limit 50000
   ```
   Check the "derived tenant per source" table looks like employers, and that
   `repairable` / pair counts match the table above.
4. **Apply in bounded batches** (ascending id, 200 rows per transaction), and
   repeat until `repairable` and the pair counts reach 0:
   ```
   python -m scripts.diagnose_job_identity --apply --resolve-pairs --limit 20000
   ```
5. **Validate** after each run:
   ```sql
   -- no application lost its job, none was closed by the repair
   SELECT count(*) FROM application a JOIN job j ON j.id = a.job_id
    WHERE j.closed_reason LIKE 'Superseded: duplicate of the tenant-scoped copy%';   -- expect 0
   -- pairs remaining open on both sides
   SELECT count(*) FROM job o JOIN job n ON n.user_id IS NOT DISTINCT FROM o.user_id
     AND n.source = o.source AND n.external_id LIKE '%:' || o.external_id
    WHERE position(':' in o.external_id) = 0 AND NOT o.is_closed AND NOT n.is_closed; -- expect 2
   -- nothing re-dated
   SELECT count(*) FROM job j JOIN backup_job_identity_20260926 b USING (id)
    WHERE j.external_id IS DISTINCT FROM b.external_id AND j.is_closed IS DISTINCT FROM b.is_closed; -- rows both renamed AND closed: expect 0
   ```
6. **Recompute what depends on identity**: re-run
   `python -m scripts.recheck_eligibility` (dry run first) so untouched
   shortlist entries are re-decided against the renamed geography rows.

## Rollback

```sql
UPDATE job j SET external_id = b.external_id, is_closed = b.is_closed,
                 closed_reason = b.closed_reason
  FROM backup_job_identity_20260926 b WHERE j.id = b.id
   AND (j.external_id, j.is_closed, j.closed_reason) IS DISTINCT FROM
       (b.external_id, b.is_closed, b.closed_reason);
UPDATE job_geography g SET external_id = b.external_id
  FROM backup_geo_identity_20260926 b WHERE g.id = b.id AND g.external_id <> b.external_id;
-- same for job_hiring_context and job_liveness with their backup tables
```
Pair closures alone can be undone with
`UPDATE job SET is_closed=false, closed_reason=NULL WHERE closed_reason='Superseded: duplicate of the tenant-scoped copy (identity repair)';`

## What it never does

Deletes a row; changes `first_seen`, `posted_at` or `discovered_at` (no old
job is made to look new); touches an application; re-scores anything.
