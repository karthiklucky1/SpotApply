# Future Scaling Plan

> Scope: what has to change to take SpotApply from ~10 active users to ~10,000, staged so that
> nothing is rebuilt before the thing it replaces has actually broken. Every "this breaks" claim
> below cites the line that breaks.
>
> Terminology: **active user** = a user who passes the dormancy gate (`_user_is_active`,
> `app/api/server.py:308`) — i.e. authenticated within `DORMANT_USER_GRACE_DAYS`
> (`app/config.py:276`, default 21). Registered users who go quiet cost nothing; the gate is
> already doing real capacity work and should be treated as load-bearing, not as a tidy-up.

---

## 0. The scaling model

Three independent ceilings govern this system. Almost every failure below is one of them.

| # | Ceiling | Where it lives | Scales with |
|---|---|---|---|
| **C1** | Global LLM final-score budget: 1,500/day, 150/hour, **platform-wide across all users and all lanes** | `app/config.py:191-192`, enforced `app/matching/reranker.py:117-126` and raised as an exception at `:725-728` | Nothing. It is a fixed number divided by N users. |
| **C2** | One process holding web + torch/MiniLM + FAISS + 5 lanes + transient Chromium, with every lock and counter in module globals | `app/api/server.py:398-409` (the CRITICAL log on `WEB_CONCURRENCY>1`), `app/common/inflight.py:19-20`, `app/matching/reranker.py:98-100` | CPU, RAM, and per-tick O(users) work in one container |
| **C3** | Per-tenant physical row duplication: a posting is copied in full (description included) into every interested user's pool | `app/db/models.py:74-76` (`uq_job_user_source_external_id`), copies made at `app/strategy/adoption.py:195` | O(users × on-role postings) storage, egress, and write throughput |

**Unit economics** (the number that decides everything downstream). One fully-processed job costs
one Tier-1 prescore plus roughly a 30% chance of a Tier-2 final:

```
prescore (gpt-4o-mini, ~1.7k in / ~30 out)          ≈ $0.00018   reranker.py:329-349, :574-585
final    (claude-haiku-4-5, warm cache)             ≈ $0.0033    reranker.py:398-430, :521-533
blended at a 30% advance rate                       ≈ $0.0012 / job
```

A user with ~150 new on-role rows/day therefore costs **~$5.40/month in scoring** plus ~$1/month
in tailoring, against a `$10` plan price (`app/db/models.py:344-350`). That is a ~35% gross margin
at the low end of inflow and **negative** for a broad-role user at 400 rows/day. This single ratio
is why the distilled scorer (§3.4) is the highest-value item in this plan and why it appears
earlier in the roadmap than its "nice research project" reputation suggests.

---

## 1. Stage 1 — 10 → 100 active users

### 1.1 What breaks first, in order

**(a) C1 — the LLM budget divides by N.** The scoring lane's work list is round-robin across users
(`app/strategy/scoring_lane.py:446-459`), so each user gets ~`1500/N` authoritative scores per day.
At N=10 that is 150/user/day; at N=100 it is **15/user/day** while their pool grows by 150–400 rows
(`ADOPT_MAX_JOBS = 400` per pass, `app/strategy/adoption.py:31`, up to 16 scheduled passes/day).
The unscored backlog then grows monotonically, and the "fresh job scored within minutes" promise
silently becomes hours. Nothing logs this; the only signal is `skipped: "LLM budget reached"`,
which the driver does not even print (`app/api/server.py:813-814` only logs when work happened).

**(b) `_lane_user_ids()` is O(users) blocking HTTPS per lane tick.** `_user_has_resume` does a
Supabase Storage `list(uid)` per user (`app/api/server.py:330-346`), and the same shape runs in
`_active_users` on **every 60-second pulse tick** (`app/strategy/hot_lane.py:37-61`, called from
`app/strategy/pulse_lane.py:366`). At 100 users that is 100 sequential round-trips per minute
consumed out of a 150-second tick budget (`app/config.py:349`) before a single board is fetched.
The docstring already flags this as a production-review finding.

**(c) The matching lane is already over its deadline at 10 users.** `_run_matching_lane` iterates
users serially under the discovery lock with a 240-second budget (`app/api/server.py:886-912`), and
one `run_matching` pass is ~130s dominated by the local cross-encoder at ~1s/pair × 120 pairs
(`app/matching/rerank_backend.py:3-5`, `app/config.py:165`). Roughly **two users fit per tick**, and
the loop always restarts from the head of the list — users deep in the list are permanently
deferred to the lock-free scoring lane and never get retrieval, re-shortlisting, or the self-heal
resets.

