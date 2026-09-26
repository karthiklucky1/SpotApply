# Audit 2026-09-25 — implementation checklist and evidence

Branch `claude/modest-sagan-kl3hkn`. `e6bc2a2` (P1 fixes) is already on `main`
and deployed (Railway deploy 36483291). Everything after it is in this branch
and is **NOT deployed**. Columns:

* **Impl** — code written · **Local** — covered by tests / lab run in this
  container · **Deployed** — running in production · **Prod-verified** —
  observed working in production after deploy.

Nothing is marked prod-verified unless it was observed in production. Anything
blocked by a missing credential, staging environment or user interaction is
listed as **unverified**, never passed.

## Checklist

| # | Item | Commit | Impl | Local | Deployed | Prod-verified |
|---|---|---|:-:|:-:|:-:|:-:|
| 0 | Production writes failing intermittently (pooled backend stuck read-only) — diagnosed, backend terminated, write-failure detector (`db_health`, `/api/admin/health.db_writes`) | ec58df6 | ✅ | ✅ | detector: no | writes resumed after the fix (117 jobs + 123 events written within minutes); detector pending deploy |
| 1a | Location re-decided at delivery; attendance text beats structured remote; relocation targets bind | e6bc2a2 | ✅ | ✅ | ✅ | partly — 2,000-decision review against live posting evidence (below) |
| 1b | Country-only site + unknown work mode held for a non-relocator (defect found BY the review) | 3ace51d | ✅ | ✅ | no | no |
| 1c | Hard constraints before paid ranking: gig/AI-training work, citizenship-only roles for confirmed non-citizens, explicitly required higher degree | 4f93d33 | ✅ | ✅ | no | no |
| 1d | One role delivered once: same company + normalised title within 40 days = duplicate; a first-party ATS copy replaces an unviewed aggregator copy; same-source distinct locations stay distinct | 4f93d33 | ✅ | ✅ | no | no |
| 2a | Skill duration only from dated evidence; one export verdict | e6bc2a2 | ✅ | ✅ | ✅ | no (needs an authenticated review) |
| 2b | Future-sponsorship question never pre-answered for a dated status (fill-pack, extension, answer pack) | aa3c91a | ✅ | ✅ | no | no |
| 2c | DOCX single column and section order | aa3c91a | ✅ | ✅ | — | DOCX→PDF rendered with mammoth+Chromium (approximate; LibreOffice Writer is not installed) |
| 3a | Compute lifecycle: setup/active/idle/dormant/paused/paid; paid AI only while active or paid; checked when queued AND before each provider call | 53a03f6 | ✅ | ✅ (clock boundaries, 12 threads vs cap 5, routes) | no | no |
| 3b | Atomic per-user and platform daily paid-call reservations (`platform_counter` conditional UPDATE) | 53a03f6 | ✅ | ✅ | no | no |
| 3c | Pause / resume controls and dashboard notice | 53a03f6 | ✅ | ✅ | no | no (needs a signed-in browser) |
| 4 | `/api/pipeline/live` cheaper + 10 s shared cache; nested-session lock waits removed | 0330cb6 | ✅ | ✅ lab | no | no |
| 5 | Identity repair: pairs closed (never deleted), runbook with backup + rollback | cb0e265 | ✅ | ✅ | — | dry run only; **apply NOT run** |
| 6a | Temporary Pro ON by default; nothing for sale | 33a80cd | ✅ | ✅ | no | no |
| 6b | Public pages on compiled CSS (no Tailwind Play CDN on pricing/privacy/terms/auth); sitemap/robots on `app.spotapply.ai`; research preview labelled on hold | 33a80cd | ✅ | ✅ | no | no |
| 7a | Journey milestones from action routes only, salted user key, internal accounts excluded, `/api/admin/journey` (incl. spend per activated user) | d23b455 | ✅ | ✅ | no | no — a week of data is needed |
| 7b | Contact-research observations persisted (`/api/admin/contact-research.persisted_30d`) | d23b455 | ✅ | ✅ | no | no |

## Verification run in this container

* Full suite, both orders (`pytest -p no:randomly` and reversed file order),
  `--disable-socket`, on the release head: **3053 passed / 7 skipped in each
  order** (48 s / 52 s; was 142 s before the nested-session lock fix — the
  three 30 s SQLite lock waits in `test_mark_ghost_jobs` are gone). The 7
  skips are the ML-stack and real-Postgres suites, as in CI.
