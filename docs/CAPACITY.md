# SpotApply — Capacity & Scale

*Present-day analysis at committed defaults. Every number is either read from the code (cited with `file:line`) or derived from cited numbers with the arithmetic shown. Where the code does not determine a value, it says so.*

> ### ⚠️ Partially superseded — read this first
>
> This document analysed the defaults as they stood **before** the per-plan cap
> change. Four of its inputs have since moved, so the §2–§3 arithmetic is a
> record of the old regime, not current behaviour:
>
> | | Then | Now |
> |---|---|---|
> | Tier-2 allocation | one global `LLM_DAILY_FINAL_CAP = 1500` | **per user**, `PLAN_LIMITS["finals_daily"]` (Free 15 / Pro 50 / Agency 100) |
> | `LLM_DAILY_FINAL_CAP` | the binding constraint | a runaway **backstop** (5000) |
> | `LLM_HOURLY_FINAL_CAP` | 150 | 400 |
> | `SHORTLIST_SCORE_THRESHOLD` / `PRESCORE_ADVANCE_THRESHOLD` | 35 / 35 | **60 / 60** |
>
> Also fixed since: the two uncapped Tier-1 leaks in §2.2 (both lanes now check
> `llm_budget_exhausted()` before prescoring), and the retrieval `select(Job)`
> that was streaming full job descriptions — the direct cause of a 205% Supabase
> egress overage on 2 MB of stored data.
>
> **Production evidence that drove the threshold change** (57,309 real Claude
> finals, identified by `rerank_breakdown IS NOT NULL` — the only marker that
> distinguishes a Tier-2 final from a free-filter or Tier-1 stamp): **44.5%
> scored ≥35, 11.6% scored ≥65.** Of 335,867 stamped rows only 17% ever reached
> Claude; 60% were drained by the Tier-1 gate and 20% by the ghost filter, so
> the cheap cascade is doing its job. The §2.4 "25% shortlist rate" assumption
> was low by ~1.8×.

---

## 0. Executive summary

| Question | Answer |
|---|---|
| **What binds first?** | `LLM_DAILY_FINAL_CAP = 1500` Tier-2 scores/day, global across all users and all lanes (`app/config.py:191`) |
| **Users servable today** | **~10–15 comfortably; degrades 15–30; structurally broken past ~30** at default caps and typical inflow |
| **Platform LLM spend at caps** | **~$5.6–8.0/day** for scoring (fixed, user-count independent) |
| **Cost per authoritative score** | **$0.0033 warm / $0.0087 cold** (Haiku 4.5, code-derived tokens) |
| **Largest uncapped exposure** | **Tailoring** — Sonnet 4.6, 150/user/day ceiling, outside the scoring budget. One user at the cap can outspend the entire platform scoring budget 1–5×. |
| **Discovery vs scoring imbalance** | Discovery can fetch ~25k–45k postings/day and poll 130k–438k boards/day; scoring can authoritatively rate ~1,500/day. **~20–300× over-provisioned.** |

---

## 1. Every hard cap, limit and interval

### 1.1 LLM spend & scoring cascade

| Setting | Default | Unit | Gates | Where |
|---|---|---|---|---|
| `LLM_DAILY_FINAL_CAP` | 1500 | Tier-2 calls / UTC day | **THE binding cap.** Global across all lanes + users. Past it `score()` raises; jobs stay Queued. 0 = unlimited. | `app/config.py:191`; `reranker.py:120-123`, `:725-728` |
| `LLM_HOURLY_FINAL_CAP` | 150 | Tier-2 calls / clock hour | Smoothing valve. Needs ≥10 clock hours to spend the daily budget. | `app/config.py:192`; `reranker.py:124-125` |
| `LLM_PROVIDER_COOLDOWN_MINUTES` | 30 | minutes | Circuit breaker on credit/quota/`per day`/`rpd` errors | `app/config.py:190`; `reranker.py:45-56` |
| `LLM_RERANK_MAX_RETRIES` | 1 | attempts/backend | 1 = **no** in-call retry; the 90s lane re-queues instead | `app/config.py:189` |
| `LLM_REQUEST_TIMEOUT` | 45.0 | seconds | Per-request SDK timeout (SDK default is 600s) | `app/config.py:229` |
| `PRESCORE_ENABLED` | True | bool | Turns the two-tier cascade on | `app/config.py:183` |
| `PRESCORE_MODEL` | `gpt-4o-mini` | model id | Tier-1 bulk scorer | `app/config.py:185` |
| `PRESCORE_ADVANCE_THRESHOLD` | 35 | 0–100 | Effective gate = `min(this, shortlist_score_threshold)` = 35. **Sets the advance rate `a`, which sets throughput.** | `app/config.py:187`; `scoring_lane.py:490` |
| `PRESCORE_CAP` | 600 | jobs / matching pass | Tier-1 ceiling per matching-lane pass. Never binds (retrieval caps at 120 first). | `app/config.py:186` |
| `PRESCORE_WORKERS` | 16 | threads | Tier-1 concurrency, matching lane | `app/config.py:188` |
| `LLM_RERANK_CAP` | 100 | jobs / matching pass | Tier-2 ceiling per matching-lane pass | `app/config.py:174` |
| `LLM_RERANK_WORKERS` | 12 | threads | Tier-2 concurrency, matching lane | `app/config.py:175` |
| `SCORING_MODEL` | `claude-haiku-4-5-20251001` | model id | Tier-2 authoritative scorer | `app/config.py:365` |
| `DUAL_SCORE_ENABLED` | **False** | bool | gpt-4o as second final scorer. Off — ~2.5× Haiku input price for no quality gain. | `app/config.py:258` |
| `SCORING_FAIL_MAX_ATTEMPTS` / `_DEFER_HOURS` | 3 / 6.0 | attempts / hours | Poison-job attempt ceiling + deferral | `app/config.py:193-194` |

### 1.2 Scoring lane (primary scorer)

