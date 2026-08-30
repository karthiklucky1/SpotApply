# CLAUDE.md — SpotApply (formerly HirePath)

> Deep reference (read from the code, `file:line` cited): **docs/ARCHITECTURE.md** (topology,
> per-user lifecycle, the full ranking cascade) · **docs/CAPACITY.md** (every cap + arithmetic;
> note its banner — the allocation moved to per-plan caps) · **docs/SCALING.md** (10→10k roadmap) ·
> **docs/MEMORY.md** (OOM post-mortem). This file stays the short working map.

AI job-application copilot (app.spotapply.ai). Discovers tech roles from ATS APIs/feeds, scores each
against the user's résumé via a cascade, tailors résumé+cover letter (grounded), and
auto-fills forms. **The human always reviews and clicks Submit** — never auto-submit.

Multi-tenant web app (grew from a single-user agent). Public README has the full story;
this file is the working map for editing the code.

## Architecture
`Discover → Match cascade → Score & enrich → Tailor (grounding check) → Auto-fill → User reviews & Submits`

**Matching cascade** (`app/matching/pipeline.py`, cheapest-first so LLM cost stays low):
1. Retrieval — BM25 + FAISS (`all-MiniLM-L6-v2`) over UNSCORED jobs only (newest
   first) → top-K (`matcher.py`); scored jobs re-shortlist via direct query, not
   retrieval — letting them compete starved fresh postings of CE slots
2. Rule filter — title/seniority/location/job-type, per-company cap (`filters/`)
3. Ghost filter — drops inactive/fake postings
4. Embedding gate — cosine-similarity floor
5. LLM reranker — two-tier cascade (`reranker.py`): a cheap Tier-1 model
   (`prescore()`, GPT-4o-mini or Haiku) bulk-scores up to `prescore_cap` fresh
   candidates; only those clearing the advance gate reach Tier-2 (Claude, the
   authoritative 0–100 + reasoning). Clear misfits are stamped with their
   prescore so they exit the unscored corpus — draining the backlog instead of
   re-reading it every pass. Toggle with `PRESCORE_ENABLED`.
6. Hire probability — blends fit + hiring-intent signals (`hire_probability.py`)

## Stack
Python 3.11 · FastAPI/Uvicorn · SQLModel. **Supabase Postgres + Auth in prod; local
SQLite fallback when `SUPABASE_URL` is unset.** Claude (primary LLM) / OpenAI optional ·
sentence-transformers + FAISS + rank-bm25 · Playwright (Chromium) + MV3 Chrome extension ·
APScheduler · Jinja + Tailwind + Chart.js (server-rendered).
**Landing AND dashboard CSS are compiled + committed**, one config per template:
`landing.html` → `tailwind-landing.css` (`build:css`), `dashboard.html` →
`tailwind.css` (`build:css:dashboard`); AOS self-hosted in `app/static/vendor/`.
After editing classes in either run `npm run build` — never hand-edit the output
(the dashboard's hand-compiled file went stale and silently dropped JS-built
badge classes). tests/test_landing_assets.py guards both;
`.claude/hooks/build-tailwind.sh` rebuilds the affected one automatically. All
OTHER authed/public templates (auth, pricing, extension, messages, privacy,
terms, recruiter, public_profile) stay on the Tailwind Play CDN.

## File structure
```
app/
  api/server.py     # ALL routes, dashboard, auth, admin (single large file)
  config.py         # Settings (pydantic-settings, env-driven)
  db/models.py      # SQLModel tables; supabase_client.py = JWT/user_id; init_db.py
  discovery/        # ATS scrapers; sources/ = aggregators & feeds (~20)
  matching/         # matcher, filters/, reranker, hire_probability, pipeline
  tailoring/        # tailor, ats_keywords, grounding (anti-hallucination), doctor
  autofill/         # Playwright filler + answer_pack
  intelligence/     # sponsorship/H1B, work_auth, urgency, referral,
                    # skill_gap (JD vs resume/GitHub advice), job_check (free ghost/fit check)
  strategy/         # scoring_lane, pulse_lane/hot_lane, adoption, realign, degraded, hygiene
  analytics/        # funnel, reporter
  qa_store/         # canonical answers (answers.yaml) + resolver
  templates/        # landing, dashboard, pricing, auth, privacy, terms, extension
extension/          # MV3: background.js, content.js, popup
mobile/             # Expo React Native app (iOS+Android) — Supabase auth +
                    # /api/* JSON client only, no backend coupling (own README)
scripts/            # run_discovery, run_matching, seed_registry, status_check
tests/              # pytest (matching, tailoring, grounding, autofill, funnel...)
data/               # résumé master, FAISS index, generated docs, local SQLite
```