* `ruff check app --select=E9,F63,F7,F82`, `scripts/validate_templates.py`,
  `node --check extension/*.js`.
* `scripts/audit_reproductions.py` (synthetic): on `20fb576` findings F2, F3a,
  F3b, F4, F5, F6, F7a, F7b, F8, F11, F13a, F13b reproduce; on `e6bc2a2` only
  F11 and F13b; on this branch none.

## Production evidence (read-only, aggregates only — no user data copied)

* **Write incident, 2026-09-26.** `ReadOnlySqlTransaction` from 02:26:33 to
  ~04:00 UTC on three pooled backends (pid 346684: 1,212 errors). The server
  default was `off`; the first error coincides with the audit's read-only
  snapshot, i.e. a session-level `SET` survived in the Supavisor transaction
  pool. Writes were intermittent, not stopped. The idle backend was
  terminated at ~04:00; writes resumed immediately.
* **Baseline (~04:02).** 19 profiles; 1 active in 24 h/7 d; 7 with no home
  location; 182 shortlisted (181 eligible stamps, 1 unknown), 176 distinct
  company+title (5 duplicate groups), 40 from aggregators.
  `/api/pipeline/live` 1.06–2.27 s with one open tab. HTTP 03:00–03:46:
  109×200, 91×404 (bot probes), 0×5xx.
* **Match review.** The 400 most recent `JobGeography` rows (public posting
  evidence) × 5 synthetic profiles = 2,000 decisions. 0 hard-constraint
  violations after fix 1b; relocation-only-Chicago profile: 9
  `relocation_target_excluded`; no-home profile: 10 `home_location_missing`
  held. Spot-checked "Amsterdam" remote rows eligible for US users — all
  also list "Remote - United States".
* **Identity dry run.** Workday 457,406 unscoped / 5,196 pairs; BambooHR
  5,228 / 13,251; Teamtailor 26,303 / 1,201. Applications on the old copy in
  7 pairs, on both copies in 2 (left for a human). See
  `docs/RUNBOOK_IDENTITY_REPAIR.md`.

## Lab load test (local SQLite — NOT staging, NOT Postgres)

20 clients × 30 requests, seeded 20k jobs / 3.2k applications:

| | before (33a80cd) | after (0330cb6) |
|---|---:|---:|
| `/api/pipeline/live` p50 / p95 | 9,994 / 14,891 ms | 32 / 50 ms |
| `/api/notifications` p95 | 4,750 ms | 58 ms |
| wall time | 92.7 s | 3.0 s |

Single uncached request ~25 → ~11 ms: most of the concurrent gain is the
shared 10 s cache. SQLite serialises writers, so these numbers do not predict
Supabase latency; `scripts/load_test_polling.py` refuses production hosts and
is meant for staging.

## Unverified / blocked

* Auth flows with test accounts (signup, verification, Google, refresh,
  logout, recovery, extension) and two-account isolation in a browser: no
  test credentials in this container. Isolation is covered only by the route
  inventory and ownership tests.
* Staging load test: no staging environment.
* True DOCX→PDF via LibreOffice Writer.
* `app.spotapply.ai` is not reachable from this container (egress policy), so
  no HTTP smoke of the live site from here.
* Retention and spend per activated user: needs at least 7 days after deploy.
  Nothing in this branch claims a week of observation.

## Deploy

See the PR. Schema: three nullable `userprofile` columns, added by the
existing startup migration (`init_db` adds only the columns it finds missing). Rollback =
redeploy `e6bc2a2`; the columns are harmless to the old code. Kill switches:
`COMPUTE_POLICY_ENFORCED=0`, `TEMPORARY_PRO_FOR_ALL=0`.

**Expected behaviour change on deploy:** every existing account starts
DORMANT (NULL `last_meaningful_activity_at` is not grandfathered) — no
automatic paid ranking for anyone until they take a meaningful action or
press Resume. Shared-pool discovery and free DB work continue.

## Next observation period (7 days after deploy)

1. `/api/admin/health` — `db_writes.read_only_errors` stays 0.
2. `/api/admin/journey` — signup → first_shortlist → document_downloaded
   conversion; day-1/3/7 returns (cohorts will be tiny; report counts, not
   rates).
3. `/api/admin/spend` — spend per activated user vs the 2026-09-16 week;
   dormant accounts should cost ~0.
4. Placement events — share of `duplicate` / `ineligible` / `held`
   outcomes; complaints about missing jobs.
5. `/api/pipeline/live` p95 in Railway HTTP metrics.