| Setting | Default | Unit | Gates | Where |
|---|---|---|---|---|
| `SCORING_LANE_ENABLED` | True | bool | Master switch | `app/config.py:242` |
| `SCORING_LANE_INTERVAL_SECONDS` | 90 | seconds | Sleep **after** each cycle → real period = duration + 90s | `app/config.py:243`; `server.py:806-819` |
| `SCORING_WORKERS` | 20 | threads | Global concurrent LLM workers — sized to provider rate limit, **not** user count | `app/config.py:244` |
| `SCORING_PER_USER_CAP` | 40 | jobs/user/cycle | Fairness cap in the round-robin | `app/config.py:245` |
| `SCORING_GLOBAL_CAP` | 200 | jobs/cycle | Work-list ceiling | `app/config.py:246` |
| `SCORING_LANE_MAX_SECONDS` | 120 | seconds | Hard wall clock; outer `wait_for` = 150s | `app/config.py:247` |

### 1.3 Matching lane (FAISS retrieval + self-heal backstop)

| Setting | Default | Unit | Gates | Where |
|---|---|---|---|---|
| `MATCHING_LANE_INTERVAL_MINUTES` | 5 | minutes | Cadence; per-tick deadline = 0.8 × interval = 240s | `app/config.py:231` |
| `MATCHING_CATCHUP_PASSES` / `_BACKLOG` | 4 / 200 | passes / jobs | Extra passes per user while backlog > floor | `app/config.py:232-233` |
| `TOP_K_RERANK` | 600 | jobs | Retrieval pool — **inert**, truncated to `cross_encoder_cap` first | `app/config.py:164` |
| `CROSS_ENCODER_CAP` | 120 | pairs/pass | **The CPU bottleneck.** ~1s/pair local. | `app/config.py:165` |
| `CROSS_ENCODER_MAX_LENGTH` / `_TEXT_CHARS` | 256 / 700 | tokens / chars | Per-pair truncation | `app/config.py:166-167` |
| `MIN_MATCH_SCORE` | 0.15 | cosine | CE floor; soft floor 0.6× admits top 5 if batch zeroes | `app/config.py:163` |
| `MIN_EMBEDDING_SCORE` | 0.28 | cosine | Stage-4 gate → stamps `rerank_score=15.0` | `app/config.py:371` |
| `GHOST_SCORE_THRESHOLD` | 0.6 | 0–1 | **Dead setting** — real cutoff is hardcoded in `GhostResult.is_ghost` | `app/config.py:383` vs `ghost_detector.py:70` |
| `REBUILD_MAX_JOBS` | 4000 | vectors | FAISS from-scratch rebuild cap (module constant, not env) | `app/matching/matcher.py:41` |
| `MAX_LIVENESS_CHECKS_PER_RUN` | 25 | checks/pass | ~2.5s each, **lock-held** → up to 62s of blocking I/O | `app/config.py:230` |

### 1.4 Discovery

| Setting | Default | Unit | Gates | Where |
|---|---|---|---|---|
| `DISCOVERY_INTERVAL_HOURS` | 6 | hours | Full global pass → 4 runs/day | `app/config.py:289` |
| `FRESH_LANE_INTERVAL_HOURS` | 2 | hours | Boards + 7 keyless feeds → 12 runs/day | `app/config.py:327` |
| `MAX_BOARDS_PER_RUN` | 400 | boards/run | 800 contributed to a prior OOM | `app/config.py:319` |
| `BOARD_PHASE_BUDGET_MINUTES` | 30 | minutes | Wall clock on the board phase | `app/config.py:323` |
| `MAX_JOBS_PER_SOURCE` | 200 | postings / source **instance** | Country-aware sources instantiate once per country → real ceiling 200 × countries | `app/config.py:59` |
| `KEYWORD_SEARCH_MAX_SLUGS` | 250 | boards/source/run | GH/Lever keyword search vs a ~56K registry | `app/config.py:64` |
| `SERPAPI_MAX_KEYWORDS` / `_CONCURRENCY` | 8 / 5 | searches / concurrent | 1 quota unit each; free tier = 100/month | `app/config.py:22-25` |
| `PULSE_TICK_SECONDS` / `_MAX_SECONDS` | 60 / 150 | seconds | Tick sleep (after work) / hard budget, 40% reserved for LLM | `app/config.py:348-349` |
| `PULSE_MAX_BOARDS_PER_TICK` | 300 | boards/tick | → 18,000 board polls/hour ceiling | `app/config.py:350` |
| `PULSE_FETCH_WORKERS` | 24 | threads | Fetch concurrency | `app/config.py:351` |
| `PULSE_FAST_INTERVAL_MINUTES` | 5 | minutes | Watchlist + recently-posting boards | `app/config.py:344` |
| `PULSE_FLOOR_INTERVAL_MINUTES` | 60 | minutes | "Every live board within the hour" promise | `app/config.py:345` |
| `PULSE_DEAD_INTERVAL_HOURS` | 24 | hours | Never-held-a-job boards | `app/config.py:346` |
| `PULSE_FAST_PATH_SCORE_CAP` | 10 | finals/tick | **Global** per tick, shared across users touched | `app/config.py:352` |
| `HOT_LANE_INTERVAL_MINUTES` / `_MAX_BOARDS` | 20 / 400 | min / boards | Legacy lane (only when `PULSE_LANE_ENABLED=0`) | `app/config.py:332-333` |

### 1.5 Adoption (shared pool → per-user pool)

| Setting | Default | Unit | Gates | Where |
|---|---|---|---|---|
| `ADOPT_MAX_JOBS` | 400 | rows/user/pass | **Not env-tunable.** The row-multiplication faucet. | `app/strategy/adoption.py:31` |
| `ADOPT_MAX_AGE_DAYS` | 21 | days | Freshness window on shared rows | `app/strategy/adoption.py:28` |
| `ADOPTION_SEMANTIC_MAX_EXTRAS` | 50 | rows/user/pass | Off-title neighbours — each is new LLM debt | `app/config.py:317` |
| `ADOPTION_SEMANTIC_MAX_CANDIDATES` | 1500 | embeddings/pass | CPU-bound MiniLM inference | `app/config.py:316` |
| `ADOPTION_SEMANTIC_THRESHOLD` | 0.30 | cosine | Résumé↔job floor | `app/config.py:315` |
| `ONBOARDING_MIN_JOBS` | 25 | jobs | Below this, fire a targeted per-user scrape | `app/config.py:298` |