**(d) Browser concurrency is 1, process-wide.** `browser_slot` refuses a second Chromium and raises
`BrowserBusy` after 120s (`app/common/browser.py:76-93`). Two users clicking Preview at the same
moment means one gets an error; a headful preview holds the slot for up to 3,600s
(`app/autofill/agent.py:2362`) and then waits on window close indefinitely (`:2540-2541`).

**(e) Adoption CPU.** Each pass embeds up to 1,500 job texts on the shared CPU
(`app/config.py:316`, `app/strategy/adoption.py:55-79`), ×16 passes/day × N users, competing with
board fetches and FAISS rebuilds in the same container.

**(f) `funnel_events` has no retention and no `user_id`.** One row per discovered job
(`app/analytics/funnel.py:27-29`), one per pulse tick (`app/strategy/pulse_lane.py:472-482`), one
per scoring cycle. `get_summary` reads them with `len(session.exec(...).all())` and then
`session.get(Job, ...)` **per row** (`app/analytics/funnel.py:35-81`) — an N+1 over the entire
discovery history.

### 1.2 What to change

Almost all of Stage 1 is config plus small, contained code changes. **No re-architecture.**

| Change | Type | Notes |
|---|---|---|
| Raise `LLM_DAILY_FINAL_CAP` / `LLM_HOURLY_FINAL_CAP` in step with paying users | **config** | `app/config.py:191-192`. Counters stay process-local and correct while there is one process. |
| `RERANK_PROVIDER=jina` | **config** | `app/config.py:171-173`. Collapses the 120-pair cross-encoder from ~2 min to one ~300–800 ms call (`app/matching/rerank_backend.py:38-76`) *and* keeps the ~200 MB model unloaded (`app/matching/matcher.py:169-180`). Single biggest latency + RAM win available for one env var. |
| `BROWSER_SERVICE_URL=…` — **split browser work into its own service** | **config** (the code already exists) | `app/config.py:406-408`, `app/common/browser_client.py`. Moves the three *stateless* render paths (JD scrape, Google discovery, search-engine source) to a separate container; ~400 MB of Chromium leaves the main cgroup. Autofill and headful preview deliberately stay local — they are stateful interactive sessions — so `BROWSER_MAX_CONCURRENCY` still gates them. |
| Cache "has résumé" as a `UserProfile` column, set on upload/delete | **small code** | Kills breakage (b) outright. Turns O(users) HTTPS per tick into one indexed query. |
| Give the matching lane a rotating cursor (round-robin like the scoring lane) | **small code** | `app/api/server.py:886-912`. Fixes head-of-list starvation without changing the lane's role. |
| `funnel_events` retention job + replace `len(.all())` with `COUNT` and drop the N+1 | **small code** | `app/analytics/funnel.py:35-81`. Mirror the existing `purge_old_closed_jobs` batching pattern (`app/strategy/job_retention.py:27-57`). |
| Index for the dashboard sort (`blended_score`, `rerank_score`) and for `job.company` | **migration** | Dashboard sorts the whole per-user open pool per page view (`app/api/server.py:2809-2818`), and the company cap joins on an unindexed `job.company` per shortlist candidate (`app/matching/pipeline.py:117-124`, column at `app/db/models.py:82`). |
| Batch `prune_stale_shortlist` | **small code** | `app/strategy/shortlist_hygiene.py:42-63` builds an unbounded `IN (…)`; after any lane outage the first tick emits one enormous statement against Supabase's `statement_timeout`. |

### 1.3 Rough cost at 100 users

| Line item | ~$/month |
|---|---|
| Scoring LLM (100 × ~$5.40) | 540 |
| Tailoring LLM (bursty, `TAILOR_ABUSE_DAILY_CAP=150/user/day` is the *only* bound — `app/config.py:275`) | 100–400 |
| App container + browser-service container | 40–100 |
| Supabase (compute + storage + egress) | 100–200 |
| Jina rerank | 20–50 |
| **Total** | **~$800–1,300/mo** (~$8–13/user) |

Note the tailoring line: it is Sonnet 4.6 spend that never calls `_register_final_call`, so it is
**invisible to C1** (`app/tailoring/tailor.py:215-216` vs `app/matching/reranker.py:751`). Three
users at the abuse ceiling out-spend the entire scoring budget. Bringing tailoring under a shared
budget counter is a Stage-1 sized fix with Stage-3 sized consequences if skipped.

---

## 2. Stage 2 — 100 → 1,000 active users

This is where the single-process design ends. Stage 2 is the only stage that contains a genuine
re-architecture, and it is all one workstream: **make the process-local state shared, then split
the lanes out of the web process.**

### 2.1 What breaks first