## Key models (`app/db/models.py`)
`Job`, `Application`, `UserProfile`, `UserSubscription`/`UserUsage`/`PlanTier`,
`CompanyRegistry`, `DiscoveryRun`, `FunnelEvent`, `PendingQuestion`, `AnswerMemory`,
`UserPersonalMemory`, `H1BSponsor`, `UserNotification`, referrals/coupons.

UI-relevant `Job`/`Application` fields: `rerank_score` (0–100 fit), `rerank_reasoning`,
`blended_score` (priority), `hire_probability_signals` (JSON), `ghost_score`/`ghost_flags`,
`custom_highlight_block`.

## Conventions & decisions
- **Multi-tenancy:** every query is scoped by `user_id` from the Supabase JWT.
  `_get_user_id` returns None when anonymous — scoping off it FAILS OPEN, so a
  non-public route must refuse the anonymous case first via a guard that RAISES:
  `_require_user` (:258), `_require_owned_application` (:349),
  `_require_admin_user` (:5487), `_require_admin` (:7732), all in server.py.
  `"local"` = SQLite dev user. Never leak data across users; check ownership on
  per-application routes.
- **Scrape once, serve many:** all scheduled lanes write postings ONCE to the
  shared pool (`Job.user_id == SHARED_POOL_USER`, pipeline.py); per-user pools
  are filled by `strategy/adoption.py` (cheap DB copy by roles+country; also
  runs on resume upload + role edits = instant feeds). Scheduled discovery is
  ONE global pass with the union of all users' roles — never per-user.
- **Roles re-point the pool** (`strategy/realign.py`): `target_roles` are re-derived
  on every résumé upload (`target_roles_auto`; a hand edit pins them). On a real
  role change: on-role jobs are re-scored ONLY when RECENT + SHORTLISTED
  (`REALIGN_RESCORE_DAYS`=2) — re-judging a whole pool would burn days of the
  finals cap on postings nobody is looking at, so everything else keeps its
  score. Jobs the OLD roles brought in that don't fit the new ones leave the
  board and are stamped `Off-role`, leaving the `rerank_score IS NULL` queue
  with NO LLM call. Jobs matching NEITHER list are left alone — the gate names
  domains, not every real title. TAILORED-and-beyond untouched; parks reverse on
  the way back (`[roles-realign]`, and a parked job is ALWAYS requeued when
  on-role again or its marker score would strand it). Cap: `REALIGN_MAX_RESCORE`.
- **Role families** (`discovery/title_filter.py` `_ROLE_FAMILIES`): the alias table
  is directional and missed neighbours — "Software Developer" expanded to only
  {software developer, software} ("developer" is generic), so Backend/Frontend/SDE
  postings were rejected, as was "Machine Learning Engineer" for an "AI Engineer"
  user. Families are SYMMETRIC: matching any member pulls in all of them. The gate
  is meant to be permissive (a false positive gets scored and ranked; a false
  negative is a job the user never sees).
- **Scheduler:** `server.py`'s asyncio scheduler runs global discovery→adopt→match
  ~every `DISCOVERY_INTERVAL_HOURS` in BOTH local and prod, plus a "fresh lane"
  every 2h (`_global_fresh_scan`, phase="fresh" = registry boards + free keyless
  feeds; quota-keyed sources stay on the full lane; env FRESH_LANE_INTERVAL_HOURS,
  0 disables) and a board-freshness lane: the "pulse lane" by default
  (`strategy/pulse_lane.py`, per-board `next_poll_at` schedule — watchlist
  `UserProfile.target_companies` + recently-posting boards every 5 min, every
  live board ≤60 min, dead boards daily; unchanged boards skipped via
  `poll_hash`; new jobs take a lock-free per-job fast path: ghost check →
  prescore cascade → Claude → shortlist → fresh alert). Set PULSE_LANE_ENABLED=0
  to fall back to the legacy 20-min "hot lane" (`strategy/hot_lane.py`) — only
  one of the two runs. Do NOT also schedule those in
  `app/main.py` (it only adds the harvester/validator/report jobs)
  — double-runs otherwise.