### 1.6 Per-user product limits

| Setting | Default | Unit | Gates | Where |
|---|---|---|---|---|
| `DAILY_SHORTLIST_LIMIT` | 200 | Applications/user/UTC day | **Unreachable in practice** — see §2.4 | `app/config.py:269` |
| `SHORTLIST_SCORE_THRESHOLD` | 35 | 0–100 | Min fit to shortlist; also clamps the Tier-1 gate | `app/config.py:270` |
| `SHORTLIST_STRONG_THRESHOLD` | 65 | 0–100 | Client-side default board filter | `app/config.py:271` |
| `COMPANY_CAP` | 3 | active apps/company | + 40-day cooldown (`pipeline.py:46`), margin 5 to displace | `app/config.py:280`, `:287` |
| `SHORTLIST_MAX_AGE_DAYS` | 7 | days | Auto-SKIP untouched shortlists; frees cap slot | `app/config.py:278` |
| `SHORTLIST_RENDER_CAP` | 200 | cards | Dashboard LIMIT | `app/config.py:277` |
| `FRESH_ALERT_MIN_SCORE` | 65 | 0–100 | Alert bar (max of rerank/blended) | `app/config.py:272` |
| `FRESH_ALERT_DAILY_CAP` | 10 | notifications/user/day | + `MAX_ALERTS_PER_PASS=5`, 24h window (constants) | `app/config.py:273`; `fresh_alerts.py:32-33` |
| `TAILOR_ABUSE_DAILY_CAP` | 150 | tailors/user/day | **The only ceiling on user-triggered Sonnet spend.** Applies even to "unlimited" plans. | `app/config.py:275` |
| `PLAN_LIMITS[FREE]` | 5 / 2 | tailors/day, autofills/week | **Unreachable** — everyone resolves PRO while Stripe is unconfigured | `app/db/models.py:339`; `server.py:5674` |
| `DAILY_APPLY_LIMIT` | 25 | submissions/user/day | Autofill only | `app/config.py:268` |
| `DORMANT_USER_GRACE_DAYS` | 21 | days | Idle users dropped from all lanes — **defines "active user"** | `app/config.py:276` |
| `JOB_PURGE_MAX_AGE_DAYS` | 60 | days | Hard-DELETE closed, unapplied jobs (batch 2000 × 100) | `app/config.py:279` |

### 1.7 Infrastructure

| Setting | Default | Unit | Gates | Where |
|---|---|---|---|---|
| `LANES_ENABLED` | True | bool | **Must be 0 on every replica but one.** All locks/counters/claims are process-local. | `app/config.py:274`; `server.py:398-409` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 10 / 20 | connections | 30 total for 20 scoring + 16 prescore + 12 rerank + 24 pulse-fetch workers + web | `app/config.py:201-202` |
| `BROWSER_MAX_CONCURRENCY` | 1 | Chromium instances | ~300–500 MB each, child process charged to the cgroup | `app/config.py:396` |
| `BROWSER_SLOT_WAIT_SECONDS` | 120.0 | seconds | Waiters get `BrowserBusy` rather than queueing | `app/config.py:397` |
| `MEMORY_WARN_PCT` | 85.0 | % of cgroup | Only pre-OOM signal (an OOM kill leaves no traceback) | `app/config.py:415` |

---

## 2. Throughput math

### 2.1 Jobs discovered per day

**Aggregator postings fetched (pre-dedupe ceiling):**

```
Full lane (4 runs/day, phase="all"):
  9 keyless aggregators × 200            = 1,800   (Indeed RSS off by default: config.py:34)
  HN who-is-hiring                       =   200
  5 country-aware × 200 × 5 countries    = 5,000   (only with all API keys set)
  ------------------------------------------------
  no keys set:  2,000/run  × 4 =  8,000/day
  all keys set: 7,000/run  × 4 = 28,000/day

Fresh lane (12 runs/day, phase="fresh"):
  7 keyless feeds × 200 = 1,400/run × 12 = 16,800/day

TOTAL aggregator postings fetched/day: 24,800 (stock) → 44,800 (fully keyed)
```
*Sources: `config.py:59`, `:289`, `:327`; `pipeline.py:902-918`, `:946-975`, `:935-940`.*

**Board polls:**

```
Full lane:   400 × 4  =   1,600/day
Fresh lane:  400 × 12 =   4,800/day
Pulse lane:  300/tick × ticks/day

  ticks/day = 86,400 / (tick_duration + 60s sleep)     [server.py:750-757]
    worst case (150s ticks): 86,400/210 =   411 ticks → 123,300 polls/day
    best case (instant):     86,400/60  = 1,440 ticks → 432,000 polls/day

TOTAL board polls/day: ~130,000 – 438,000
```

**Net NEW `Job` rows/day: the code does not determine this.** It depends on (a) how many postings each board returns — no constant in the repo, (b) the churn rate of those boards, and (c) the `_upsert` dedupe hit rate. Three mechanisms suppress nearly all of it: the pulse `poll_hash` signature short-circuit (`pulse_lane.py:419-425`), the 6h content-hash fast path (`pipeline.py:311-313`), and the `cross_source_slug` dedupe (`pipeline.py:357-397`).

**What *is* determined** is the per-user faucet: adoption copies **≤400 rows/user/pass** (`adoption.py:31`) and adoption runs after every full + fresh lane pass = **16 passes/day** → a **6,400 rows/user/day ceiling**, realistically 100–400/day for a normal role set.

**Binding cap at this step:** `MAX_JOBS_PER_SOURCE=200` and `MAX_BOARDS_PER_RUN=400` on the fetch side; `ADOPT_MAX_JOBS=400` on the per-user side. None of them is the system's constraint — discovery is wildly over-provisioned relative to scoring (§2.6).

### 2.2 Tier-1 prescores per day

The raw worker ceiling is never reached; the finals budget gates it. Every job that gets scored pays exactly one prescore; a fraction `a` (the advance rate) then pays a final:

```
finals = a × prescores   ⇒   prescores = finals / a = 1500 / a

  a = 0.10 → 15,000 prescores/day  (15,000 jobs leave the queue)
  a = 0.30 →  5,000 prescores/day  ( 5,000 jobs leave the queue)
  a = 0.50 →  3,000 prescores/day  ( 3,000 jobs leave the queue)
  a = 0.90 →  1,667 prescores/day  ( 1,667 jobs — the regime config.py:187 calls "a cost ADD")
```

Raw ceiling for comparison: `scoring_global_cap=200` × 411–720 cycles/day = **82,200–144,000 job-slots/day**, i.e. **16–48× more than the budget allows.** Neither `scoring_workers=20`, nor `scoring_global_cap=200`, nor `scoring_lane_max_seconds=120` is ever the limiter at defaults.

**Two uncapped prescore leaks** (OpenAI Tier-1 never calls `_register_final_call()` — confirmed by grep: only `reranker.py:602` (Anthropic Tier-1) and `:751` (Tier-2) register):

| Leak | Ceiling | Why |
|---|---|---|
| Pulse fast path after the budget trips | 10/tick × 411–1,440 ticks = **4,110–14,400 prescores/day** | `pulse_lane.py` has **no** cycle-level `llm_budget_exhausted()` check. It pays the prescore, then `score()` raises. |
| Matching lane | ≤120 candidates × ~576 passes/day = **~69,000/day** theoretical | `pipeline.py:710-748` — no budget pre-check either; bounded only by the 240s tick deadline and per-user unscored corpus |

**Binding cap:** none directly on Tier-1. It is bounded only *indirectly* by the scoring lane's fast-exit (`scoring_lane.py:410-411`), which the other two lanes don't share.

### 2.3 Tier-2 finals per day

```
Hourly:  150  (LLM_HOURLY_FINAL_CAP)
Daily:  1500  (LLM_DAILY_FINAL_CAP)

150 × 24 = 3,600 > 1,500  ⇒  THE DAILY CAP BINDS.
Reaching 1,500 requires ≥10 clock hours (1500/150).
```

**Burstiness.** At `a=0.5`, one scoring cycle offers 200 jobs → ~100 finals. The 150/hr budget is therefore consumed by **~1.5 cycles ≈ 3–5 minutes** of each clock hour; the remaining ~55 minutes every cycle fast-exits with `skipped: "LLM budget reached"`. That skip is **not logged at INFO** (`server.py:813` only logs when `scored` or `shortlisted` is truthy), so the degradation is invisible.

Consequence: the "fresh jobs scored within ~a minute" claim (`server.py:791-793`) holds for a job adopted at :01 and not for one adopted at :06.

**Once the daily cap trips, the *entire* lane stops** — the check is at cycle entry (`scoring_lane.py:410-411`), before the free ghost gate and free Tier-1 drain. For the remaining ~14 hours the backlog stops draining at all.

### 2.4 Shortlists per user per day

Nominal cap is `daily_shortlist_limit = 200` (`config.py:269`). It is unreachable:

```
Platform-wide finals/day                    = 1,500
Shortlist rate (score ≥ 35)                 ≈ 25%   [not determined by code — assumption]
Platform-wide shortlists/day                ≈   375
Per user, round-robin across N users        ≈ 375/N

  N = 10 →  ~37 shortlists/user/day
  N = 30 →  ~12
  N = 100 →  ~4
```

To hit 200 shortlists in one day a single user would need ~800 of the platform's 1,500 finals — possible only if they are the sole active user.

**Board size** is separately bounded: `shortlist_max_age_days=7` auto-SKIPs untouched entries (`shortlist_hygiene.py:37-40`), `company_cap=3`, and `shortlist_render_cap=200` truncates the render.

**Binding cap:** `LLM_DAILY_FINAL_CAP`, not `DAILY_SHORTLIST_LIMIT`.

### 2.5 Full funnel, one pass, one user (matching lane)

```
FAISS index                ≤ 4,000 vectors        matcher.py:41
  ↓
Retrieval corpus           ≤ 2,000 unscored       matcher.py:363 (corpus_cap)
  ↓ BM25 + FAISS + RRF(k=60)
Cross-encoder              = 120 exactly          config.py:165  ← CPU wall
     (60 reserved for freshest + 60 by relevance) fresh_budget.py:51-72
  ↓ CE score ≥ 0.15 (soft floor 0.09 admits top 5)
Cheap gates: rule(10) → ghost(5) → embedding(15) → door(20)
  ↓
Tier-1 prescore            ≤ 600 (never binds)    config.py:186
  ↓ advance if ≥ 35
Tier-2 Claude              ≤ 100                  config.py:174  ← first cap that can bite
  ↓ ≥ 35, daily limit, company cap
SHORTLISTED Application
```

`TOP_K_RERANK=600` is **inert** — `search_for_resume` truncates to `ce_cap=120` before the `[:k]` slice (`matcher.py:482` then `:528`).

### 2.6 The imbalance

| Stage | Daily capacity | Ratio to scoring |
|---|---|---|
| Board polls | 130,000 – 438,000 | 87× – 292× |
| Aggregator postings fetched | 24,800 – 44,800 | 17× – 30× |
| Adoption ceiling per user | 6,400 rows/user | — |
| **Jobs authoritatively scored** | **1,500** | **1×** |

Every adopted row is permanent LLM debt — `adoption.py:92-97` says so in its own comment. Debt accrues 20–300× faster than the budget services it.

---

## 3. Cost model

### 3.1 Pricing — assumptions, clearly labelled