**(a) Pulse-lane per-user fan-out is O(changed_boards × users).** For each changed board the tick
loops every active user and runs a full `_upsert` (`app/strategy/pulse_lane.py:433-443` — verified:
`for u in users: … _upsert(relevant, user_id=…)`), each with its own chunked dedupe prefetch and a
session+commit per inserted row (`app/discovery/pipeline.py:412-419`). At 30 changed boards × 1,000
users that is **30,000 `_upsert` invocations inside a 150-second tick**. This breaks somewhere
between 200 and 400 users and is not tunable away.

**(b) The watchlist scan is O(boards × users) per tick.** `_watchlist_terms()` builds the union of
every `UserProfile.target_companies` on every tick (`app/strategy/pulse_lane.py:65-79`, verified),
and `_is_watched` substring-matches every term against every due board (`:82-91`). At 1,000 users ×
25 companies = 25,000 terms × 300 boards = 7.5M string comparisons per tick, plus a full-table read
of `target_companies` every 60 seconds.

**(c) The job-retention purge falls permanently behind.** `purge_old_closed_jobs` deletes at most
`batch(2000) × max_batches(100) = 200,000` rows per invocation (`app/strategy/job_retention.py:27`)
and runs **once per 24h** (`app/api/server.py:624-628`). At ~150–200 new per-user rows/day, 1,000
users produce 150k–200k rows/day — the purge ceiling is reached at roughly **1,300 users**, after
which the job table only grows.

**(d) Storage from C3.** At ~4 KB of description per row, 1,000 users × 150 rows/day ≈ **600 MB/day**
of duplicated job text, ~36 GB at 60-day retention. Every per-user scan pays that egress.

**(e) DB connections.** 30 total (`app/config.py:201-202`, `app/db/init_db.py:25-32`) against 20
scoring + 16 prescore + 12 rerank + 24 pulse-fetch workers plus web. This works today *only*
because no worker holds a session across an LLM call (`app/strategy/scoring_lane.py:224-229`). The
moment there is more than one lane process, 30 becomes 30×processes against Supabase's pooler.

**(f) The deferral cliff.** Attempt-deferred job ids are excluded in-SQL only while the set is
≤2000 (`app/strategy/scoring_lane.py:142-160`); past that it degrades to post-`LIMIT` filtering and
the starvation it was written to fix returns.

### 2.2 Splitting the lanes out of the web process — the shared-state inventory

Today, splitting is *one env var* on the extra replicas (`LANES_ENABLED=0`, `app/config.py:274`) —
but that only gives you more **web** capacity. Running a second *lane* process requires every item
below to move out of module globals first. **This table is the re-architecture.** Everything not in
it is a config change.

| Process-local state | Location | Consequence if split without fixing | Replacement |
|---|---|---|---|
| `_daily_finals` / `_hourly_finals` | `app/matching/reranker.py:98-100` (verified) | Caps become `N × cap`; spend multiplies silently | Redis `INCR` + `EXPIRE` on keys `finals:{UTC-day}` / `finals:{UTC-hour}`. Must be atomic **and** checked before the API call, preserving the raise-not-return-0 behaviour at `:725-728`. |
| `_provider_down_until` (circuit breaker) | `app/matching/reranker.py:29-56` | Each process re-discovers a dead provider and burns its own retry budget | Redis key with TTL = `llm_provider_cooldown_minutes` |
| In-flight job claim set | `app/common/inflight.py:19-20` (verified — the module docstring explicitly says "everything runs in ONE uvicorn process … no Redis") | Two processes pay for the same job; the write-back is idempotent so there is no data harm, only doubled spend | **Preferred:** replace polling + claim with `SELECT … WHERE rerank_score IS NULL … FOR UPDATE SKIP LOCKED` over the existing `ix_job_unscored` partial index (`app/db/init_db.py:335-339`). This makes the queue *be* the claim and deletes the abstraction. Fallback: Redis `SET NX PX`. |
| `_LANE_LOCK`, pulse `_TICK_LOCK`, `discovery_guard` | `app/strategy/scoring_lane.py:51`; `app/strategy/pulse_lane.py:320-343`; `app/common/discovery_lock.py:21` | Concurrent ticks double-fetch every due board (already possible today via the 90s lock steal at `pulse_lane.py:328-343`) | Postgres advisory locks (`pg_try_advisory_lock`) with a lease/heartbeat, or Redis locks with TTL. Keep the existing steal-on-overrun semantics — they exist for a reason. |
| `_fail_counts` / `_deferred_until` | `app/strategy/scoring_lane.py:59-61` | Poison jobs retried 3× *per process*; restarts reset all deferrals | **Columns on `Job`** (`score_attempts`, `score_deferred_until`). Also fixes the 2000-id cliff (§2.1f) because the exclusion becomes an indexed predicate. |
| `_prescore_memo` | `app/strategy/scoring_lane.py:214` | Failed finals re-pay Tier-1 | Redis with TTL, or a nullable `prescore` column on `Job` |
| slowapi limiter | `app/api/server.py:82` | Per-IP limits become `N × limit` across web replicas | slowapi Redis storage backend (config once Redis exists) |
| `_JWT_CACHE` | `app/db/supabase_client.py:32-44` | Correct but wasteful: each process pays its own Auth round-trip per token/minute | Better fix is to stop calling Auth at all — verify the JWT locally against the project JWKS. Removes an HTTPS round-trip from the request path entirely. |
| `_LAST_ACTIVE_STAMP` | `app/api/server.py:268` | Harmless (more `UPDATE`s) | Leave process-local |

