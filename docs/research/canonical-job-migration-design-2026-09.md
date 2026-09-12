# Canonical job identity and the hot-table split — DESIGN ONLY, NOT SHIPPED

Status: **design**. Nothing in this document has been implemented. The
hiring-context work shipped alongside it (`JobHiringContext`, `JobLiveness`)
is a deliberate first step in this direction — it proves the shared-key idea
on a new, low-risk table before anything touches `job`.

Written 2026-09-12. **The row counts below come from the operator's report,
not from a query I ran**: this session cannot reach Supabase (`CONNECT` denied
by the egress proxy for `api.supabase.com`, the pooler host, and the app
domain). Every number is therefore marked REPORTED, and every step of the plan
begins by verifying it. Nothing here should be executed on the strength of
this document alone.

## 1. The problem, as reported

| Measure | Value | Status |
|---|---|---|
| `job` rows | ~1,469,358 | REPORTED |
| open (`is_closed = false`) | ~930,114 | REPORTED |
| shared-pool rows | ~924,927 | REPORTED |
| per-user duplicate copies | ~544,400 | REPORTED |
| distinct users | ~12 | REPORTED |
| `/api/pipeline/live` under DB load | already degrading | REPORTED |

Roughly 37% of the table is duplicate copies of postings the shared pool
already holds. The duplication is structural, not accidental: uniqueness is
`(user_id, source, external_id)` (`app/db/models.py`), and
`strategy/adoption.py` copies a shared-pool posting into a per-user row so
that per-user scores and lifecycle never collide across tenants.

The expensive part is not the row count, it is what each row carries.
`Job.description` is stored untruncated, and `rerank_breakdown`,
`corporate_insights` and `hire_probability_signals` are JSON text columns on
the same row. Every duplicate copies all of it.

## 2. Why the copy exists, and what actually differs

Per-user state on `Job` today: `similarity_score`, `rerank_score`,
`rerank_reasoning`, `rerank_breakdown`, `prescore`, `blended_score`,
`hire_probability_score`, `hire_probability_signals`, `embedding_id`,
`on_role`, `prescored_at`, `scored_at`, `expired_at`, `is_closed`,
`closed_reason`, plus `user_id`.

Everything else describes the posting and is identical across copies:
`source`, `external_id`, `company`, `title`, `location`, `remote`, `url`,
`description`, `posted_at`, `content_hash`, `cross_source_slug`,
`salary_text`, `sponsorship_json`, `job_type`, `is_cap_exempt`,
`corporate_insights`, `ghost_score`, `ghost_flags`, and now `origin` /
`origin_provider`.

That split is the migration.

## 3. Target shape

```
CanonicalJob      one row per distinct posting, keyed by (source, external_id)
  └─ everything intrinsic: description, url, company, title, posted_at,
     content_hash, cross_source_slug, salary_text, sponsorship_json,
     corporate_insights, ghost_*, origin, origin_provider
UserJobMatch      one row per (user, canonical job)
  └─ user_id, canonical_job_id, prescore, rerank_score, rerank_reasoning,
     rerank_breakdown, blended_score, hire_probability_*, similarity_score,
     embedding_id, on_role, status, shortlist state, viewed_at,
     prescored_at, scored_at, expired_at
Application       unchanged, points at UserJobMatch
```

`JobHiringContext` and `JobLiveness` are already keyed exactly the way
`CanonicalJob` would be, so they need no migration when it happens. That is
the main reason they were built that way now.

## 4. Estimated savings — ARITHMETIC ONLY, NOT MEASURED

Cannot be confirmed without `pg_total_relation_size` and
`avg(length(description))`, neither of which this session can run.

Using the reported 544,400 duplicate rows, the saving is
`544,400 × (size of the intrinsic columns)`. Description length dominates.
At a plausible 3–6 KB of intrinsic text per posting the range is roughly
1.6–3.3 GB of table data, before index savings. **Do not put this number in a
plan until the two queries in §7 step 1 have been run.**

