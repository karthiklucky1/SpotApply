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
                    # skill_gap (JD vs resume/GitHub advice), job_check (free ghost/fit check),
                    # hiring_contacts (evidence-typed people/org assertions from JD text)
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
  per-application routes. Account removal is ONE deletion for two callers:
  `DELETE /api/account` and the daily orphan reconcile both run
  `app/common/account_purge.py` (schema-driven, children-first, both storage
  buckets). `purge_orphaned_accounts` compares our tenants against the Supabase
  Auth listing and deletes NOTHING on any doubt — a listing that raised, came
  back empty, did not paginate to the end, or implies more orphans than live
  users (guard: `test_account_purge`). One deleted auth user had left 33k job
  copies, 2,860 applications and 13 storage objects behind.
- **Temporary Pro is a PLAN grant, never a payment** (`TEMPORARY_PRO_FOR_ALL`,
  `billing.temporary_pro_active`, docs/TEMPORARY_PRO_AND_SUBSCRIPTIONS.md): ON =
  every tenant resolves PRO through the ONE entitlement lookup
  (`_get_user_plan`, which every gate and lane already reaches directly or via
  `common/plan_limits`), so no second check can drift. It writes NO rows —
  flipping it off needs no migration. `is_paid_entitlement` must NEVER learn
  about it (pinned structurally): if it did, MRR would be a headcount and the
  dormancy gate would apply to nobody — the 90.8%-of-a-week's-spend bug exactly.
  PRO's own ceilings + the platform backstops still bound spend: "Pro allowances
  for all", never unlimited. While ON nothing is for sale SERVER-SIDE
  (`purchase_available()`; `/api/billing/checkout` 503s before any Stripe call,
  bank details withheld) but `/api/billing/portal` and the webhook stay OPEN — a
  subscriber keeps their card, invoices and cancel button precisely because the
  month is free. Copy is one string, NO end date, NO auto-enrolment
  (`TEMPORARY_PRO_NOTICE`), rendered server-side on `/` and `/pricing` so a page
  can't advertise a price the route is refusing. **No $100/mo recruiter-research
  product exists** — every `$100` is the Pro price, `/recruiter` is a free
  recruiter-facing portal, and `payment_options().recruiter_research` states the
  absence so one isn't invented. Charging is Stripe's, not ours:
  `scripts/audit_subscriptions.py` (READ-ONLY, no `--apply`, no mutator) buckets
  the rows and answers it; a live-key row IS still billed and stopping it is an
  unauthorised financial mutation. Guard: `test_temporary_pro`.