- **SELECTED IS NOT POLLED** (pulse lane): a tick selects up to
  `PULSE_MAX_BOARDS_PER_TICK` and hard-stops at `pulse_tick_max_seconds`.
  Whatever never came back is DEFERRED — `_defer_boards` moves ONLY
  `next_poll_at` (short retry + id-derived jitter). It must never touch
  `last_seen`/`poll_hash`/`job_count`/`failure_count`: those are the record of
  an actual fetch, and advancing a deferred board on the normal cadence (the old
  behaviour, at ~88% of each tick) made the schedule describe a poll rate the
  lane wasn't achieving. Only a COMPLETED fetch calls `_mark_polled` — which now
  also writes the schedule, one round-trip instead of two, and applies
  exponential backoff on a real failure. Futures are drained in COMPLETION order
  (`as_completed`), not submission order. Tick stats bucket every selected board
  exactly once (`fetch_ok`/`fetch_failed`/`unsupported`/`deferred`, deferrals
  split cancelled/running/unconsumed — that split names the bottleneck before
  anyone touches worker counts). Never derive a poll count from ticks × selected.
- **Scoring lane** (`strategy/scoring_lane.py`, every `SCORING_LANE_INTERVAL_SECONDS`):
  the decoupled, PARALLEL, cross-user scorer — drains the global `rerank_score
  IS NULL` queue across ALL users with a fixed pool of `scoring_workers` (GPT
  prescore → Claude final), so throughput is bounded by LLM rate limits, not
  user count (the matching lane scores users serially = O(users)). Lock-free
  (no FAISS); the 5-min matching lane stays as the retrieval + reshortlist +
  self-heal backstop. Set `SCORING_LANE_ENABLED=0` to fall back to matching-lane-only.
- **Run modes:** prod = `uvicorn app.api.server:app`; local all-in-one = `python -m app.main`.
- **Jinja filters** (`server.py`): `fromjson`, `cleantext`, `humanize_signal`
  (turns raw signal tokens like `fresh_posting_4d` → "Posted 4 days ago").
- **Dashboard** is one big `templates/dashboard.html` (HTML + inline `<script>`). Modals
  toggle via `style.display` (not the `hidden` class — inline `display` overrides it).
  After editing, validate: parse Jinja + `node --check` the touched `<script>` block.
- **Tuning lives in env/Settings:** `shortlist_score_threshold` (60 — of real
  Claude finals 44.5% cleared 35 but only 11.6% cleared 65, so the old bar
  shortlisted ~1,800 jobs/user that the board's own default filter
  (`shortlist_strong_threshold`=65) then hid. **Raise `PRESCORE_ADVANCE_THRESHOLD`
  in lockstep** — the Tier-1 gate is `min(advance, shortlist)`), `top_k_rerank`,
  `MIN_MATCH_SCORE`, `DAILY_APPLY_LIMIT`, `*_BOARDS` slugs.
- **Freshness has TWO bounds** (`app/common/freshness.py` — read it before
  touching any age gate): KNOWN age = `coalesce(first_seen, discovered_at)`,
  how long WE have held the posting (tight: `SCORING_MAX_JOB_AGE_DAYS` /
  `SHORTLIST_MAX_AGE_DAYS` = 5, the "be first to apply" promise); POSTED age =
  `coalesce(posted_at, …)`, what the source claims (loose: `*_MAX_POSTED_AGE_DAYS`
  = 30, and it exists ONLY to suppress evergreen/ancient listings). Stale = past
  EITHER. These were one `coalesce(posted_at, first_seen, discovered_at) < 5d`
  expression, so posted_at won and a job discovered TODAY expired the instant an
  ATS called it a week old — 82.9% of `rerank_score=8.0` stamps were already ≥5d
  old at first sight, and 11 of 13 users were stamped down to an empty queue,
  invisible to `_scorable_user_ids`. ATS dates are not reliable enough for that
  (Greenhouse `updated_at` moves on edits, aggregators stamp their crawl date,
  some feeds are future-dated), so `first_seen - posted_at` is NOT crawler
  latency. All four consumers — scoring gate, render filter, shortlist hygiene,
  description stripper — build from that module, and widening either bound is
  spend-neutral (per-cycle finals/prescores are capped, so a bigger queue changes
  WHICH jobs the fixed budget buys).
- **`rerank_score` is overloaded**: it carries real 0–100 verdicts AND the ghost
  (5.0) / age-expiry (8.0) sentinels, so `rerank_score IS NOT NULL` is NOT "was
  scored" (production's "621k scored jobs" was mostly expiry stamps). Lifecycle
  now lives in its own columns, written INSIDE the existing updates (no extra
  round-trip): `prescored_at`, `scored_at`, `expired_at`. Count with
  `genuinely_scored_expr()` / `expired_without_scoring_expr()`, never the raw
  NULL check. Stage latency: `scripts/stage_latency.py`.