The query-time argument is firmer than the storage one. Every per-user scan —
the board query, the scoring queue, adoption — walks rows that are ~90%
identical payload. Narrowing the scanned row is what makes those faster, and
it is available without the full migration (§6).

## 5. Backward-compatibility hazards

1. `Job` is referenced across the API, every lane, the matcher, the funnel and
   the extension. A rename breaks all of it at once.
2. `Application.job_id` points at the per-user `job.id`. Repointing it at
   `UserJobMatch` touches the ownership guard `_require_owned_application`,
   which is the multi-tenancy boundary. That code is load-bearing for tenant
   isolation and must not change in the same deploy as a data move.
3. `test_account_deletion` is schema-driven and sweeps every table with a
   `user_id`. `UserJobMatch` would be user-scoped and must be added to the
   sweep, in FK order, before its parent.
4. `embedding_id` indexes into FAISS. Moving it changes index rebuild.
5. The unique constraint `(user_id, source, external_id)` is what makes the
   upsert race-safe under `ON CONFLICT DO NOTHING`. The replacement needs an
   equivalent on `(user_id, canonical_job_id)` before any writer switches.

## 6. What can be done first, independently, with real benefit

These are separable and each is deployable alone. **None is implemented.**

1. **Stop selecting whole `Job` rows on the remaining hot paths.** The
   retrieval path is already projected (`matcher._candidate_columns`, six
   columns, description truncated in SQL). `test_architecture_invariants`
   enforces this for six named files only. Extend the projection discipline to
   the board query and the scoring queue.
2. **Move the cold text off the hot row.** `description`, `rerank_breakdown`,
   `corporate_insights` and `hire_probability_signals` are large, rarely read
   during scans, and always read individually. On Postgres, large values are
   already TOASTed out of line, so the win here is smaller than it looks and
   should be measured before it is built. Verify with
   `pg_column_size(description)` percentiles first.
3. **Deduplicate the description only.** A `JobText` table keyed by
   `content_hash` (already computed, already indexed) with `job.description`
   becoming a nullable pointer. This captures most of the storage saving with
   none of the identity churn in §5, because `job.id` never moves.

Option 3 is the recommended next step if storage is the pressing problem, and
it is strictly less risky than the full canonical migration.

## 7. Staged plan, with the verification each stage depends on

**Stage 0 — measure (nothing changes).** Run, read-only:
`SELECT count(*), count(*) FILTER (WHERE user_id = <shared>) FROM job;`
`SELECT pg_total_relation_size('job'), pg_relation_size('job');`
`SELECT avg(pg_column_size(description)), percentile_cont(0.5) WITHIN GROUP (ORDER BY pg_column_size(description)) FROM job TABLESAMPLE SYSTEM (1);`
`SELECT count(DISTINCT (source, external_id)) FROM job;`
plus `pg_stat_user_indexes` for `job` and `EXPLAIN (ANALYZE, BUFFERS)` on the
board query and the scoring queue. Gate: the duplication figure and the
description size must match §1 and §4 within 20%, or this design is rewritten.

**Stage 1 — additive tables, no writers.** Create `CanonicalJob` and
`UserJobMatch`. No code reads them. Reversible by dropping two empty tables.

**Stage 2 — dual write.** New postings write both shapes. Old readers untouched.
Gate: a week with zero divergence between the two, checked by a comparison job.

**Stage 3 — backfill in batches.** Chunked by `job.id` range, bounded per
statement because Supabase enforces a statement timeout, resumable, with a
progress table. Gate: row counts reconcile.

**Stage 4 — cut readers over, one at a time,** behind a per-reader flag,
starting with the least critical (analytics) and ending with the board.

**Stage 5 — stop writing `job`,** then drop columns only after a full backup
cycle has aged out.

Rollback: stages 1–3 are additive and roll back by reverting code. Stage 4
rolls back by flipping its flag. Stage 5 is the first irreversible step and
should be separated from the rest by weeks.

## 8. Recommendation

Do not start the canonical migration on the strength of this document. Run
Stage 0, then choose between option 3 in §6 (description dedup, most of the
saving, little risk) and the full split. The hiring-context tables shipped now
are deliberately compatible with either outcome.