- **PAID ≠ PLAN** (`app/billing.py`, 2026-09-17): `_get_user_plan` answers what
  LIMITS a user gets; `billing.is_paid_entitlement(row)` answers whether they
  are PAYING (a non-FREE, unexpired row that is either a manual/bank activation
  or Stripe-backed under `sk_live_`). Only the latter overrides the dormancy
  gate (`_user_paid_search_is_live`) or counts as MRR. A grandfathered PRO (no
  row, `PLAN_GRANDFATHER_UNTIL` unset) and a test-mode (sandbox) subscription
  are complimentary — reading `plan != FREE` as "paying" let 10 dormant
  accounts take 90.8% of a week's LLM spend at the PRO ceiling. Production ran
  on `sk_test_` keys the whole beta: `stripe_live_mode()`/`stripe_mode()` say
  so, `warn_if_stripe_test_mode` logs it at boot, `/api/admin/metrics` reports
  `sandbox_subscriptions`/`grandfathered_users`/`stripe_mode`, and
  `active_users_7d` reads `UserProfile.last_active_at` (authenticated
  requests), never `Application.updated_at` (lanes bump it for everyone). The
  webhook must be subscribed to checkout.session.completed,
  customer.subscription.created/updated/deleted AND invoice.paid; checkout
  completion now fetches the period end itself and `reconcile_subscriptions`
  (daily, `_billing_maintenance`) re-reads rows the stream let drift (the
  founder's row sat on `current_period_end=NULL` = never expires for two weeks).
- **Scrape once, serve many:** all scheduled lanes write postings ONCE to the
  shared pool (`Job.user_id == SHARED_POOL_USER`, pipeline.py); per-user pools
  are filled by `strategy/adoption.py` (cheap DB copy by roles+country; also
  runs on resume upload + role edits = instant feeds). Scheduled discovery is
  ONE global pass with the union of all users' roles — never per-user.
  Adoption also runs INCREMENTALLY every matching-lane tick
  (`adopt_incremental`: shared rows `first_seen >=` a per-user watermark minus
  one interval, cap 200; the watermark advances only after an UNCAPPED step, so
  a capped step replays the same window and `_drop_already_adopted` makes the
  replay free). It ran only on the ~6 h scheduler before: adoption p95 was
  ~110 min for a DB copy the pulse route does in 4 s.
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
  **The selection is ADAPTIVE (AIMD)**: a tick that deferred >
  `PULSE_ADAPTIVE_DEFER_PCT` (50%) of what it selected, or hit the consumer
  deadline, halves the next cap (floor `PULSE_MIN_BOARDS_PER_TICK` 40); a clean
  tick inside 70% of its budget grows it 25%+1 back toward
  `PULSE_MAX_BOARDS_PER_TICK`. Selecting 300 and consuming 69 (09-16: 231/300
  deferred, upsert p90 38s, deferrals 1-4K → 20-41K/day) was ~230 wasted fetches
  per tick feeding back into a Disk-IO-exhausted database. `board_cap` is in
  every tick event; `PULSE_ADAPTIVE_ENABLED=0` restores constant selection.
- **Scoring lane** (`strategy/scoring_lane.py`, every `SCORING_LANE_INTERVAL_SECONDS`):
  the decoupled, PARALLEL, cross-user scorer — drains the global `rerank_score
  IS NULL` queue across ALL users with a fixed pool of `scoring_workers` (GPT
  prescore → Claude final), so throughput is bounded by LLM rate limits, not
  user count (the matching lane scores users serially = O(users)). Lock-free
  (no FAISS); the 5-min matching lane stays as the retrieval + reshortlist +
  self-heal backstop. Set `SCORING_LANE_ENABLED=0` to fall back to matching-lane-only.
  **Housekeeping never spends the cycle**: `_expire_stale_unscored` runs FIRST
  and was unbounded, so when its SELECT hit Supabase's statement timeout (~150s
  vs a 120s deadline) every cycle logged `queued: 200, scored: 0` — alive, on
  schedule, buying nothing, for hours, on two consecutive builds. It now takes a
  slice (`SCORING_EXPIRY_MAX_SECONDS` 20) + a per-statement `SET LOCAL`
  ceiling, and reports `expiry_stopped`. Stopping it early is free (`_user_queue`
  bounds by the same freshness expression, so unswept rows never reach a
  worker); stopping SCORING early is what users feel. Any new pre-scoring step
  must be bounded the same way. **The matching lane HOLDS its `inflight`
  claims from the LLM call through the Phase-3 write** (`held_claims`, released
  in one `finally`): `with claim()` let go the moment a score came back, and
  the job read `rerank_score IS NULL` to the other two lanes for the minutes
  until Phase 3 stored it. Its loop sleeps `interval − elapsed` (floor 30 s) —
  the interval is a cadence, not a pause after the work. The sweep runs PER OWNER on `ix_job_unscored`
  with the known bound spelled index-friendly (`freshness.known_before_expr`:
  `first_seen < c OR (first_seen IS NULL AND discovered_at < c)` — provably the
  coalesce, guard: `test_expiry_sweep`); the old whole-table
  `coalesce(...) < c` scan could use no index and hit the statement timeout in
  159 of 271 cycles (`expiry_stopped="error"`), leaving an 84-day-old unscored
  copy in one queue. One owner's cancelled statement is counted
  (`owners_failed`) and the sweep CONTINUES; owners rotate round-robin. The scheduler's `wait_for` cannot cancel a
  `to_thread` cycle, so an overrun drops later ticks — now logged, was silent.