**Recommended split topology at Stage 2:**

```
web (N replicas, LANES_ENABLED=0)  — FastAPI only, no torch, no lanes
scorer (M replicas)                — scoring lane only; lock-free by design already
                                     (scoring_lane.py:19-23: no FAISS, no embedding model)
ingest (1–2 replicas)              — pulse + fresh + full discovery
matcher (1 replica)                — FAISS/retrieval backstop; the only process that loads MiniLM
browser (1–2 replicas)             — already supported via BROWSER_SERVICE_URL
```

The scoring lane is the easiest to extract because it was *already* written lock-free with respect
to `discovery_guard` and with strict read → detach → LLM → idempotent write-back discipline
(`app/strategy/scoring_lane.py:243-263`, `:185-207`). The matcher is the hardest — see §2.3.

Add **PgBouncer in transaction mode** in front of Supabase before, not after, the split; `DB_POOL_SIZE`
becomes per-process and 30×5 processes exceeds the plan's connection limit.

### 2.3 Replacing per-user FAISS files with a real vector store

Per-user index files (`app/matching/matcher.py:186-198`) are the **hard blocker** on splitting the
matcher, and they have an independent defect:

- **Ephemeral disk.** The files live on the container's disk, so a redeploy triggers a from-scratch
  rebuild of up to 4,000 vectors *per user* (`REBUILD_MAX_JOBS`, `app/matching/matcher.py:41`),
  serialized behind `discovery_guard`. At 1,000 users that is 4M MiniLM encodes before matching
  returns to steady state.
- **No shared storage.** Two matcher processes cannot see each other's indexes; the design assumes
  one writer per file.
- **Size is not bounded.** ⚠️ The `≤ 6.1 MB/user` figure (4,000 × 384 × 4 B) applies only to a
  *from-scratch* build (`app/matching/matcher.py:265`, `:302`). The incremental branch appends with
  `existing_index.add(new_embs)` and `np.concatenate` and never evicts
  (**verified, `app/matching/matcher.py:328-340`**), so an index only shrinks when `force_rebuild`
  fires (`:277`). Per-user index size grows without bound over a container's lifetime.
- Orphaned index files are never cleaned up on account deletion (`app/api/server.py:7943-7962`).

**Change:** move to **pgvector in the existing Postgres** — one `job_embedding(job_id, embedding vector(384))`
table with an HNSW index, and per-user retrieval as a `WHERE user_id = ? AND rerank_score IS NULL`
filter over it. Reasons to prefer pgvector over a hosted vector DB here: the corpus is small
(384-dim, tens of millions of vectors at 10k users), the filter is a plain tenant predicate, and it
removes a whole storage system rather than adding one. A dedicated store (Qdrant/Turbopuffer) only
earns its keep if retrieval QPS becomes the bottleneck, which it will not — the cross-encoder does
(`app/matching/rerank_backend.py:3-5`).

Also note the retrieval math shifts favourably: `search_for_resume` currently does a **full** scan
(`index.search(chunk_embs, self.index.ntotal)`, `app/matching/matcher.py:455-456`) precisely because
the index holds all open jobs while the corpus is a small unscored subset. A filtered pgvector query
expresses that intent directly instead of working around it.

### 2.4 DB read replicas and partitioning

**Read replicas** are worth adding at Stage 2 — but *after* the indexes from §1.2. The dashboard's
`ORDER BY nullslast(blended_score DESC)` over the whole per-user open pool
(`app/api/server.py:2809-2818`) is an index problem, not a replica problem; routing an unindexed
sort to a replica just moves the burn. Once indexed, route read-only traffic (dashboard, job list,
funnel/analytics) to a replica and keep all lane writes on the primary. Analytics reads in
particular (`app/analytics/funnel.py:35-81`, `app/analytics/spend.py:59-90`) belong on a replica —
they scan unbounded tables and have no latency requirement.