**Verified (Anthropic first-party rates, from the `claude-api` skill's cached model table):**

| Model | Input $/MTok | Output $/MTok |
|---|---|---|
| `claude-haiku-4-5` | $1.00 | $5.00 |
| `claude-sonnet-4-6` | $3.00 | $15.00 |

**Verified prompt-caching mechanics:**
- Cache **read** ≈ **0.1×** base input → $0.10/MTok on Haiku, $0.30/MTok on Sonnet
- Cache **write** = **1.25×** base input for the default 5-minute ephemeral TTL → $1.25/MTok on Haiku, $3.75/MTok on Sonnet
- **Minimum cacheable prefix: 4,096 tokens on Haiku 4.5; 1,024 tokens on Sonnet 4.6.** Below the minimum, `cache_control` is silently ignored — no error, `cache_creation_input_tokens: 0`.

> **The 4,096 figure independently confirms the code comment at `reranker.py:385-388`, which is why `_CACHE_MIN_BLOCK_CHARS = 15500` exists.** The padding is not cargo-culted; it is load-bearing.

**ASSUMED (not verifiable from this repo — OpenAI is not the Anthropic pricing surface):**

| Model | Input $/MTok | Cached input $/MTok | Output $/MTok |
|---|---|---|---|
| `gpt-4o-mini` | $0.15 | $0.075 | $0.60 |
| `gpt-4o` | $2.50 | $1.25 | $10.00 |

**Token estimation:** ~4 chars/token throughout. All character counts are read from the code.

### 3.2 Code-derived token counts

| Call | Cached prefix | Uncached | Output | Source |
|---|---|---|---|---|
| **Tier-2 final** | rubric 2,900–3,500 ch (~725–875 tok) + résumé block padded to ≥15,500 ch (~3,875–4,000 tok) = **~4,600–4,875 tok** | JD `[:5000]` + headers = **~1,300 tok** | max 600, typical **250–350** | `reranker.py:393-395`, `:398-430`, `:521-533` |
| **Tier-1 prescore** | system ~150–200 tok + résumé `[:4000]` (~1,000 tok) = **~1,200 tok** (OpenAI auto-cache) | JD `[:1800]` ≈ 450 tok | max 120, typical **~35** | `reranker.py:329-349`, `:574-585` |
| **Tailor résumé** | TAILOR_SYSTEM (~625 tok) + full master résumé (~2,000–4,000 tok) = **~2,600–4,600 tok** | JD `[:5000]` + ATS block ≈ **1,500 tok** | max 4,000, typical **1,200–2,000** | `tailor.py:143-235` |
| **Cover letter** | system + résumé (**typically < 4,096 tok → does NOT cache on Haiku**) | JD `[:4000]` ≈ 1,000 tok | max 600 | `tailor.py:237-283` |
| **Grounding verify** | none (fresh client per call) | **full master résumé** + 1 bullet ≈ 2,500 tok | max **10** | `grounding.py:109-140` |
| **Doctor verdict** | none | JD `[:1500]` + résumé `[:3000]` ≈ 1,125 tok | max 120 | `doctor.py:246-247` |
| **Senior review** | system + 12,626 B of profiles ≈ **4,157 tok** (*barely* clears 4,096) | JD `[:6000]` + ledger ≈ 2,000 tok | max **1,200** | `senior_reviewer.py:234-246` |

### 3.3 Cost per call

**Tier-2 final (Haiku 4.5), warm cache:**
```
cached prefix : 4,700 tok × $0.10/M = $0.00047
uncached JD   : 1,300 tok × $1.00/M = $0.00130
output        :   300 tok × $5.00/M = $0.00150
                                      ─────────
                                      $0.00327  ≈ $0.0033/final
```

**Tier-2 final, cold (first job for a user, or >5 min gap → cache write at 1.25×):**
```
4,700 × $1.25/M = $0.00588 + $0.00130 + $0.00150 = $0.00868  ≈ $0.0087/final
```

**Without the padding fix** (prefix ~2,500 tok, below Haiku's 4,096 minimum → nothing caches, every call bills at 1×):
```
4,700 × $1.00/M = $0.00470 + $0.00130 + $0.00150 = $0.00750
```
→ **the `_CACHE_MIN_BLOCK_CHARS` padding cuts steady-state Tier-2 cost by ~56%** ($0.0075 → $0.0033).

**Tier-1 prescore (gpt-4o-mini) — assumed pricing:**
```
cold : 1,670 × $0.15/M + 35 × $0.60/M = $0.00025 + $0.00002 = $0.00027
warm : 1,200 × $0.075/M + 470 × $0.15/M + out    = $0.00018
                                                    ≈ $0.0002/prescore
```

**Tier-1 fallback to Anthropic** (`_prescore_anthropic`, when OpenAI is missing or cooling down). Its `cache_control` marker is **inert** — the system block is only ~200 tokens, far below the 4,096 minimum:
```
1,670 × $1.00/M + 35 × $5.00/M = $0.00185/prescore  →  ~7–10× the gpt-4o-mini path
```
(the code comment at `reranker.py:597` says "~5x" — my token-derived figure is somewhat higher.) **And it charges the finals budget** (`reranker.py:602`), so falling back to Haiku Tier-1 roughly **halves effective final throughput** to ~75/hour.

### 3.4 Platform cost per day at the caps

```
Tier-2:  1,500 finals × $0.0033 (mostly warm) ≈ $4.95/day
Tier-1:  (1500/a) prescores × $0.0002

  a = 0.50 →  3,000 × $0.0002 = $0.60   TOTAL ≈ $5.55/day
  a = 0.30 →  5,000 × $0.0002 = $1.00   TOTAL ≈ $5.95/day
  a = 0.10 → 15,000 × $0.0002 = $3.00   TOTAL ≈ $7.95/day

Plus the pulse-lane prescore leak after the cap trips:
  4,110–14,400 × $0.0002 = $0.82 – $2.88/day

PLATFORM SCORING SPEND: ~$5.6 – $8.0/day  ≈ $170 – $240/month
```

This **matches the config comment's own estimate** of "~$5-8/day worst case" (`config.py:191`) and "~$0.50-0.75/hr" ($0.49/hr computed: 150 × $0.0033). Note the repo's flat ledger estimate of $0.010/final (`app/analytics/spend.py:25`) **over-states Tier-2 by ~3×**.

### 3.5 Cost per job

| Metric | Formula | a=0.3 | a=0.5 |
|---|---|---|---|
| Per job **drained** from the queue | $0.0002 + a × $0.0033 | **$0.0012** | **$0.0019** |
| Per job **authoritatively scored** | $0.0002 + $0.0033 | **$0.0035** | **$0.0035** |
| Per **shortlist** (25% shortlist rate — assumption) | $0.0035 / 0.25 | **~$0.014** | **~$0.014** |
| Per job **ghost-filtered** | $0 (`scoring_lane.py:251-258`) | $0 | $0 |

### 3.6 Cost per user per month

Scoring spend is **fixed and global** — it does not scale with users, so per-user cost is purely the division:

```
$170–240/month ÷ N active users

  N =  5 →  $34 – $48 /user/month
  N = 10 →  $17 – $24 /user/month
  N = 20 →  $8.50 – $12
  N = 50 →  $3.40 – $4.80
```

Against `PLAN_PRICES[PRO] = $10/mo` (`models.py:350`): **scoring alone exceeds revenue below ~N=20** — before tailoring, senior review, Supabase egress, or Railway compute.

### 3.7 The uncapped spend: tailoring

Tailoring is user-triggered Sonnet 4.6 spend that **never calls `_register_final_call()`** and is therefore entirely outside `LLM_DAILY_FINAL_CAP`.

Per tailor (`MAX_TAILOR_ATTEMPTS=2`, `tailor.py:478`), using verified Sonnet 4.6 + Haiku 4.5 rates:

```
Résumé attempt 1 (cold cache, Sonnet 4.6):
   2,600 tok × $3.75/M (write) + 1,500 × $3.00/M + 1,500 × $15.00/M  = $0.0368
Résumé attempt 2 (warm):
   2,600 × $0.30/M + 1,500 × $3.00/M + 1,500 × $15.00/M              = $0.0278
Cover letter (Haiku, prefix < 4,096 tok → no caching):
   3,500 × $1.00/M + 500 × $5.00/M                                    = $0.0060
Doctor verdict (Haiku, PASS only):                                     = $0.0016
Grounding verify (Haiku, full résumé per flagged bullet, UNBOUNDED):
   2,500 × $1.00/M each                                               = $0.0025 × N

TOTAL:
  best  (1 attempt, 0 flagged bullets)     ≈ $0.045
  typical (1–2 attempts, 3–8 bullets)      ≈ $0.05 – $0.09
  bad   (2 attempts, ~40 verify calls)     ≈ $0.17
```

The ledger's flat $0.05 estimate (`spend.py:29`) is right for the typical case and ~3× low for the bad case.

**At the abuse ceiling of 150 tailors/day/user: $6.75 – $25.50 per user per day** — i.e. **1× to 5× the entire platform scoring budget, from one account.** And with Stripe unconfigured every user resolves to PRO (`server.py:5674-5675`), so `TAILOR_ABUSE_DAILY_CAP` is the *only* limit in play; `PLAN_LIMITS[FREE].tailor_daily = 5` is unreachable.

### 3.8 The other uncapped spend: senior review

Fires on every job-drawer open, gated only by `not application.senior_verdict` (`server.py:3640-3649`), and never registers against the budget:

```
warm: 4,157 × $0.10/M + 2,000 × $1.00/M + 800 × $5.00/M = $0.0064
cold: 4,157 × $1.25/M + 2,000 × $1.00/M + 800 × $5.00/M = $0.0112
```
Browsing 50 shortlisted jobs = **$0.32 – $0.56**, synchronously inside the request handler.

---

## 4. How many users can this serve today?

### The reasoning chain

**Step 1.** The platform can authoritatively score **1,500 jobs/day** (`config.py:191`). This is global, not per-user.

**Step 2.** The scoring lane distributes that budget **round-robin across users** — all per-user queues are fetched *before* the global cap is applied, then interleaved by depth (`scoring_lane.py:446-459`). The split is therefore **fair but not larger**: each of N users gets ~**1,500/N** finals/day.

**Step 3.** Each final is preceded by a prescore, and jobs below the gate are drained by the prescore alone. Jobs fully processed per user per day = **(1,500/N) / a**.

**Step 4.** A user's daily inflow of new `rerank_score IS NULL` rows comes from adoption (≤400/pass × 16 passes/day = 6,400 ceiling) plus pulse-lane per-user routing. Realistic inflow for a normal role set: **100–400 rows/user/day**.

**Step 5.** Steady state requires **drain ≥ inflow**:

| Advance rate `a` | Drain/user/day | Inflow 400 | Inflow 250 | Inflow 150 | Inflow 50 |
|---|---|---|---|---|---|
| 0.30 | 5,000/N | **N ≤ 12** | N ≤ 20 | N ≤ 33 | N ≤ 100 |
| 0.50 | 3,000/N | **N ≤ 7** | N ≤ 12 | N ≤ 20 | N ≤ 60 |
| 0.10 | 15,000/N | N ≤ 37 | N ≤ 60 | N ≤ 100 | N ≤ 300 |

**Step 6.** A *tighter* constraint than backlog equilibrium is **freshness**. The 150/hr cap is exhausted in the first 3–5 minutes of each clock hour (§2.3). The pulse fast path alone budgets 10 finals/tick × ~60 ticks/hr = 600 finals/hr — **4× the sustainable rate** — so the product's core promise ("be first to apply") degrades from minutes to "within the hour, if you were lucky about timing" well before the backlog constraint bites.

**Step 7.** The dormancy gate (`DORMANT_USER_GRACE_DAYS = 21`, `config.py:276`) is what makes N = *active* users rather than *registered* users. It is doing real load-bearing work: registered users idle >21 days are dropped from `_lane_user_ids`, `_active_users`, and `_scorable_user_ids`, so they consume neither adoption, scoring slots, nor budget.

### The answer

> **~10–15 active users comfortably. 15–30 with visible freshness degradation. Structurally broken past ~30.**

At **10 users**: 150 finals/user/day, ~500 jobs/user/day drained, backlog stable, freshness mostly holds. Scoring cost ~$17–24/user/month.

At **30 users**: 50 finals/user/day. Inflow at 150–400/day exceeds drain; the unscored backlog grows monotonically. Jobs sit "Queued". The 5-minute pulse promise becomes multi-hour. `freshest-first` ordering means old jobs are never reached at all.

At **100 users**: 15 finals/user/day. The product does not function as designed.

**Binding constraint: `LLM_DAILY_FINAL_CAP = 1500` (with `LLM_HOURLY_FINAL_CAP = 150` as the tighter freshness constraint).**

The config comment on that line says "Raise as paying users grow" — that is exactly correct, and it is the single lever. Raising it to 15,000/day (~$50–80/day) buys ~100–150 users, at which point the constraint shifts to Anthropic account rate limits (§6).

---

## 5. Per-user marginal cost

### 5.1 DB rows

| Item | Rate | Bound |
|---|---|---|
| New `Job` rows/day | 100–400 realistic; **6,400 ceiling** (400 × 16 adoption passes) | `adoption.py:31`; `config.py:289`, `:327` |
| Row shape | **Full copy including the multi-KB description** — uniqueness is `(user_id, source, external_id)` | `models.py:74-79` |
| `Application` rows/day | ≤ shortlists (~12–37/user/day realistic; 200 nominal cap) | `config.py:269` |
| `FunnelEvent` rows | **1 per newly-inserted job, forever, no retention policy anywhere** | `pipeline.py:418`; `funnel.py:15-32` |
| Steady-state pool size | **Unbounded.** Per-user pools get **no** age-based close — only shared (`__shared__`) rows are closed at 45d. Purge requires `is_closed=True` **and** no Application. | `server.py:608-615`; `job_retention.py:24` |

**This is the second-worst scaling term after LLM cost.** At 200 new rows/day with no per-user age-close, a year-old account holds ~73,000 full `Job` rows with descriptions. Multiplied by N users, that is the Supabase egress driver that already forced the batch-scoped dedupe prefetch rewrite (`pipeline.py:258-292`) and the `load_only` projection in adoption (`adoption.py:149-168`).

### 5.2 FAISS disk

```
IndexFlatIP, DIM = 384, float32  →  384 × 4 = 1,536 bytes/vector
                                    matcher.py:33, :310
id sidecar: int64                →  8 bytes/vector

At a from-scratch rebuild (REBUILD_MAX_JOBS = 4000, matcher.py:41, :265):
   4,000 × 1,536 =  6.14 MB  index
   4,000 ×     8 = 32 KB     .ids.npy
```

> ⚠️ **6.14 MB is NOT an upper bound.** `REBUILD_MAX_JOBS` limits only the candidate `SELECT` for a *from-scratch* rebuild (`matcher.py:265`, `:302`). The incremental branch (`matcher.py:328-340`) does `np.concatenate` on the id map and `existing_index.add(new_embs)` on the loaded index — **no eviction, no truncation**. The on-disk index therefore accumulates past 4,000 vectors across passes and only shrinks when `force_rebuild` fires (i.e. when some indexed job has `embedding_id IS NULL`, `matcher.py:277`). **Per-user FAISS disk is unbounded over time.**

Files also live on ephemeral container disk (wiped on redeploy → N × 4,000 MiniLM encodes to re-warm) and are **not deleted on account deletion** (`server.py:7943-7962` omits them).

### 5.3 LLM calls

| Per user per day | At N=10 | At N=30 |
|---|---|---|
| Tier-2 finals (1,500/N) | 150 | 50 |
| Tier-1 prescores ((1500/N)/a, a=0.3) | 500 | 167 |
| Ghost drains | free, unmetered | free |
| Senior reviews | 1 per drawer-open, **uncapped** | uncapped |
| Tailors | ≤150/day, **uncapped by the scoring budget** | ≤150/day |

### 5.4 CPU seconds

Derived from the code's own measured claims (`matcher.py:36-41`: ~4,000 encodes ≈ 1 min → ~66 encodes/s; `rerank_backend.py:3-5`: ~1s/pair for the local cross-encoder on Railway's fractional CPU):

| Work | Per unit | Per user per day |
|---|---|---|
| Adoption semantic pass | ≤1,501 MiniLM encodes ≈ **23s** | × 16 passes = **~370s** |
| FAISS from-scratch rebuild | ≤4,000 encodes ≈ **60s** | 1× after deploy, then incremental |
| Cross-encoder (local provider) | 120 pairs × ~1s = **~120s/pass** | dominant per-pass cost |
| Embedding filter | (C+1)×120 ≈ 840 encodes ≈ **13s/pass** | ~7× waste — résumé chunks re-encoded per candidate (`embedding_filter.py:26-36`) |
| BM25 build | O(2,000 docs), no cache | per pass |
| **Total per matching pass** | | **~130–190s** — matches `scoring_lane.py:8-12`'s "~130s/user" |
| Résumé load (Storage GET + pypdf/docx parse) | | 1× per 90s scoring cycle + 1× per pulse tick touched |

Setting `RERANK_PROVIDER=jina` collapses the 120s cross-encoder to one ~300–800ms HTTP call and keeps the ~200 MB model unloaded (`config.py:171`; `docs/MEMORY.md:18`).

### 5.5 Memory (per-process, shared — not per-user)

```
torch + transformers      ~300–400 MB
MiniLM (one instance)     ~100 MB      matcher.py:160-166
cross-encoder (lazy)      ~200 MB      unloaded under RERANK_PROVIDER=jina
FAISS + rows in rebuild   ~50–250 MB
5 lanes                   ~100–300 MB
─────────────────────────────────────
ML baseline               ~600–800 MB (2 GB realistic floor)
+ each headless Chromium  ~300–500 MB  child process, charged to the cgroup
```
Gated to **one** Chromium by `BROWSER_MAX_CONCURRENCY=1` (`config.py:396`). Three concurrent was an OOM kill on its own (`docs/MEMORY.md:27-38`).

---

## 6. What binds, in order

### 1️⃣ `LLM_DAILY_FINAL_CAP = 1500` — binds **now**

*`app/config.py:191`; `reranker.py:120-123`, `:725-728`*

A single process-local counter shared by every lane and every user. Round-robin makes the split fair; it cannot make it bigger. Per-user throughput is strictly **O(1/N)** — contradicting the scoring lane's own docstring ("9 users or 10,000, the scorer runs the same 20 workers flat-out", `scoring_lane.py:8-12`), which is true only of the *worker pool*.

**Symptom:** jobs stuck at "Queued"; `skipped: "LLM budget reached"` in cycle stats (unlogged at INFO).
**Fix:** raise the cap. ~$0.0033/final means 15,000/day ≈ $50/day ≈ 100–150 users.

### 2️⃣ `LLM_HOURLY_FINAL_CAP = 150` — binds **now**, for freshness specifically

*`app/config.py:192`; `reranker.py:124-125`*

Even with the daily cap raised, 150/hr caps the platform at 3,600/day and makes backlog drain arithmetically impossible (a 5,000-job backlog needs 33 clock hours). It also makes scoring **bursty**: the hour's budget is gone in 3–5 minutes, then ~55 minutes of skipped cycles. The pulse fast path's 10-finals/tick budget is 4× the sustainable hourly rate.

**Symptom:** the "fresh within minutes" promise silently becomes "within the hour", timing-dependent.

### 3️⃣ Tailoring + senior review — **already unbounded**, binds on the first abusive or heavy-browsing user

*`config.py:275`, `:366`; `server.py:5801-5817`, `:3640-3649`*

Neither passes through `_register_final_call()`. At 150 tailors/day/user × $0.045–$0.17 = **$6.75–$25.50/user/day** — one user can be 1–5× the whole platform scoring budget. The grounding checker's per-flagged-bullet Haiku calls (each carrying the **full master résumé**, serially, uncapped, no caching, `grounding.py:109-140`, `:213-222`) are the least-bounded LLM cost in the codebase. Senior review adds $0.0064–$0.0112 per job-drawer open, synchronously in the request handler, with only a truthy-`senior_verdict` cache.

### 4️⃣ Anthropic / OpenAI account rate limits — binds after (1) and (2) are lifted

*`config.py:244`, `:229`, `:189`*

With both budget caps removed, the ceiling becomes 200 jobs/cycle × 411–720 cycles/day = **82,200–144,000 job-slots/day** → ~25,000–43,000 finals/day at a=0.3, requiring 20 sustained concurrent Haiku calls. With `llm_rerank_max_retries=1` (no in-call retry) and a 45s timeout, any 429 defers the job a full 90s cycle. Throughput becomes a function of the Anthropic tier, not the code. Cost jumps to **~$85–140/day**.

### 5️⃣ Per-user `Job` row growth — binds on Supabase egress/storage, ~6–12 months out

*`adoption.py:31`; `server.py:608-615`; `job_retention.py:24`*

Per-user pools have no age-based close; only `__shared__` rows do. At 100–400 full rows/day/user (descriptions included), the `job` table grows as O(N × days) with no ceiling. This is already the documented egress driver that forced two query rewrites. `funnel_events` is worse — one row per discovered job, forever, with no purge job at all.

### 6️⃣ Single-process memory ceiling — binds on any attempt to scale *up*

*`docs/MEMORY.md:14-22`; `config.py:274`, `:319`, `:396`*

~600–800 MB ML baseline in the same cgroup as every lane and any transient Chromium. Raising `SCORING_WORKERS`, `MAX_BOARDS_PER_RUN` (800 caused an OOM), or `BROWSER_MAX_CONCURRENCY` (+~400 MB each) all push one limit that **cannot be escaped by adding replicas** — extra replicas must run `LANES_ENABLED=0` and therefore add zero scoring capacity. Escape hatches: `BROWSER_SERVICE_URL` (moves ~400 MB out) and `RERANK_PROVIDER=jina` (keeps ~200 MB unloaded).

### 7️⃣ Pulse-lane board coverage vs registry size — degrades silently as the registry grows

*`config.py:64`, `:345`, `:350`; `pulse_lane.py:94-120`*

Ceiling is 300 boards/tick × 60 ticks/hr = **18,000 polls/hr** against a ~56K registry. Honoring the 60-minute floor for `L` live boards needs `L/60` polls/min, watchlist boards need `F/5`, dead boards `D/1440`. The floor holds only while live boards ≲12,000. The config comment already pegs steady-state demand at ~150 boards/min = 9,000/hr — **half the ceiling is already consumed**. `_due_boards` just returns the 300 stalest; "within the hour" becomes "within several hours" with no error and no metric.

### 8️⃣ Matching lane O(users) serialization — degrades past ~2–4 users

*`server.py:884-912`; `config.py:231-233`, `:165`*

Iterates users serially under the discovery lock with a 240s deadline; each pass is ~130–190s (FAISS rebuild + BM25 + 120 cross-encoder pairs). Only ~2 users fit per tick, and the loop **always restarts from the head of the list** (no rotation, unlike the scoring lane). Users deep in the list never get retrieval, re-shortlisting, or the self-heal resets — only the lock-free scoring lane reaches them. This is already true today; it is why the scoring lane exists and why the matching lane was demoted to a backstop.

### 9️⃣ Cost observability — not a capacity limit, but blocks safely raising (1)

*`scoring_lane.py:179-180`, `:519-534`; `spend.py:24-29`*

At default settings (`dual_score_enabled=False`), `_pick_provider` returns `None`, `Reranker.score()` never reports its backend, and **successful Claude finals record nothing in `LlmSpend`** — only prescore drains and local fallbacks are attributed. The dominant cost line is invisible to per-user spend reporting. Raising the caps to grow throughput would be flying blind.

---

## 7. Things the code does not determine

Stated explicitly rather than invented:

- **Postings returned per board fetch.** No constant, no measurement. The ~10/board figure circulating in the subsystem map is an estimate, not code.
- **Net NEW `Job` rows per day.** Depends on board churn and dedupe hit rate; the three dedupe mechanisms are in the code but their hit rate is not.
- **The Tier-1 advance rate `a`.** `config.py:187` notes that a gate of 30 advanced "~90%", implying a≈0.9 there, but gives no measured figure for the current gate of 35. All throughput results are presented as functions of `a`.
- **Shortlist rate among finals.** Used 25% as an explicit assumption; nothing in the repo measures it.
- **OpenAI pricing.** Not in this repo, not on the Anthropic pricing surface. Labelled as an external assumption in §3.1 and used only for Tier-1 figures.
- **Registry size.** Comments disagree: `config.py:61-63` says ~56K boards, `registry.py:81-83` says ~22K seeded of ~86K available, `server.py:536` says ~62K. All are prose, none are measured.
- **Actual per-user résumé length**, which determines whether the Tier-2 cache padding engages at all (`_CACHE_PAD_MAX_REPEATS=5` means a résumé body under ~2,580 chars gets **no** padding, no caching, and costs $0.0075/final instead of $0.0033 — a 2.3× penalty on exactly the new-user population).