- **Run modes:** prod = `uvicorn app.api.server:app`; local all-in-one = `python -m app.main`.
- **Jinja filters** (`server.py`): `fromjson`, `cleantext`, `humanize_signal`
  (turns raw signal tokens like `fresh_posting_4d` → "Posted 4 days ago").
- **Dashboard** is one big `templates/dashboard.html` (HTML + inline `<script>`). Modals
  toggle via `style.display` (not the `hidden` class — inline `display` overrides it).
  After editing, validate: parse Jinja + `node --check` the touched `<script>` block.
  **Per-user aggregates are bounded AND cached** (`app/common/ttl_cache.py`):
  `/api/freshness-stats` (was 52s — it loaded every open row of a 65k pool to
  take two medians; now SQL aggregates, 300s TTL), the `/api/pipeline/live`
  pool tile (300s) and the `/api/jobs` COUNTs (60s per filter signature) run
  through `_BoundedReads`; a timed-out count is `None`/`degraded: true`, never
  0, is never cached, and the JS pages on "did a full page arrive".
  `_BoundedReads` expunges before it rolls back so rows earlier panels loaded
  keep their state (a rollback expires them and the sort keys read them after
  the session closes). Admin surfaces: `/api/admin/settings` (secrets shown only
  as `configured: bool` — guard: `test_admin_observability`) and
  `/api/admin/health` (breakers, budget, stripe mode, pool, memory, last ticks).
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
- **FILL then CHALLENGE — the day's count ends delivery, not the search**
  (`strategy/slate.py` + `matching/finals_budget.py`, 2026-09-12;
  docs/DELIVERY_ARCHITECTURE.md). Until `PLAN_LIMITS["shortlist_daily"]` (Free
  20 / Pro 35) jobs reach the board, scoring runs FLAT OUT. After that the
  budget does NOT return 0 — it raises `Allowance.gate` to the day's **cutoff**
  (the fit score of the weakest entry a challenger could replace, floored at
  the shortlist bar), and that gate already reaches all three lanes as
  `spend_gate`. Discovery, routing and Tier-1 continue; only Tier-2 narrows.
  Returning 0 was a kill switch: the pulse fast path (70% of shortlists)
  returned BEFORE Tier-1, so on 3 of 6 days the board filled 18:00-22:00 UTC and
  a 15:12 posting worth 92 waited behind 35 jobs scoring 71-73.
  **`slate.place()` is the ONLY writer of a SHORTLISTED application** (guard:
  test_daily_slate) — capacity, the company cap and the challenger rule live
  there, because three lanes each carrying their own `today_count < cap` check
  had already drifted. **It is also the ONLY idempotency check**: it locks the
  job row (`FOR UPDATE`) and answers `exists` when an Application is already
  there — every caller's own "is there an application?" pre-check raced (one job
  delivered twice, 1.7 s apart, by the matching and scoring lanes). Every
  decision writes ONE `FunnelEvent(stage="placement", reason=outcome)` with
  user/score/cutoff, so a qualified job that missed the board says why
  (guard: test_delivery_race). A challenger beating the cutoff by `slate_displace_margin`
  (5) replaces the weakest **replaceable** entry = SHORTLISTED, delivered today,
  `viewed_at IS NULL`; anything opened/tailored/applied/dismissed is permanent.
  If nothing is replaceable, `slate_overflow_margin` (15) delivers it anyway, up
  to `slate_overflow_daily` (5). Challenge spend is bounded by what is LEFT of
  `finals_daily` — no second counter. `SLATE_CHALLENGE_ENABLED=0` restores the
  old stop. Remaining stops: spent ≥ `finals_daily` (Free 120 / Pro 250 — the
  cost ceiling); yield collapsed. **MEASURED over six days
  2026-09-05..11** (supersedes the 5-hour sample that claimed 6.5): hit rate
  held at **15.2%** (738 finals → 112 shortlists) but **finals per delivered job
  was 15.6** for the user who hit the ceiling — 2.4x the design number, because
  the queue was 73% ineligible, not because the bar was wrong. Fix supply, not
  the ceiling. $0.0025/final measured; `spend.py`'s old $0.010 was 4.1x high and
  its Tier-1 estimate is still UNMEASURED. **No window is longer than a day and
  nothing is paced**: the weekly ceiling + release curve took production to zero
  finals for 39 hours on 09-03 while reporting itself healthy. Two rules from
  that, pinned by test: a spend control must never retroactively invalidate
  spend already made, and a reason meaning "you get nothing" is never filed
  under healthy — **decided by the NUMBERS, never the reason's wording**
  (2026-09-19): the ceiling is tested BEFORE the delivered branch, so a
  completed day that also ran out of budget reads "daily cost ceiling (…) at
  26/20 delivered", and keying on the `delivered` prefix called a user on 130%
  of their plan a stall for 13 hours. `allowance()` sets `Allowance.target_met`
  where both numbers are already loaded (deriving it in the lane costs an
  uncached `user_subscription` SELECT per capped user per cycle) and it
  defaults FALSE — not knowing is never evidence the day went well. The lane
  files that as `target_met_users`, everything else as `plan_capped_users` +
  `plan_capped_reasons` (cost_ceiling / yield_collapsed / prescore_allowance /
  slate_full; a bare count meant naming the stop took three lanes' logs).
  `/api/admin/budget-diagnostic` answers all of it per account without DB
  access (no secrets, no full user id).
  The **yield stop** needs a real sample — hits/finals TODAY, judged only past
  `FINALS_YIELD_WINDOW` (50) finals, continue at ≥2%: at a 10% true rate zero
  hits in 10 finals happens 35% of the time, and the first version read an
  in-process ring only a purchased final could refill, so a coin-flip left users
  at zero finals until the next deploy. **Promise ordering** decides WHICH
  finals you buy: `_user_queue` orders the user's whole unscored queue by
  `prescore` in SQL, unknowns ranked AT the advance gate (never 100 — that let
  an unjudged job pre-empt a genuine 90). It only works because the matching
  lane now PERSISTS the prescores it cuts at `_tier2_cap`; every other writer
  sets `Job.prescore` as a job leaves the queue, so dropping them left the whole
  waiting corpus NULL and the ORDER BY collapsed to arrival order. Never chase
  the target: 6 good jobs means 6. `delivered_today()` is the ONE definition of
  what reached the board (email imports excluded) — the budget and all three
  shortlist caps read it. Plan lookup fails open to the widest plan ceiling,
  never to unbounded; counters persist in `UserUsage`. `LLM_DAILY_FINAL_CAP`
  (15000) / `_HOURLY_` (2000) are platform backstops — Anthropic Tier-1
  prescores charge the same counter, which is why `PRESCORE_BUDGET_MULTIPLIER`
  is 2.
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
  `prewarm_cache` obeys the SAME breaker + budget (it had no gate: during the
  09-14..16 outage it was the only call still asking, twice per user per cycle).
  **Attribution names the backend that ANSWERED, never the one requested**
  (`Reranker.score_with_meta` → meta `provider`/`model`/`usage`; `score()` stays
  the 4-tuple wrapper). 09-14..16 Anthropic rejected every call (unpaid
  balance) and OpenAI served every final, yet cycles logged `by_claude` and the
  ledger booked Haiku rates. **The spend ledger is METERED and has ONE writer**
  (`analytics/spend.py`): the Reranker buffers each real call with provider,
  model and token usage priced from `PRICES_PER_MTOK`; the lanes only flush.
  A call that did not happen records nothing — ghost stamps, rule rejections and
  the empty-JD guard were booked as 26K prescores/week that OpenAI never saw,
  and the flat estimate ran 3.9x the bill. `LlmSpend` is keyed by
  (user, day, kind, provider, model); `metered_share` in `/api/admin/spend`
  says how much is measured. `provider_status()` (down_since, sanitised
  last_error) feeds `/api/admin/health`; the breaker WARNING names the outage
  length and who is serving. Grounding (`_ask_verifier`, records
  `verifier_provider`) and card minting consult the SAME breaker — neither may
  call a provider the lanes already know is down.
- **DB egress:** never `select(Job)` on a hot path. Retrieval + FAISS rebuild use
  `matcher._candidate_columns()` (6 cols, description truncated in SQL — nothing
  reads past ~800 chars). Full descriptions put Supabase at 205% of its egress
  quota on 2 MB of stored data (tests/test_retrieval_egress.py).
- **Eligibility is ONE deterministic gate, applied at EVERY door**
  (2026-09-12): `_upsert` only filters when the caller passes
  `preferred_country`/`role_gate_terms`, and the three doors passed different
  things — the pulse lane's per-user route passed NEITHER, which is why the lane
  that delivers 70% of shortlists was the one with no location filter. All doors
  now pass both, and `app/common/tenant_prefs.effective_country` is the ONE
  resolution the scoring PROMPT also reads: a blank profile country meant "no
  gate" at intake while Claude was still told the candidate wants the US and
  scored everything else 0-30, so we admitted foreign postings for free and paid
  to reject them (73% Tier-1 drain). `geo.detect_country` resolves in tiers — US
  signal > foreign country NAME > `, XX` US state code > foreign city — because
  city-before-state made Dublin OH Irish and Melbourne FL Australian. Role terms
  drop DOMAIN tokens (`_DOMAIN_TOKENS`: "full" matched every "Full Time", "data"
  matched "Data Entry"); `_STRUCTURAL_TOKENS` is the smaller set preference
  learning reads, where "sales" IS the signal. **A scraper must read fields the
  API actually emits**: `ashby.py` read `locationName` (not a real key), so every
  Ashby row was stored with a blank location and `remote=isRemote`, and 11 of 31
  audited cards — Warsaw, London, Vilnius hybrids for US users — sailed through
  a gate that keeps "unspecified" (guard: `test_ashby_scraper`). Location now
  comes from `location`/`address.postalAddress`/`secondaryLocations`, salary
  from `compensation` → `RawJob.salary_text` (scraper value wins over the
  description regex, and copies keep it). `location_allowed` splits multi-site
  strings and keeps a posting if ANY site is the user's country; the scorer's
  job block prints "Location: not stated in the posting" instead of a blank.
- **Location eligibility is ONE decision, decided from evidence, once per
  posting** (2026-09-18, `docs/GEO_ELIGIBILITY.md`; NEW postings only):
  adapters fill `RawJob.geo` (every site untruncated, structured country,
  workplace type, the source's remote-restriction field — a title suffix, a
  department, an HQ or a currency is never location evidence). The shared
  `_upsert` records `JobGeography` keyed `(source, external_id)` for keys it
  has never seen (`geo_verify.derive`: structured fields + description
  restriction scan); every per-user door then calls
  `eligibility.decide(geography, geo_prefs)` → `Job.eligibility` =
  eligible / ineligible (dropped, or stamped 10.0 "Location filtered:") /
  unknown (HELD: excluded from `_user_queue`, retrieval and the pulse fast
  path; refused by `slate.place`). "Remote"/"Homeoffice" establish no
  country; a remote role anchored abroad is ineligible; on-site/hybrid
  outside the home area is ineligible when `open_to_relocation` is off. The
  scoring cycle's bounded pre-step `geo_verify.verify_pending` resolves held
  postings: official ATS detail / page JSON-LD first, then ONE extraction
  call on the cheapest configured model only when location text exists the
  rules could not read — the quote must be verbatim IN FULL in the excerpts
  AND itself name the place claimed (`quote_supports`: name, city, US state,
  demonym); an unsupported claim is dropped, none left = `unsupported_quote`;
  no text = no call. Unresolved rows are cached with backoff (`GEO_VERIFY_*`).
  A copy of a pre-rollout posting gets no row and keeps the string gate
  (`geo.location_allowed`). **Second review (2026-09-18):** the page probe
  derives from the fetched structured fields PLUS the description, so a
  detail endpoint restating `country=US` cannot overturn "must be based in
  Germany"; a CONFLICT row is never sent to the model and is parked a year
  (`conflict_retained`; `mark_held` does not re-arm it — only changed
  evidence does). "Must be based in California" is a STATE restriction
  (`Geography.areas`, `areas_json`): held when the profile names no state,
  never widened to the country. `remote_ok=False` makes a remote role
  INELIGIBLE unless it has an office in the user's area. The legacy string
  filters (`RuleFilter`, `matcher._passes_legacy_country_gate`) SKIP their
  country check for any row with a verdict — "Remote · Tiranë, Albania ·
  Austin, TX" is eligible by the verdict and Albanian to the regex. The
  shared row carries `Job.geo_hash` (16 chars of `evidence_hash`: structured
  evidence, no text; projected by the SHARED door's prefetch only) so a Lever
  `country` moving under an unchanged string re-derives and re-decides
  (location-only write, no re-embed); NULL rows adopt a baseline on their
  next stale touch, never re-derived. `GEO_VERIFY_LLM_DAILY_CAP` is RESERVED
  per call in `platform_counter` (`app/common/daily_counter.py`; 0 = no
  calls) — the in-process counter reset on every deploy and was per replica. Guards:
  `test_geo_eligibility`, `test_geo_review_followup`.
- **Copying a posting must not make it younger**: `RawJob.first_seen` is carried
  by the COPIERS (adoption, per-user routes) and `_build_job` honours it. Before
  that, a 3-week-old shared row entered a user's pool stamped `first_seen=now` —
  labelled New, back inside the 5-day scoring window, against a promise to be
  first to apply. The pulse route did it AGAIN (35 copies, one of a posting held
  56 days), so `_upsert` now enforces it at the door: the shared upsert stamps
  the shared sighting onto the RawJob objects the per-user routes reuse, and
  `_inherit_shared_first_seen` looks it up for any door that arrives without it;
  `adoption.repair_copied_first_seen` (daily, registry maintenance, bounded)
  re-dates open copies still younger than their posting by >1 h.
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
- **Hiring context is keyed by the POSTING, not the tenant**
  (`discovery/hiring_context.py`; `JobHiringContext`/`JobLiveness` keyed by
  `(source, external_id)`, which per-user copies preserve — `adoption.py:212`).
  12 users adopting one posting write ONE context row, same rule as `JobCardRow`.
  Every value carries an `EvidenceClass`; no evidence entry = never rendered as
  fact, and a weaker claim never overwrites a stronger one (`merge_into`).
  Adapters read keys the responses ALREADY contain (zero extra HTTP);
  `apply_text_extraction` runs at ingest on the FULL description because
  retrieval only projects 800 chars. **Capture runs once per posting PER
  DESCRIPTION**, not per sighting: the pulse lane re-sees 4-5k postings a tick,
  and extracting from each sighting took `upsert_shared` p50 810→2,200ms on an
  already capacity-limited lane. `captured_state()` asks one bulk indexed
  question first and re-extracts only where `content_hash` differs, so an
  EDITED posting is re-read and an unchanged one is free. Two rules keep that
  bounded: a posting that yields nothing still gets an `examined_only` row (the
  ~1/3 with no context were being re-read forever), and the hash advances even
  when a re-read finds nothing new (or the same posting re-reads forever).
  Rows predating `content_hash` adopt the current text as their baseline
  WITHOUT re-extracting — backfilling by re-extraction is the original
  regression, all at once. A new ADAPTER (same text, new fields) needs
  `EXTRACTOR_VERSION` bumped; a new description does not. Measured on 45 live shortlisted jobs:
  64.4% department/team, 35.6% requisition id, 2.2% named recruiter, **0/45
  named manager** — hence the UI says "People & team", never "hiring manager",
  and a posting creator is never relabelled as one.
- **Liveness: only REMOVED/EXPIRED mean dead** (`discovery/liveness.py`). 429 =
  RATE_LIMITED, 403 = BLOCKED: an endpoint refusing to answer says nothing about
  the vacancy, and treating it as death would close live jobs whenever a board
  throttled the pulse lane. Free signal = absence from a COMPLETE board fetch;
  an incomplete fetch records nothing. The gate runs LATE (`strategy/delivery_gate.py`):
  `slate.place()` holds the cached in-session backstop, the scoring lane does the
  network refresh outside its session for candidates that already cleared the
  bar, single-flighted per posting and bounded by `CycleBudget`. Measured: ~55
  requests in 10.5h, p50 ~335ms. **A refusal must also CLOSE the row** — the
  first version only refused, and one dead Workday req was re-nominated and
  re-refused 17 times in 7 hours. Aggregate counters only
  (`metrics_snapshot`, drained once per scoring cycle into one log line);
  never a job id, external id or URL in a label.
- **`source` is a routing bucket; `origin` is the truth.** Both HN sources write
  `source="indeed"`, RemoteOK writes `"remotive"`, SerpAPI discarded `via`.
  `Job.origin`/`origin_provider` record the real producer without moving rows
  between buckets, so existing analytics keep their meaning. Read
  `coalesce(origin, source)`.
- **Tenure is a UNION of intervals** (`tailoring/inventory.py`,
  docs/EVIDENCE_INVENTORY.md): `_years_experience` measured `min(start) →
  max(end)` — the SPAN of a career — so a 2016 internship plus a 2024 job read as
  **10 years**, which drives `reranker`'s seniority rules, `RuleFilter.cand_years`
  and the UserCard, spending finals on Staff postings nobody will screen for.
  Summing periods is the mirror error (concurrent roles counted twice);
  `merged_months` is the ONE implementation (contiguous merges, gaps don't).
  FIVE kinds of work, not one substance: professional + freelance = experience,
  internship reported SEPARATELY, academic/personal/volunteer NEVER employment —
  a project proves a skill but is not years of a job (`project_only` vs
  `internship_only`, distinct because calling an internship "a personal project"
  is wrong). Kind = section heading, then the TITLE overrides it ("… Intern"
  under `## Experience`); an override never promotes coursework to employment.
  `requirements.py` reads wording AS WRITTEN ("three (3) years", "4-6", "2+",
  "18 months"; "up to 5" is a ceiling = no requirement) and scopes preference to
  the CLAUSE and to a "Preferred Qualifications" HEADING — both mis-scopings
  invent blocking gaps. `GET /application/{id}/review` = the pre-download review:
  supported / genuine gaps (a PREFERRED shortfall is NOT one) / open questions +
  an improvement plan where suggested projects stay until confirmed
  (`unconfirmed_project_claims` fails a draft that wrote one in as done — a class
  `fabrication_violations` can't catch, since a skill is not an employer or a
  date). **No score, no percentage, no interview chance** — a keyword match is
  not a hiring probability. Year-only dates are admitted as approximate, and only
  dates feeding a total can make a total approximate. Guards:
  `evidence_inventory`, `requirement_review`.
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
  stored hash always denotes fully-INGESTED content — a poll whose details
  failed never establishes the baseline, or recovery reads as unchanged and
  the postings are never upserted) · `spend_attribution` (the backend that
  answered is the one billed; no phantom calls) · `expiry_sweep` (per-owner,
  index-friendly, survives one owner's failure) · `pulse_adaptive` (the cap
  shrinks under deferral and grows back) · `account_purge` (the reconcile
  deletes nothing on any doubt) · `admin_observability` (no secret value in
  any admin payload) · `grounding_provider` (never fails open; names the
  verifier) · `tailor_accounting` (usage and ledger move together; one
  generation per application at a time).
- **Tests must clean up only their OWN rows.** A wholesale `delete(Job)` /
  `delete(CompanyRegistry)` takes out fixtures other files already built, which
  is a suite that fails differently every run. Prefix your rows and delete by
  that prefix; scope global-count assertions to them too.

## Maintenance
Update on major architectural changes or completed modules. Keep under ~150 lines —
prune stale info rather than appending.