**Partitioning:** partition `funnel_events` by month (it is append-only, has no `user_id`, and is
scanned by `(stage, created_at)` — `app/db/init_db.py:334`), so retention becomes `DROP PARTITION`
rather than a delete storm. Partitioning `job` is *not* the right fix — see Stage 3.

### 2.5 Rough cost at 1,000 users

| Line item | ~$/month |
|---|---|
| Scoring LLM (1,000 × ~$5.40) | 5,400 |
| Tailoring LLM | 1,000–3,000 |
| Compute (web ×3, scorer ×2, ingest, matcher, browser ×2) | 300–700 |
| Redis | 30–80 |
| Postgres (primary + 1 replica, ~40 GB) | 500–1,200 |
| **Total** | **~$7,500–10,500/mo** (~$8–11/user, i.e. **at or past the plan price**) |

At this stage the LLM line is ~65% of COGS and the margin is roughly zero. Stage 3 is not optional
if the plan price stays at $10.

---

## 3. Stage 3 — 1,000 → 10,000 active users

### 3.1 What breaks first

**(a) C3 becomes the dominant cost.** 10,000 users × ~150 rows/day ≈ 1.5M new `Job` rows/day, each a
full copy of a description (`app/db/models.py:74-76`). ~360 GB of duplicated text at 60-day
retention, 3M commits/day just for inserts (two per row —
`app/discovery/pipeline.py:412-419` plus `app/analytics/funnel.py:27-29`), and a purge that gave up
at ~1,300 users (§2.1c).

**(b) LLM spend at ~$65k/month** with Tier-2 Claude on every promising job. C1 raised to match is
simply a decision to spend that money.

**(c) Board polling saturates.** `pulse_max_boards_per_tick = 300` × 60 ticks/hr = **18,000
polls/hour** (`app/config.py:350`, `app/strategy/pulse_lane.py:353`) against a ~56K-board registry
(`app/config.py:64`). The "every live board within 60 minutes" promise
(`pulse_floor_interval_minutes`, `app/config.py:345`; cadence logic verified at
`app/strategy/pulse_lane.py:94-104`) holds only while live boards number roughly ≤12,000 — and the
watchlist tier grows with users (§2.1b), consuming 12 polls/hr per watched board. `_due_boards`
silently truncates via `LIMIT` (`app/strategy/pulse_lane.py:107-120`); nothing alarms.

**(d) No global scraper rate limiting.** 12–24 concurrent fetches per lane × multiple lane
processes, with no per-host token bucket (`app/discovery/pipeline.py:803`,
`app/strategy/pulse_lane.py:380`). A registry heavy in one ATS gets the whole deployment blocked —
RemoteOK already re-raises on Cloudflare blocks (`app/discovery/sources/remoteok.py:62-67`).

### 3.2 Re-architect the job corpus (the big one)

Replace "one full `Job` row per tenant" with:

```
job              — one canonical row per (source, external_id): description, company, title, …
user_job         — thin per-tenant row: (user_id, job_id, rerank_score, blended_score,
                   reasoning, ghost_score, first_seen_for_user, …)
```

This is the single largest migration in the plan and it fixes, simultaneously:

- storage and egress (one description, not N);
- the pulse-lane fan-out (§2.1a) — routing becomes one set-based `INSERT … SELECT` into `user_job`
  per changed board instead of a Python loop of `_upsert` calls per user;
- the purge ceiling (§2.1c) — deleting a canonical job removes N tenant rows;
- vector storage — one embedding per posting instead of N (`app/matching/matcher.py:211-220` embeds
  the same text once per tenant today);
- adoption (`app/strategy/adoption.py:107-199`) becomes a cheap `INSERT … SELECT` with no RawJob
  round-trip through `_upsert`.

It also touches essentially every query in the codebase, which is exactly why it belongs at Stage 3
and not earlier. Do it *after* the lane split, so the migration is exercised by one subsystem at a
time.

Partition `job` by `first_seen` month once it is canonical; partition `user_job` by `user_id` hash.

### 3.3 Shard discovery

Shard boards by `hash(slug) % N` across N ingest processes, each claiming its shard with an advisory
lock. Coverage becomes linear in processes rather than capped at 18,000 polls/hour. Add a per-host
token bucket in Redis in front of every scraper (fixes §3.1d). Hoist `_watchlist_terms` into a
cached, incrementally-maintained set (invalidated on `PUT /api/target-companies`,
`app/api/server.py:7178-7220`) rather than rebuilding it every 60 seconds.

### 3.4 The distilled scorer replaces Tier-2 Claude