- **Spend is a per-user BUDGET, not a result cap** (`matching/finals_budget.py`):
  `PLAN_LIMITS["finals_daily"]` (Free 15 / Pro 50 / Agency 100) is the SOFT
  point; past it a user keeps scoring only while candidates are strong
  (`FINALS_PROMISE_FLOOR`=55 raises the Tier-1 gate in burst territory) AND the
  last `FINALS_YIELD_WINDOW` finals still clear the bar
  (`FINALS_YIELD_CONTINUE_RATE`). Bounded by burst (×2/day) and weekly (×7 =
  the SAME money the flat cap cost — bursting reallocates, never adds). Decided
  in `scoring_lane._finals_allowance` (slice + gate), inherited by the pulse
  fast path; plan lookup fails OPEN. Counters live in `UserUsage.finals_count/
  finals_hits` — **persisted**, because in-memory ones reset on every deploy.
  Never chase a target: 6 good jobs means 6. The queue is promise-ordered
  (`_user_queue`: freshness filters, prescore sorts) so the money buys the best
  candidates, not the newest. `LLM_DAILY_FINAL_CAP` (5000) / `_HOURLY_` (400) are now
  only a runaway backstop + burst smoothing — raise as users grow.
- **LLM cost guards** (`reranker.py` + `scoring_lane.py`): dual-provider finals
  OFF by default (gpt-4o was ~2.5x Haiku for no quality gain — `DUAL_SCORE_ENABLED`);
  credit/quota circuit breaker `LLM_PROVIDER_COOLDOWN_MINUTES` (30) — trips on
  billing errors AND daily-quota 429s ("requests per day"); per-job attempt
  ceiling defers repeat failures (`SCORING_FAIL_MAX_ATTEMPTS`); résumé block
  padded past Haiku's 4096-token cache minimum and written once per user/cycle by
  `Reranker.prewarm_cache` (`max_tokens=0` prefill) — a cache entry is unreadable
  until the response writing it streams, so 20 concurrent workers otherwise all
  miss and all pay the 1.25x write; cache telemetry every 25 finals; adoption
  extras bounded by `ADOPTION_SEMANTIC_MAX_EXTRAS`. **Every lane checks
  `llm_budget_exhausted()` BEFORE Tier-1** — prescores are cheap, not free.
- **DB egress:** never `select(Job)` on a hot path. Retrieval + FAISS rebuild use
  `matcher._candidate_columns()` (6 cols, description truncated in SQL — nothing
  reads past ~800 chars). Full descriptions put Supabase at 205% of its egress
  quota on 2 MB of stored data (tests/test_retrieval_egress.py).
- **Company cap** (3 active apps/company, 40d cooldown): a new job outscoring the
  weakest merely-SHORTLISTED cap-holder by ≥`COMPANY_CAP_DISPLACE_MARGIN` (5)
  displaces it (→SKIPPED); TAILORED-and-beyond apps are never displaced.
- **DB discipline:** NEVER hold a session across an LLM call (scoring lane is
  read → LLM → idempotent write-back). Pool is env-tunable (`DB_POOL_SIZE` 10 /
  `DB_MAX_OVERFLOW` 20) — the old 5+10 starved funnel/web when lanes overlapped.
  Every MULTI-ROW companyregistry write is per-PK executemany, ascending id,
  ONE monotonic transaction per homogeneous group, wrapped in
  `app.common.db_retry.run_with_deadlock_retry` (all such writers are
  idempotent). Piecewise-sorted groups in one txn deadlocked production
  (68-99/day pre-single-consumer; once after). Guard: `test_registry_lock_order`.
- **Memory discipline** (`docs/MEMORY.md` — one container holds torch + models +
  FAISS + all lanes + Chromium): ALL Playwright launches go through
  `app.common.browser.browser_slot` (`BROWSER_MAX_CONCURRENCY`, default 1 — each
  headless Chromium is a ~400MB child process charged to the container but
  invisible in our RSS; unbounded concurrency was an OOM kill). Load MiniLM via
  `matcher._get_embed_model()` — never construct a second `SentenceTransformer`.
  `app.common.memuse` + the memory watcher log the climb; `/api/debug/memory`
  (admin) shows `non_python_mb` = the browsers. **LLM SDK clients come ONLY from
  `app/common/llm.py`** (shared process-wide pair; `with_options()` for per-path
  timeout/retry) — a fresh `Anthropic()`/`OpenAI()` per call leaks an httpx pool
  + SSL context. Lanes reuse persistent thread pools (never a per-tick
  `ThreadPoolExecutor` — glibc-arena churn); allocator env (`MALLOC_ARENA_MAX=2`
  etc.) is pinned in the Dockerfile and must stay process env.