This is the **highest-leverage item in the entire plan** and the only thing that makes 10,000 users
economically coherent.

The machinery already exists and is wired end-to-end — export → train → shadow → report
(`docs/DISTILLATION.md`, `scripts/export_training_data.py`, `scripts/train_local_scorer.py`,
`scripts/shadow_report.py`, `app/matching/local_scorer.py`). It is **dormant**: nothing exists at
`LOCAL_SCORER_PATH` (`app/config.py:209`), so `_model_source()` returns `None`
(`app/matching/local_scorer.py:40-46`), `available()` is `False` (`:81-86`), and `shadow_score()`
returns at its first guard (`:150-152`). Shadow mode is *enabled by default* and recording nothing,
so the ≥90% shortlist-decision-agreement criterion (`docs/DISTILLATION.md:36-39`) can never be met.
The pipeline is stalled at step 2 of 5, and step 5 — local model becomes Tier-2, LLM writes reasoning
only for shortlisted top-N plus a daily audit sample — is explicitly "deliberately not built yet"
(`docs/DISTILLATION.md:41-44`).

Target end state:

- Local cross-encoder scores **every** job at ~50 ms CPU (`docs/DISTILLATION.md`). 10,000 users ×
  150 jobs/day = 1.5M jobs/day = ~75,000 CPU-seconds/day ≈ **0.9 cores sustained**; call it 4–8
  cores with headroom.
- Claude runs only for (i) reasoning text on jobs that actually shortlist, and (ii) a random audit
  sample (~50/day) that feeds continuous retraining and drift detection.
- LLM spend drops from ~$65k/mo to low single-digit thousands. `LLM_DAILY_FINAL_CAP` stops being the
  capacity ceiling and becomes what it was always meant to be — a safety net.

Two invariants to protect during this work: `build_pair` must stay byte-identical between
`app/matching/local_scorer.py:55-59` and `scripts/train_local_scorer.py:21-30`, and the training
exporter must keep excluding cheap-gate rows (`scripts/export_training_data.py:34-53`) or the model
learns the ghost/rule filters instead of the rubric.

**Start collecting the data at Stage 1.** Training on 10-user data and deploying at 1,000 is fine;
having *no* data at 1,000 is not.

### 3.5 Rough cost at 10,000 users

| Line item | ~$/mo (no distillation) | ~$/mo (distilled) |
|---|---|---|
| Scoring LLM | 54,000 | 3,000–6,000 |
| Local inference compute | — | 400–1,000 |
| Tailoring LLM | 10,000–30,000 | 10,000–30,000 (unchanged — user-triggered) |
| Compute (web/scorer/ingest/matcher/browser fleets) | 3,000–6,000 | 3,000–6,000 |
| Postgres (partitioned primary + replicas, ~400 GB → ~40 GB post-corpus-fix) | 4,000–10,000 | 1,500–4,000 |
| Redis, object storage, egress | 500–1,500 | 500–1,500 |
| **Total** | **~$70–100k/mo** | **~$18–48k/mo** |

Tailoring becomes the largest LLM line once scoring is distilled. Bringing it under a shared budget
counter and caching per-(user, JD-family) drafts is the Stage-3 follow-on.

---

## 4. Multi-region — Stage 4, conditional

**Multi-region is premature until 10,000 users *or* a hard data-residency requirement, whichever
comes first.** It buys almost no throughput: the bottlenecks are LLM budget, CPU, and Postgres
writes, none of which regionalize cleanly.

The three things that *do* justify it, in order of likelihood:

1. **EU data residency.** Résumés live in Supabase Storage per tenant
   (`app/matching/pipeline.py:189-200`) and profile/experience data in Postgres. Before any EU
   region exists, fix account deletion — it currently leaves `FunnelEvent`, `UserNotification`,
   `LlmSpend`, `UserPersonalMemory`, `TrustHistory`, `UserReview`, `TrialGrant`,
   `CouponRedemption`, `UserReferralReward` rows and the user's FAISS/vector data behind
   (`app/api/server.py:7943-7962`). Shipping a GDPR-motivated region on top of an incomplete erasure
   path is worse than not shipping it.
2. **Scraper IP diversity.** Once §3.3's shard count grows, regional egress spreads load across
   source ASNs — but a per-host token bucket is the correct first answer, not geography.
3. **Dashboard latency.** Solved far more cheaply with a read replica plus edge caching of static
   assets.

Recommended shape if forced: **single write primary, regional read replicas, region-pinned object
storage for résumés and tailored documents.** Do *not* attempt multi-master; the company cap,
daily shortlist limit, and LLM budget are all global invariants
(`app/matching/pipeline.py:92-155`, `app/config.py:269`, `app/config.py:191`) and cross-region
consensus on them is not worth the complexity at any user count this plan covers.