- **Browser service** (`browser-service/`, its own container + README): the three
  STATELESS render/search paths (JD scrape, Google discovery, search-engine
  source) call `app.common.browser_client`, which routes to the service when
  `BROWSER_SERVICE_URL` is set and otherwise renders locally behind the gate —
  flip it with one env var, no code change. Autofill/preview stay local on
  purpose (stateful interactive sessions; server-side autofill is founder-only
  via `autofill_multi_user_enabled`, everyone else fills via the MV3 extension).
  New page-rendering code belongs in the client, NOT a fresh `pw.chromium.launch`.
- **Compliance:** public ATS/feeds only, respect robots.txt; no LinkedIn/Indeed
  automation (discovery-only links). Tailoring must stay grounded in the real résumé.

- **Distilled scorer (shadow)** — `docs/DISTILLATION.md`: export LLM finals
  (`scripts/export_training_data.py`) → fine-tune cross-encoder on Colab
  (`scripts/train_local_scorer.py`) → drop model at `LOCAL_SCORER_PATH` →
  shadow mode records LLM-vs-local agreement (`scripts/shadow_report.py`).
  Flip to local-first only on ≥90% shortlist-decision agreement. `build_pair`
  must stay identical in `local_scorer.py` + the train script. The competing
  "compiler layer" plan (JD → per-family scoring program) is gated by
  `scripts/compiler_replay.py`: fits linear programs against logged Claude
  finals (LOO-validated, `--selftest` for synthetic check) — build the
  compiler only if COMPILABLE families cover most scored volume.

- **CardRace v2 (shadow)** — `docs/CARDRACE_DESIGN.md`: understand-once matching.
  JobCard per DISTINCT posting (shared across tenants, `matching/cards.py`) ×
  UserCard per user → deterministic `g()` (`card_match.py`: dual direct/expanded
  score via `skill_graph.py` inference; spread = assumption share) → conformal
  bands (`conformal.py`; **no calibration file = everything BAND = Claude decides**).
  `CARD_MATCH_SHADOW=1` (default) records agreement beside every real final
  (`card_match_shadow` table); fit with `scripts/build_calibration.py`; NEVER set
  `CARD_MATCH_ENABLED=1` before its holdout gates pass (§3.4). Mint spend capped
  by `CARD_MINT_DAILY_CAP` and never charged to plan finals.

## Workflow
- Tests: `pytest` (or target files); lint: `ruff check app`.
- Validate template/python edits before committing; keep commits scoped + descriptive.
- Branch per the session's assigned feature branch; commit + push when done.
- CI installs requirements MINUS the ML stack, so the app must import from its
  DECLARED deps (jinja2 was missing; prod only worked because torch pulls it in).
  Suite runs twice — normal + reversed file order — with `--disable-socket`; skips
  capped at 8.
- **Guard tests fail on a whole CLASS of mistake** — read the one that covers what
  you're touching (rationale + incidents: docs/AUDIT_2026_07_30.md).
  `route_auth_inventory` (every route on `PUBLIC_PATHS` with a reason or guarded,
  + ownership on id-bearing routes; `if uid and uid != "local"` is FAIL-OPEN and
  leaked 7 routes) · `account_deletion` (schema-driven — a new user-scoped table
  fails until handled) · `architecture_invariants` (Playwright only in
  `browser_slot`; MiniLM/CrossEncoder only in `matcher._MODEL_CACHE`; no
  unprojected `select(Job)` on a hot path) · `settings_defaults` (the load-bearing
  numbers + their lockstep relations) · `index_declarations` (the 3 DDL sites can't
  disagree) · `grounding_enforcement` (3 states; "never ran" ≠ passed) ·
  `pulse_deferral` (a board never fetched is never recorded as polled) ·
  `expiry_semantics` (the two freshness bounds, and that expiry stamps aren't
  counted as scoring) · `registry_lock_order` (every multi-row companyregistry
  txn locks ascending-PK; the deadlock class) · `signature_stability` (poll
  hashes come from listing-phase entries, immune to detail-fetch jitter; a
  volatile hash never overwrites the baseline).
- **Tests must clean up only their OWN rows.** A wholesale `delete(Job)` /
  `delete(CompanyRegistry)` takes out fixtures other files already built, which
  is a suite that fails differently every run. Prefix your rows and delete by
  that prefix; scope global-count assertions to them too.

## Maintenance
Update on major architectural changes or completed modules. Keep under ~150 lines —
prune stale info rather than appending.