---

## 5. Config change vs re-architecture

| Just a config change | Requires code | Requires re-architecture |
|---|---|---|
| `LLM_DAILY_FINAL_CAP` / `LLM_HOURLY_FINAL_CAP` (`app/config.py:191-192`) | `has_resume` column to kill the O(users) Storage probe (`app/api/server.py:330-346`) | Every row of the shared-state table in §2.2 |
| `RERANK_PROVIDER=jina` (`app/config.py:171-173`) | Matching-lane rotation cursor (`app/api/server.py:886-912`) | Per-user FAISS files → pgvector (`app/matching/matcher.py:186-198`, `:328-340`) |
| `BROWSER_SERVICE_URL` (`app/config.py:406-408`) — the browser split is *already built* | `funnel_events` retention + `COUNT` instead of N+1 (`app/analytics/funnel.py:35-81`) | Canonical `job` + thin `user_job` (`app/db/models.py:74-76`) |
| `LANES_ENABLED=0` on web replicas (`app/config.py:274`) — but only safe for **web**, never for a second lane process | Batch `prune_stale_shortlist` (`app/strategy/shortlist_hygiene.py:42-63`) | Set-based per-user routing replacing the pulse fan-out loop (`app/strategy/pulse_lane.py:433-443`) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`, lane cadences, `SCORING_*` caps, `PULSE_*` caps, `ADOPT*` caps | Job-level `score_attempts` / `deferred_until` columns (`app/strategy/scoring_lane.py:59-61`, `:142-160`) | `SELECT … FOR UPDATE SKIP LOCKED` queue replacing `inflight` claims (`app/common/inflight.py:19-20`) |
| Indexes (migration, but mechanical) | Cached watchlist set (`app/strategy/pulse_lane.py:65-79`) | Distilled scorer as Tier-2 (`docs/DISTILLATION.md:41-44`) |

The critical thing to internalize: **`LANES_ENABLED=0` makes web horizontally scalable today, and
nothing else in the system is.** A second *lane* process is not a config change — it is §2.2.

---

## 6. Ordered roadmap

Effort is one engineer: **S** < 1 week · **M** 1–3 weeks · **L** 1–2 months · **XL** 3 months+.

| # | Item | Effort | Do it at | Why now |
|---|---|---|---|---|
| 1 | Ship a `LOCAL_SCORER_PATH` model so shadow mode starts recording | S | **now** | `app/matching/local_scorer.py:40-46` — zero data is being collected; every month of delay pushes §3.4 out by a month |
| 2 | `RERANK_PROVIDER=jina` + `BROWSER_SERVICE_URL` | S | now | Two env vars; ~200 MB + ~400 MB out of the cgroup, cross-encoder 2 min → <1 s |
| 3 | `has_resume` column | S | now | Removes O(users) blocking HTTPS from every 60 s tick (`app/api/server.py:330-346`) |
| 4 | Dashboard + `job.company` indexes; batch the shortlist prune | S | now | `app/api/server.py:2809-2818`, `app/matching/pipeline.py:117-124` |
| 5 | Bring tailoring under a shared budget counter; fix spend attribution | S | now | `app/tailoring/tailor.py:215-216` bypasses C1; ledger books gpt-4o finals as `$0.001` prescores (`app/strategy/scoring_lane.py:527-528`) and records **nothing** for default Claude finals (`:179-180`) |
| 6 | Budget pre-check in the pulse fast path; cap OpenAI prescores | S | now | `app/strategy/pulse_lane.py:256-274` keeps paying Tier-1 after the cap trips; `app/matching/reranker.py:574-585` never registers a call |
| 7 | Fix FAISS incremental growth (truncate/evict or force periodic rebuild) | S | now | Verified unbounded at `app/matching/matcher.py:328-340` |
| 8 | Fix per-tenant senior-review / profile-variant leak | M | **before 100** | `app/intelligence/senior_reviewer.py:130-139` loads the founder's profiles for every tenant, and `app/matching/pipeline.py:238` → `app/tailoring/tailor.py:450-457` then tailors from them. Correctness/PII, not scale — but it blocks growth. |
| 9 | `funnel_events` retention + partition; fix the analytics N+1 | M | 100–300 | `app/analytics/funnel.py:35-81` |
| 10 | Matching-lane rotation cursor | S | 100–300 | `app/api/server.py:886-912` |
| 11 | **Redis + shared-state migration** (§2.2 table) | **L** | 200–400 | Prerequisite for every later item |
| 12 | `SKIP LOCKED` queue replacing `inflight`; attempt/defer state onto `Job` | M | with #11 | `app/common/inflight.py:19-20`, `app/strategy/scoring_lane.py:142-160` |
| 13 | Advisory-lock lane locks; extract the **scoring lane** into its own process | M | with #11 | Easiest lane to extract — already lock-free and session-disciplined |
| 14 | PgBouncer + read replica; route analytics/dashboard reads | S–M | with #13 | Do *after* #4, or you just move the burn |
| 15 | Per-user FAISS → pgvector | **L** | 400–800 | Blocks a multi-process matcher; also fixes redeploy re-encode storms |
| 16 | Extract ingest (pulse/fresh/full) into its own process; per-host token bucket | M | 400–800 | `app/discovery/pipeline.py:803` has no global rate limiting |
| 17 | Set-based per-user routing replacing the pulse fan-out | M | ~500 | `app/strategy/pulse_lane.py:433-443` is O(boards × users) |
| 18 | Cached/incremental watchlist set | S | ~500 | `app/strategy/pulse_lane.py:65-79` |
| 19 | **Distilled scorer to primary Tier-2**, Claude to reasoning + audit | **L** | 800–2,000 | The economics fix; needs 6+ months of shadow data from #1 |
| 20 | **Canonical `job` + thin `user_job`**; partition both | **XL** | 2,000–5,000 | `app/db/models.py:74-76`; unblocks storage, purge, routing, embeddings at once |
| 21 | Shard discovery by board hash | M | 3,000+ | `app/config.py:350` ceiling of 18k polls/hr |
| 22 | Complete the GDPR erasure path | M | before any EU region | `app/api/server.py:7943-7962` |
| 23 | Multi-region read replicas + region-pinned résumé storage | L | 10,000+ or compliance | See §4 |

### Explicitly premature — do not start these yet

- **Multi-region anything** (#23) before 10,000 users or a signed compliance requirement. It buys no
  throughput against C1/C2/C3 and multiplies operational surface.
- **A dedicated vector database** (Qdrant/Pinecone/Turbopuffer) instead of pgvector. Retrieval QPS is
  nowhere near the bottleneck — the cross-encoder is (`app/matching/rerank_backend.py:3-5`).
- **Kafka / a dedicated message broker.** Postgres `SKIP LOCKED` over the existing partial index
  (`app/db/init_db.py:335-339`) handles 10,000 users' scoring queue comfortably. The DB is already
  the queue; formalize that rather than replacing it.
- **A microservice per lane.** The five-process split in §2.2 is the right granularity; splitting the
  fresh lane from the full lane buys nothing and multiplies the shared-state surface.
- **Sharding discovery** (#21) before ~3,000 users. The pulse lane has real headroom until the live-board
  count passes ~12,000 (§3.1c).
- **Partitioning the `job` table** before #20. Partitioning a table that is about to lose 90% of its
  rows to deduplication is wasted migration work.
- **Raising `BROWSER_MAX_CONCURRENCY`** (`app/config.py:396`) to fix preview contention. Each +1 is
  ~400 MB in the *main* cgroup; move interactive autofill/preview to the browser service instead —
  the stateless paths already went there in #2.

---

## 7. Correctness debt that gates growth

None of these are scaling items, but each one becomes materially worse with users and should be
cleared no later than the stage noted.

| Issue | Where | Gate |
|---|---|---|
| Senior review + tailoring use the founder's résumé for every tenant | `app/intelligence/senior_reviewer.py:130-139`, `app/tailoring/tailor.py:450-457` | before 100 |
| Telegram approval/alerts route to one global `telegram_chat_id` | `app/autofill/agent.py:102-120`, `app/strategy/fresh_alerts.py:73-81` | before server-side autofill opens beyond the founder (`app/config.py:159`) |
| Founder-shaped answer defaults (`state='Ohio'`, referral='LinkedIn', EEO values) checked before per-user identity | `app/autofill/agent.py:542-564`, `app/qa_store/answers.yaml:1-62` | before `AUTOFILL_MULTI_USER_ENABLED=True` |
| `today_count` not user-scoped when `user_id` is falsy | `app/matching/pipeline.py:530-533` | before 100 |
| Account deletion leaves 9+ tables of tenant data | `app/api/server.py:7943-7962` | before 1,000 / any EU region |
| `CREATE INDEX CONCURRENTLY` can leave a permanently INVALID index that the existence check then skips forever | `app/db/init_db.py:353-372` | before relying on `ix_job_unscored` at scale |
| Scoring-lane deadline abandons *results*, not *work* — jobs are paid for, stamped, but never shortlisted that cycle | `app/strategy/scoring_lane.py:506-535` | before the matching-lane backstop is weakened |