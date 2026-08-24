"""Centralized config loaded from .env."""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    tavily_api_key: str = ""
    exa_api_key: str = ""
    magic_api_key: str = ""

    # Job board APIs
    serpapi_key: str = ""            # serpapi.com — Google Jobs (LinkedIn/Indeed/Glassdoor). Free: 100/mo
    serpapi_date_posted: str = "3days"  # Google Jobs freshness window: today | 3days | week | month
    serpapi_max_keywords: int = 8    # searches fired per run (each = 1 quota unit); caps the shared-run role union
    serpapi_concurrency: int = 5     # concurrent Google Jobs searches (was sequential → 45s timeout)
    remotive_enabled: bool = True    # Remotive public API — no key needed
    remoteok_enabled: bool = True    # RemoteOK public API — no key needed
    hn_whoishiring_enabled: bool = True  # HN monthly "Who is hiring?" thread — no key, early signal
    themuse_enabled: bool = True     # The Muse public API — no key needed; optional THEMUSE_API_KEY for higher rate limit
    themuse_api_key: str = ""        # optional — raises rate limits
    arbeitnow_enabled: bool = True   # Arbeitnow public API — no key needed
    jobicy_enabled: bool = True      # Jobicy public API — no key needed
    weworkremotely_enabled: bool = True  # WeWorkRemotely RSS feeds — no key needed
    indeed_rss_enabled: bool = False  # Indeed killed public RSS + blocks bots (and ToS forbids it) — off by default
    # Keyed sources auto-activate: enabled by default, but each source still
    # skips itself when its key is missing — so "add the key → it works", and
    # setting the *_ENABLED flag to false force-disables even with a key set.
    adzuna_enabled: bool = True      # Adzuna API — needs app_id + app_key (free tier)
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    reed_enabled: bool = True        # Reed.co.uk API — needs api_key (free tier)
    reed_api_key: str = ""
    jooble_enabled: bool = True      # Jooble API — needs api_key (free tier)
    jooble_api_key: str = ""
    linkedin_rapidapi_enabled: bool = True  # LinkedIn via RapidAPI (~$10/mo) — needs rapidapi_key
    rapidapi_key: str = ""

    @property
    def linkedin_rapidapi_active(self) -> bool:
        """Active only when explicitly enabled AND a key is present, so
        LINKEDIN_RAPIDAPI_ENABLED=false reliably turns it off (e.g. while the
        RapidAPI quota is exhausted) even with a key still configured."""
        return self.linkedin_rapidapi_enabled and bool(self.rapidapi_key)
    scrape_company_boards: bool = False  # JOB-FIRST by default: discovery is driven purely by job
                                         # aggregators (SerpAPI/Remotive/RemoteOK/HN), NOT a fixed company list.
                                         # Set True to also scrape the bootstrap company ATS boards
                                         # (Greenhouse/Lever/Ashby) — those add direct-ATS autofill jobs but
                                         # re-introduce company-anchored discovery.
    max_jobs_per_source: int = 200   # Cap per source per discovery run (was 50)
    # Cap on boards a keyword-search source fetches per run. The registry now
    # holds ~56K boards; fetching all of them blows the 45s per-source timeout.
    # The direct-ATS board lanes cover the full registry, so keyword search only
    # needs the top productive boards.
    keyword_search_max_slugs: int = 250

    # GitHub harvester (optional token lifts the public API rate limit)
    github_token: str = ""

    # Admin token gating the one-off H-1B CSV upload page (empty = page disabled)
    admin_token: str = ""

    # Owner-only admin dashboard: comma-separated allow-list of emails.
    admin_emails: str = "karthikamruthaluri2002@gmail.com,karthiklucky899@gmail.com"

    # Referral program
    referral_threshold: int = 10          # friends needed to unlock the reward
    referral_reward_days: int = 30        # days of the reward plan granted
    referral_reward_plan: str = "pro"     # plan tier granted on unlock

    @property
    def admin_emails_list(self) -> List[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    # Founding-user trial: first N users get a budget of fully processed jobs
    # with all Pro features unlocked.
    trial_max_users: int = 10
    trial_job_quota: int = 100

    # Local personal dashboard
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # Paths
    data_dir: Path = Path("./data")
    resume_path: Path = Path("./data/resume_master.md")
    resume_docx_path: Path = Path("./data/resume_master.docx")
    profiles_dir: Path = Path("./data/profiles")
    faiss_index_path: Path = Path("./data/jobs.faiss")
    sqlite_path: Path = Path("./data/jobagent.db")

    # Supabase — set these to migrate from SQLite to PostgreSQL
    supabase_url: str = ""             # https://xxxx.supabase.co
    supabase_anon_key: str = ""        # public anon key (safe to expose in browser)
    supabase_service_role_key: str = ""  # service role key (server-only, never expose)
    database_url: str = ""             # postgresql://postgres:[password]@db.xxxx.supabase.co:5432/postgres

    @field_validator("supabase_url", mode="after")
    @classmethod
    def _normalize_supabase_url(cls, v: str) -> str:
        """Self-heal a scheme-less Supabase URL.

        A value like ``auth.spotapply.ai`` (pasted from the custom-domain setup
        without ``https://``) makes supabase-js throw ``Invalid supabaseUrl:
        Must be a valid HTTP or HTTPS URL`` in the browser — which aborts the
        auth script before ``handleGoogle`` is even defined, so BOTH email and
        Google sign-in silently break — and also breaks the server Storage
        client, so résumés stop loading and scoring goes to zero. Prepending the
        scheme + trimming a trailing slash makes that misconfiguration harmless.
        """
        v = (v or "").strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v

    @property
    def use_supabase(self) -> bool:
        return bool(self.database_url and self.supabase_url)

    @property
    def sqlite_url(self) -> str:
        if self.use_supabase:
            return self.database_url
        return f"sqlite:///{self.sqlite_path}"
    bootstrap_path: Path = Path("./data/bootstrap_companies.json")

    # Applicant
    applicant_first_name: str = "Karthik"
    applicant_last_name: str = ""
    applicant_email: str = ""
    applicant_phone: str = ""
    applicant_location: str = "Cincinnati, OH"
    applicant_github: str = ""
    applicant_linkedin: str = ""
    applicant_work_auth: str = ""

    # Autofill multi-tenancy guard. The Playwright filler historically sourced
    # identity (name/email/phone/EEO) from the process-global qa_resolver +
    # applicant_* defaults above — i.e. the FOUNDER's PII — for every user's form.
    # Until the per-user identity path is browser-verified, autofill runs ONLY for
    # the founder (founder_user_id) or the local dev user; a non-founder's autofill
    # is refused rather than risk submitting an application under someone else's
    # identity. Flip autofill_multi_user_enabled=True only after testing that a
    # second real account fills with ITS OWN profile data.
    autofill_multi_user_enabled: bool = False  # AUTOFILL_MULTI_USER_ENABLED
    founder_user_id: str = ""                  # FOUNDER_USER_ID — Supabase user_id backing applicant_* defaults

    # Matching
    min_match_score: float = 0.15          # lowered from 0.20 — cross-encoder floor
    top_k_rerank: int = 600               # final candidate pool size returned by retrieval
    cross_encoder_cap: int = 120          # max pairs scored by the local CPU cross-encoder (the real CPU bottleneck)
    cross_encoder_max_length: int = 256   # token cap per cross-encoder pair (shorter = far faster on CPU)
    cross_encoder_text_chars: int = 700   # chars of profile/job text fed to each cross-encoder pair
    # Reranker backend for the retrieval rerank stage. "local" = the on-CPU
    # cross-encoder (slow on Railway). "jina" = Jina Reranker API (fast, cheap).
    # Any API failure or missing key falls back to local, then to FAISS order.
    rerank_provider: str = "local"        # "local" | "jina"
    jina_api_key: str = ""                # api.jina.ai — rerank API key
    jina_rerank_model: str = "jina-reranker-v2-base-multilingual"
    llm_rerank_cap: int = 100             # max jobs sent to the FINAL (Claude) reranker per run (fresh-first order); env LLM_RERANK_CAP
    llm_rerank_workers: int = 12          # concurrent LLM scoring workers (tune to Anthropic tier)
    # ── Two-tier scoring cascade ──────────────────────────────────────────────
    # Tier 1: a cheap/fast model (default GPT-4o-mini) bulk-scores many candidates
    # per pass; only those clearing prescore_advance_threshold go to Tier 2
    # (Claude, the authoritative score that drives shortlisting). Jobs Tier 1
    # clearly rejects are stamped with their prescore so they leave the unscored
    # corpus — this drains the backlog (fewer repeated full-row reads = less
    # egress) and lets far more than llm_rerank_cap jobs be looked at per pass.
    prescore_enabled: bool = True         # PRESCORE_ENABLED — turn the cascade on/off (off = old single-tier behavior)
    prescore_provider: str = "openai"     # PRESCORE_PROVIDER — "openai" | "anthropic" (Tier-1 bulk scorer; falls back to whatever key exists)
    prescore_model: str = "gpt-4o-mini"   # PRESCORE_MODEL — cheap/fast Tier-1 model
    prescore_cap: int = 600               # PRESCORE_CAP — max candidates Tier-1 scores per pass (fresh-first)
    prescore_advance_threshold: int = 40  # PRESCORE_ADVANCE_THRESHOLD — Tier-1 fit >= this advances to Claude; below is stamped and drained. Effective gate = min(this, shortlist_score_threshold). Set to 40 IN LOCKSTEP with the banded Tier-1 prompt (2026-08-04 audit): the new prompt scores adjacent-role jobs 40-59 where the old one scored them 60+, so a 60 gate would convert the old false HIGHS into permanent false LOWS — a live 200-job eval caught a Claude-72 job prescoring 25, and three 62-final jobs at 30-40. 40 = the bottom of the adjacent band: every adjacent fit gets the authoritative look, only stated-blocker/wrong-profession jobs (<40) drain. Re-derive from a fresh N~2000 gate eval (scripts/eval_scorers.py --mode gate) after the new prompt has traffic; do NOT raise back toward 60 without that data. History: 30 -> 35 -> 60 (lockstep era) -> 40 (banded prompt).
    prescore_workers: int = 16            # PRESCORE_WORKERS — concurrent Tier-1 workers (cheap model tolerates more)
    prescore_budget_multiplier: int = 10  # PRESCORE_BUDGET_MULTIPLIER — bounds the ANTHROPIC Tier-1 fallback only: a user's daily allowance = PLAN_LIMITS["finals_daily"] × this (PRO: 50 × 10 = 500). OpenAI prescores are NOT counted — at $0.0002 each a user's entire Tier-1 volume is ~$0.65/month, so capping them only converts an OpenAI outage into a feed outage. The Anthropic path is ~$0.00185 (9x) and DOES need a bound. Prescores were previously charged AS finals whenever they ran on Anthropic, which spent a PRO user's whole day 15 minutes after 00:00 UTC. 0 = unbounded per user (platform caps still apply).
    # ── Adaptive finals budget (app/matching/finals_budget.py) ───────────────
    # PLAN_LIMITS["finals_daily"] is the SOFT point, not the ceiling. Past it a
    # user keeps scoring only while the evidence says the next candidate is
    # worth it; the money is bounded by burst (one day) and weekly (the real
    # economic control: 7x soft = the same spend the flat cap already cost).
    finals_burst_multiplier: float = 2.0    # FINALS_BURST_MULTIPLIER — hard daily ceiling = soft × this (PRO: 50 → 100). A strong Monday may spend double; it can never spend more.
    finals_weekly_multiplier: float = 7.0   # FINALS_WEEKLY_MULTIPLIER — rolling 7-day ceiling = soft × this (PRO: 50 → 350). 7x soft is EXACTLY the flat cap's weekly spend, so bursting reallocates money across the week rather than adding any. Raising this is the only change that increases what a user costs.
    finals_promise_floor: int = 55          # FINALS_PROMISE_FLOOR — Test A: in the burst zone a job needs this Tier-1 prescore (not just the everyday advance gate) to be worth a final. MUST sit between prescore_advance_threshold and shortlist_score_threshold: at or below the gate it does nothing, at or above the shortlist bar it demands Tier-1 already know the answer.
    finals_yield_window: int = 10           # FINALS_YIELD_WINDOW — Test B looks at the last N finals. Too small and one unlucky run stops a good day; too large and a dead pool keeps being paid for.
    finals_yield_continue_rate: float = 0.20  # FINALS_YIELD_CONTINUE_RATE — Test B: burst stays open while >= this share of the last N finals cleared the shortlist bar. The guard against a miscalibrated Tier-1 — prescore promising 70s while Claude answers 40s stops the spend even when Test A passes.
    llm_rerank_max_retries: int = 1       # LLM_RERANK_MAX_RETRIES — in-call attempts per backend on 429/overloaded. 1 = no in-call retry: a failed job stays rerank_score-NULL and the 90s scoring lane re-queues it anyway, so in-call backoff (old default 4) only stacked sleeps and hammered exhausted quotas.
    llm_provider_cooldown_minutes: int = 30  # LLM_PROVIDER_COOLDOWN_MINUTES — circuit breaker: a provider returning credit/quota errors is skipped for this long instead of being re-hit every cycle (0 disables)
    llm_daily_final_cap: int = 5000       # LLM_DAILY_FINAL_CAP — PLATFORM BACKSTOP ONLY. Allocation now lives in PLAN_LIMITS["finals_daily"] (per user, per plan); this is just the runaway ceiling if plan lookup fails open or a lane misbehaves. Was 1500 as the allocation, which meant one global pool divided by N users — every signup thinned every existing user's feed. Sized for ~100 PRO users at their 50/day allowance; raise it as paying users grow. 0 = unlimited.
    llm_hourly_final_cap: int = 400       # LLM_HOURLY_FINAL_CAP — smoothing only: bounds how FAST a backlog can burn, not how much (that is the per-plan cap). Without it a big backlog drained at ~2K finals/hour and ate a day's budget in under an hour. Still needs >=12 clock hours to reach the daily backstop. Raised 150 -> 400 alongside the daily backstop so it does not throttle legitimate multi-user throughput. 0 = unlimited.
    scoring_fail_max_attempts: int = 3    # SCORING_FAIL_MAX_ATTEMPTS — after this many failed final-score attempts a job is deferred (sits out) instead of re-queued every 90s forever
    scoring_fail_defer_hours: float = 6.0 # SCORING_FAIL_DEFER_HOURS — how long a repeatedly-failing job sits out before it may be retried
    # ── DB connection pool (Postgres/Supabase only) ───────────────────────────
    # Sized for the background lanes + web traffic. Scoring workers no longer
    # hold a connection during LLM calls (they open short sessions before/after),
    # so the pool does NOT need to scale with scoring_workers — but it does need
    # headroom over the old 5+10, which starved funnel/registry/web requests
    # ("QueuePool limit ... reached" errors) whenever the lanes overlapped.
    db_pool_size: int = 10                # DB_POOL_SIZE
    db_max_overflow: int = 20             # DB_MAX_OVERFLOW
    # ── Distilled local scorer (see docs/DISTILLATION.md) ─────────────────────
    # A small cross-encoder fine-tuned on this deployment's own LLM scores.
    # Until a trained model exists at local_scorer_path everything no-ops.
    # Shadow mode runs it NEXT TO LLM finals and records agreement as
    # FunnelEvents (stage="shadow_score") — flip to local-first only after
    # scripts/shadow_report.py shows the agreement you're comfortable with.
    local_scorer_path: str = "data/models/hirepath-scorer"  # LOCAL_SCORER_PATH
    local_scorer_shadow: bool = True      # LOCAL_SCORER_SHADOW
    # When NO LLM provider is usable (no API keys configured, or every provider
    # is in circuit-breaker cooldown after credit/billing errors), keep scoring
    # on free local models instead of stalling the whole funnel: the distilled
    # scorer if trained, else the retrieval cross-encoder with a piecewise
    # calibration. Scores are labeled as local estimates in the reasoning.
    # Set to 0 to restore the old wait-for-a-provider behavior.
    local_score_fallback: bool = True     # LOCAL_SCORE_FALLBACK
    # ── CardRace v2 (docs/CARDRACE_DESIGN.md) ─────────────────────────────────
    # Understand-once matching: JobCard per distinct posting (shared by every
    # tenant) x UserCard per user -> deterministic g() pair arithmetic; Claude
    # only for the certified-uncertain band. Rollout is two independent flags:
    #   card_match_shadow  — Phase 3: mint cards + score g() BESIDE every real
    #     Claude final and record agreement (card_match_shadow table). Zero
    #     effect on user-visible decisions; extra spend ~= one ~$0.005 card
    #     mint per Claude-scored job, bounded by card_mint_daily_cap.
    #   card_match_enabled — Phase 4 cutover: g() + conformal bands become
    #     authoritative and Claude becomes the band escalator. NEVER flip this
    #     before scripts/build_calibration.py has fitted a calibration AND its
    #     holdout gates pass (docs/CARDRACE_DESIGN.md §3.4) — without a
    #     calibration file every pair is BAND (= Claude decides) by design.
    card_match_enabled: bool = False      # CARD_MATCH_ENABLED
    card_match_shadow: bool = True        # CARD_MATCH_SHADOW
    card_mint_model: str = "claude-haiku-4-5-20251001"  # CARD_MINT_MODEL
    card_mint_daily_cap: int = 300        # CARD_MINT_DAILY_CAP — mints/day backstop (~$1.5/day max)
    card_graph_enabled: bool = True       # CARD_GRAPH_ENABLED — skill-graph inference in g()
    card_graph_path: str = "data/skill_graph.json"       # CARD_GRAPH_PATH
    # Semantic skill route: cosine between a JobCard want and a UserCard v2
    # résumé claim, over the MiniLM already resident for FAISS retrieval.
    # OFF: measured on the real model, the cosine ranks negated and adjacent
    # claims ABOVE genuine proof ("was mentored by senior engineers" 0.814 vs a
    # real vLLM/CUDA claim 0.329), so no threshold separates them. Do not turn
    # this on until the comparison is asymmetric (entailment, not similarity)
    # and re-measured — docs/CARDRACE_DESIGN.md §9.2.4.
    card_embed_enabled: bool = False      # CARD_EMBED_ENABLED
    card_calibration_path: str = "data/calibration.json" # CARD_CALIBRATION_PATH — written by scripts/build_calibration.py
    card_max_auto_in_spread: float = 12.0 # CARD_MAX_AUTO_IN_SPREAD — wider direct-vs-expanded spread never auto-admits
    # ── Payments (Stripe + manual bank transfer) ──────────────────────────────
    # All empty by default = payments OFF: every user resolves to PRO free of
    # charge (pre-revenue mode). After the LLC + Stripe account exist, set the
    # three STRIPE_* vars and real checkout/webhook plan enforcement turns on —
    # no code change needed. PAYMENT_BANK_DETAILS enables a manual path in the
    # meantime (shown on the upgrade screen; activate via admin set-plan).
    stripe_secret_key: str = ""           # STRIPE_SECRET_KEY
    stripe_price_id_pro: str = ""         # STRIPE_PRICE_ID_PRO — $10/mo recurring Price id
    stripe_webhook_secret: str = ""       # STRIPE_WEBHOOK_SECRET — signs /api/billing/webhook
    payment_bank_details: str = ""        # PAYMENT_BANK_DETAILS — bank-transfer/UPI instructions (multi-line ok)
    # A branded address on customer-facing surfaces (receipts, billing help) —
    # a personal gmail on a payment screen reads as a scam.
    payment_contact_email: str = "support@spotapply.ai"  # PAYMENT_CONTACT_EMAIL
    # Turning payments ON is a cliff: no existing user has a user_subscription row,
    # so the instant the STRIPE_* vars are set they ALL drop PRO → FREE (50 → 15
    # finals/day, unlimited → 5 tailors/day, unlimited → 2 autofills/week) with no
    # warning. Set this to an ISO date (e.g. "2026-08-01") to keep everyone who
    # signed up before then on PRO. Empty (default) = that cliff, unchanged.
    plan_grandfather_until: str = ""       # PLAN_GRANDFATHER_UNTIL
    # How long a TAILORED / autofill-review application keeps its place on the
    # board. Longer than SHORTLIST_MAX_AGE_DAYS because the user put work into
    # it, but NOT unlimited: these used to be exempt outright, and the board
    # accumulated 25 tailored applications aged 32-52 days that buried the
    # current week's matches under postings that are certainly filled by now.
    # 0 restores the old never-hide behaviour.
    tailored_max_age_days: int = 14        # TAILORED_MAX_AGE_DAYS
    # Recruiter verification unlocks /api/recruiter/search, which returns every
    # pooled candidate's full name, work authorization and sponsorship need. The
    # old auto-verify compared work_email's domain to company_domain — but BOTH
    # arrive in the same request body, so a match proved only that the caller typed
    # two consistent strings, never that they control the mailbox. Off by default:
    # a domain match is recorded as a signal and an admin promotes
    # (POST /api/admin/recruiter/verify). Turn on only once email ownership is
    # actually proven (verification link / SSO).
    recruiter_autoverify_on_domain_match: bool = False
    llm_request_timeout: float = 45.0     # per-request LLM timeout (s). Bounds a matching pass so a slow API can't freeze it while it holds the matching lock. SDK default is 600s.
    max_liveness_checks_per_run: int = 25 # cap on serial link-liveness network calls per matching pass (each ~2.5s, lock-held) so one pass can't starve other lanes
    matching_lane_interval_minutes: int = 5  # INDEPENDENT matching loop cadence (env MATCHING_LANE_INTERVAL_MINUTES; 0 disables). Decouples scoring from discovery so a stalled discovery can't starve matching.
    matching_catchup_passes: int = 4     # max scoring passes per user per lane tick when a large backlog exists (env MATCHING_CATCHUP_PASSES; 1 = old behavior). Drains a post-incident unscored backlog faster; bounded by a wall-clock budget.
    matching_catchup_backlog: int = 200  # only run extra catch-up passes while a user's unscored backlog exceeds this (env MATCHING_CATCHUP_BACKLOG)
    # ── Scoring lane (decoupled, parallel, cross-user) ────────────────────────
    # Drains the GLOBAL queue of unscored on-role jobs across ALL users at once
    # with a bounded pool of LLM workers, so scoring throughput depends on the
    # LLM rate limit — NOT on the number of users. Replaces the old O(users)
    # serial per-user matching loop as the primary "get fresh jobs scored fast"
    # engine. Lock-free (cheap gates + GPT->Claude cascade, no FAISS), so it runs
    # continuously alongside discovery. The 5-min matching lane stays as the
    # FAISS-retrieval + reshortlist + self-heal backstop.
    scoring_lane_enabled: bool = True      # SCORING_LANE_ENABLED
    scoring_lane_interval_seconds: int = 90  # cadence; 0 disables
    scoring_workers: int = 20              # GLOBAL concurrent LLM scoring workers (size to your Anthropic/OpenAI rate limit, not user count)
    scoring_per_user_cap: int = 40         # max queued jobs scored per user per cycle (fresh-first)
    scoring_global_cap: int = 200          # max total jobs scored per cycle (bounds cost + wall-clock; also bounds how many prescores can overshoot when the finals budget trips mid-cycle)
    scoring_drain_cap: int = 25            # SCORING_DRAIN_CAP — Tier-1-only slice per user per cycle AFTER their finals allowance hits 0. Production Aug 2026: with one PRO user the lane bought ~70 finals/day and then, because a user with no allowance was dropped from the cycle ENTIRELY, ran zero Tier-1 prescores for the rest of the day — the queue slice was keyed to remaining finals, so exhausting the finals budget also halted the ~$0.0002 OpenAI drain, the backlog never shrank, and the scored feed aged to a 34.8-day median. This slice keeps draining: never-prescored queue items are Tier-1'd, misfits (< the drain gate) are stamped out for good, and everything else stays Queued WITH its prescore so tomorrow's finals budget opens on the best-known candidates instead of arrival order. Never buys a final; skipped when the Anthropic prescore allowance is what ran out (that path IS metered). 0 disables (the old behavior).
    scoring_lane_max_seconds: int = 120    # hard wall-clock cap per cycle
    # ── Dual-provider final scoring (Option A) ────────────────────────────────
    # The prescore→final cascade is a RELAY (GPT drains misfits, then Claude
    # scores the survivors) — one job flows GPT→Claude, so the two can't be
    # "split" on the same job. To lift the single-provider rate-limit ceiling we
    # instead split the FINAL score across providers by JOB: ~claude_share of
    # jobs get Claude's authoritative score, the rest go to GPT-4o — in parallel.
    # BOTH score against the SAME rubric (_get_system_prompt + _SCORE_BANDS), so
    # the numbers are comparable; a small calibration offset nudges GPT's scale
    # onto Claude's if it clusters low/high. Only active when BOTH provider keys
    # exist; with one key it's a no-op (the single provider scores everything).
    dual_score_enabled: bool = False        # DUAL_SCORE_ENABLED — split final scoring across Claude + GPT. OFF by default: the GPT final is the full gpt-4o (~2.5x Haiku's price) and in practice OpenAI rate limits spilled most of its share back onto Haiku anyway — so dual mode mostly added cost, not throughput. Enable only with an OpenAI tier that can actually absorb its share.
    dual_score_claude_share: float = 0.6    # DUAL_SCORE_CLAUDE_SHARE — fraction of finals routed to Claude (rest to GPT)
    dual_score_openai_model: str = "gpt-4o" # DUAL_SCORE_OPENAI_MODEL — the GPT FINAL scorer (full model, not mini, so it's comparable to Claude). Set to gpt-4o-mini to cut cost at some accuracy loss.
    dual_score_openai_offset: float = 0.0   # DUAL_SCORE_OPENAI_OFFSET — calibration added to GPT scores to align with Claude's scale (e.g. +5 if GPT clusters ~5 points low). Clamped to 0-100.

    # ── Observability (all dormant until set — safe to ship empty) ─────────────
    sentry_dsn: str = ""                   # SENTRY_DSN — enables error tracking when set
    sentry_environment: str = "production" # SENTRY_ENVIRONMENT
    sentry_traces_sample_rate: float = 0.0 # SENTRY_TRACES_SAMPLE_RATE (0 = errors only, cheapest)
    heartbeat_matching_url: str = ""       # HEARTBEAT_MATCHING_URL — healthchecks.io ping URL for the matching lane
    daily_apply_limit: int = 25          # cap on actual auto-submissions per day (autofill)
    daily_shortlist_limit: int = 200     # cap on how many jobs get shortlisted onto the board per day

    # ── Degraded mode (LLM credits exhausted / all providers cooling down) ────
    # The local cross-encoder keeps the funnel moving, but its judgement is
    # weaker than Claude's, and at the normal 60 bar it filled the board with
    # mediocre matches. Hold local-only scores to a much higher bar: fewer
    # jobs, but still worth the user's attention. Every one is marked
    # provisional and re-scored for real once credits return.
    degraded_shortlist_threshold: int = 75   # DEGRADED_SHORTLIST_THRESHOLD
    # Look-back for the recovery recheck: the OUTAGE's own duration, hard-capped
    # here. A 2-hour gap rechecks 2 hours; a week-long gap still only rechecks
    # 2 days, because older postings are stale anyway.
    degraded_recheck_max_hours: int = 48     # DEGRADED_RECHECK_MAX_HOURS
    degraded_recheck_max_jobs: int = 20      # per user, per recovery — DEGRADED_RECHECK_MAX_JOBS
    shortlist_score_threshold: int = 60  # SHORTLIST_SCORE_THRESHOLD — min LLM rerank score (0-100) to shortlist. Raised 35 -> 60 on production evidence: of 57,309 real Claude finals, 44.5% cleared 35 but only 11.6% cleared 65 — so the old bar shortlisted ~1,800 jobs PER USER, which nobody applies to. Those 35-64 rows were pure cost: the board's default filter is shortlist_strong_threshold=65, so they were created, given a company-cap slot, and then hidden. 60 sits just under that filter so a narrow tail is still one slider-drag away. This ALSO clamps the Tier-1 advance gate (see prescore_advance_threshold) — the two must be raised together.
    shortlist_strong_threshold: int = 65 # SHORTLIST_STRONG_THRESHOLD — default min-score filter on the board so it OPENS on strong fits; the 35-64 tail stays shortlisted and is one slider-drag away
    fresh_alert_min_score: int = 65      # FRESH_ALERT_MIN_SCORE — min fit (rerank or blended) to push a "Fresh match" notification. Shortlisting stays at shortlist_score_threshold; this only gates the apply-now alert, so users aren't pushed to jobs the model itself calls a weak fit.
    fresh_alert_daily_cap: int = 10      # FRESH_ALERT_DAILY_CAP — max fresh-match notifications per user per UTC day (lanes fire every few minutes; the per-pass cap alone allows dozens/day). 0 disables the cap.
    lanes_enabled: bool = True           # LANES_ENABLED — set 0 on extra web replicas: every lane lock, LLM budget counter, and in-flight claim is process-local, so a second lane-running process silently DOUBLES scraping, LLM spend, and alerts. Exactly ONE process should run lanes.
    tailor_abuse_daily_cap: int = 25     # TAILOR_ABUSE_DAILY_CAP — per-user hard ceiling on tailors/day that applies even to "unlimited" plans. This is the ABUSE backstop, not the product limit: the per-plan number (PLAN_LIMITS[...]["tailor_daily"], 12 on PRO) is what a normal user meets first. Tailoring runs on tailoring_model (Haiku 4.5, ~$0.025-0.05 per tailor incl. the cover letter) and is deliberately OUTSIDE the adaptive finals budget, so this is the only thing bounding it: 25/day is ~$1.25/user/day worst case. Docs that still say "Sonnet" or "150" are stale (docs/CAPACITY.md predates both changes). 0 disables.
    dormant_user_grace_days: int = 21    # DORMANT_USER_GRACE_DAYS — users with no authenticated request for this many days are skipped by adoption/matching/scoring/alerts (their pool stops refilling, so no LLM money is spent on them); the next visit re-activates them within one lane tick. Profiles that predate activity tracking (last_active_at NULL) are grandfathered as active. 0 disables the gate.
    shortlist_render_cap: int = 200      # max shortlist cards rendered on the dashboard. Was 100, which HID jobs: with 161 shortlisted the board showed 100 while the header/live count said 161, so 61 jobs could never appear and the "new matches" banner looped forever. 200 covers a full day's shortlisting (daily_shortlist_limit); above it the "showing X of Y" note kicks in.
    shortlist_max_age_days: int = 5      # SHORTLIST_MAX_AGE_DAYS — the KNOWN-age render/prune bound: a job we have held this long without it being tailored/applied leaves the board (→ SKIPPED, freeing the per-company slot). Founder-set to 5 in lockstep with SCORING_MAX_JOB_AGE_DAYS=5: score nothing we have held longer than 5d, show nothing we have held longer than 5d. Measured from coalesce(first_seen, discovered_at); the source's own date is bounded separately by SHORTLIST_MAX_POSTED_AGE_DAYS. 0 disables the prune.
    # JOB_DESCRIPTION_STRIP_AGE_DAYS — blank the JD text on jobs older than this
    # that nobody applied to and that carry no real score. `description` is ~5.8 KB
    # a row and was 3.3 GB of a 5.76 GB table (over half the disk); the funnel is
    # fresh-only at 5 days, so this text is never read again. The ROW is kept, so
    # dedupe still stops discovery re-inserting and re-scoring the posting. Set
    # well above scoring_max_job_age_days so nothing in flight is touched.
    # 0 disables.
    job_description_strip_age_days: int = 14
    job_purge_max_age_days: int = 60     # JOB_PURGE_MAX_AGE_DAYS — hard-DELETE closed jobs older than this that have no Application attached, so the job table (and every scan's DB egress) stays bounded. Applied jobs are never deleted. 0 disables.
    user_job_close_age_days: int = 45    # USER_JOB_CLOSE_AGE_DAYS — age-close OPEN per-user job rows older than this that have no Application (a 45-day-old posting is filled/ghost; SHORTLIST_MAX_AGE_DAYS is 5). Shared-pool rows have their own 45d close; per-user rows previously NEVER closed by age, so the table grew forever (the 3.8 GB job-table finding). Closed rows are then purged by JOB_PURGE_MAX_AGE_DAYS. 0 disables.
    scoring_max_job_age_days: int = 5    # SCORING_MAX_JOB_AGE_DAYS — the KNOWN-age bound: an unscored job that has sat in the queue this long is stamped out of it (score 8, expired_at set) instead of paying prescores/finals. 'Be first to apply' is the product, so a posting we have held unscored for days has missed its moment. Measured from coalesce(first_seen, discovered_at) — NOT from posted_at; see scoring_max_posted_age_days and app/common/freshness.py for why the two are separate. One indexed UPDATE per cycle replaces thousands of LLM calls during a backlog drain. 0 disables.
    # SCORING_MAX_POSTED_AGE_DAYS — the POSTED-age bound, and it is deliberately
    # far looser than the known-age one. It exists ONLY to suppress genuinely
    # ancient and evergreen listings, never to second-guess a fresh discovery.
    #
    # These used to be one number: age was coalesce(posted_at, first_seen,
    # discovered_at) against 5 days, so posted_at won and a job discovered TODAY
    # expired instantly if a source claimed it went up six days ago. Production:
    # 82.9% of the 8.0 expiry stamps were already >=5d old at first discovery,
    # 36.7% of intake is >7d old the first time we see it, and 11 of 13 users had
    # been stamped down to ZERO unscored jobs — invisible to _scorable_user_ids.
    # ATS posted_at is not reliable enough to carry that: Greenhouse's updated_at
    # moves on edits, aggregators stamp their own crawl date, evergreen reqs are
    # re-dated, some feeds emit future dates.
    #
    # Widening this does NOT raise spend: per-cycle finals and prescores are
    # bounded by the adaptive budget and scoring_*_cap, so a bigger eligible
    # queue changes WHICH jobs the fixed budget buys, not how much it costs.
    # Keep <= shortlist_max_posted_age_days (tests/test_settings_defaults.py).
    scoring_max_posted_age_days: int = 30
    shortlist_max_posted_age_days: int = 30  # SHORTLIST_MAX_POSTED_AGE_DAYS — the render/prune counterpart of the above. The board shows a job while BOTH bounds hold: we found it within shortlist_max_age_days AND the source's date is within this. Must stay >= scoring_max_posted_age_days or we pay for finals the board then hides.
    company_cap: int = 3                 # max active applications per company at once (focused, low spray-risk)
    # When a company is at the cap and a NEW job scores clearly higher than a
    # cap-holding application that is still just SHORTLISTED (untouched — not
    # tailored/submitted), the weaker shortlist entry is displaced (→ SKIPPED)
    # so the stronger role takes the slot. Applications the user or agent has
    # invested effort in (TAILORED and beyond) are NEVER displaced.
    company_cap_displace_enabled: bool = True  # COMPANY_CAP_DISPLACE_ENABLED
    company_cap_displace_margin: int = 5       # COMPANY_CAP_DISPLACE_MARGIN — new job must beat the weakest shortlisted holder by at least this many points (hysteresis against churn)
    discovery_cooldown_hours: int = 24    # min hours between manual discovery runs (saves API calls + tokens)
    discovery_interval_hours: int = 6     # scheduler cadence for automatic discovery+matching per user
    # Onboarding: after a new user's résumé/roles land we first fill their board
    # from the shared pool (instant DB copy). But the shared pool skews toward the
    # roles existing users already search (historically AI/ML), so a user from a
    # different domain (mechanical, finance, nursing…) adopts almost nothing and
    # sees an empty feed. When adoption leaves them under this many on-role jobs,
    # kick a targeted scrape of THEIR roles right away instead of waiting for the
    # next 6h global pass — their domain fills within minutes. 0 disables the scrape.
    onboarding_active_discovery: bool = True  # ONBOARDING_ACTIVE_DISCOVERY
    onboarding_min_jobs: int = 25             # ONBOARDING_MIN_JOBS — adopt-count floor below which onboarding actively scrapes the user's domain

    # When a user's target roles change (new résumé or a manual edit), the pool
    # is re-pointed at the new roles: on-role jobs lose their old-résumé score
    # so the scoring lane re-judges them, off-role jobs are parked unscored.
    # This caps how many re-scores one change may queue — the lane still paces
    # the spend by the per-plan daily finals cap, this just bounds the backlog.
    realign_max_rescore: int = 500            # REALIGN_MAX_RESCORE
    # Re-scoring on a role change is limited to jobs first seen within this many
    # days AND currently shortlisted — the board the user is actually looking at.
    # 0 disables the age limit (still shortlisted-only).
    realign_rescore_days: int = 2             # REALIGN_RESCORE_DAYS
    # "My roles" relevance filter (All Jobs). Title matching alone misses jobs
    # whose title is worded differently but is the same work ("Applied Scientist"
    # ≈ "ML Engineer"). We DON'T need a separate semantic cache for this — the AI
    # fit score already IS the semantic signal (it scores the job against the
    # résumé, not the title), so a differently-titled-but-relevant job scores high.
    # The filter keeps a job when its title matches a role OR it scored at/above
    # this fit floor. 0 = title-only (no semantic catch).
    roles_filter_score_floor: int = 50        # ROLES_FILTER_SCORE_FLOOR
    # Semantic adoption. When copying shared-pool jobs into a user's pool, the
    # title gate (role_title_match) can miss "same work, different title" postings
    # (an "Applied Scientist" that's really ML work). This adds a second pass: keep
    # every title match PLUS the closest résumé-neighbours by embedding cosine
    # (reuses the local MiniLM model — no API cost), so relevant postings are
    # copied and scored instead of filtered out before scoring. Bounded by the
    # adoption cap, with a title-only fallback if embeddings are unavailable.
    adoption_semantic_enabled: bool = True     # ADOPTION_SEMANTIC_ENABLED
    adoption_semantic_threshold: float = 0.30  # ADOPTION_SEMANTIC_THRESHOLD — min résumé↔job cosine for a non-title-match to be adopted
    adoption_semantic_max_candidates: int = 1500  # ADOPTION_SEMANTIC_MAX_CANDIDATES — cap on non-title jobs embedded per pass (CPU bound)
    adoption_semantic_max_extras: int = 50     # ADOPTION_SEMANTIC_MAX_EXTRAS — cap on off-title neighbours ADOPTED per user per pass. Every adopted row is a new rerank_score-NULL job the LLM scorer must pay for; adoption passes repeat every few hours, so a small per-pass budget still surfaces the same jobs — just spread out. 0 = title-only.
    direct_ats_enabled: bool = True       # scrape active CompanyRegistry boards directly (live jobs, direct links)
    max_boards_per_run: int = 400         # cap on registry boards scraped per discovery run. Higher covers the ~56K registry faster but holds more jobs in memory per run; 400 balances coverage vs. the container memory limit (800 contributed to an OOM crash).
    # Wall-clock cap on the board phase (fetch + per-board DB work) of a
    # discovery run. Without it a run held the global discovery lock for 3+
    # hours and starved the hot lane. Deferred boards go first next run.
    board_phase_budget_minutes: int = 30
    # Fresh lane: boards-only rescan every N hours (0 disables). Applying within
    # 24-72h of posting measurably lifts response rates, so registry boards are
    # rescanned far more often than full discovery runs.
    fresh_lane_interval_hours: int = 2
    # Hot lane: poll the most productive boards every N minutes (0 disables) so
    # brand-new postings reach shortlists within minutes. Fetches each board
    # once and distributes to matching users (cost = O(boards), not O(boards×
    # users)). This is what makes "fresh within minutes of posting" real.
    hot_lane_interval_minutes: int = 20
    hot_lane_max_boards: int = 400
    # ── Pulse lane (freshness guarantee) ─────────────────────────────────────
    # Replaces the hot lane's rotating 400-board batches with a per-board
    # next_poll_at schedule: watchlist companies + boards that recently posted
    # are polled every PULSE_FAST_INTERVAL_MINUTES; every other LIVE board is
    # swept at least every PULSE_FLOOR_INTERVAL_MINUTES (the "within the hour"
    # promise); boards that 404 / never held a job decay to a daily retry. New
    # jobs take a per-job fast path (role match → cascade score → alert) instead
    # of waiting for the next batch matching tick. When enabled, the legacy hot
    # lane loop is not started (the fresh/full lanes stay as safety nets).
    pulse_lane_enabled: bool = True        # PULSE_LANE_ENABLED
    pulse_fast_interval_minutes: int = 5   # watchlist + recently-active boards
    pulse_floor_interval_minutes: int = 60 # every live board at least this often
    # PULSE_DEAD_INTERVAL_HOURS — retry cadence for ZERO-YIELD boards only:
    # active, not watched, no posting in pulse_active_days, and job_count == 0.
    # A board with job_count > 0 never reaches this branch of _cadence.
    #
    # Raised 24 -> 72 on measured evidence, not intuition. The census
    # (scripts/zero_yield_boards.py) found 31,219 of 52,698 active boards —
    # 59.2% — holding zero jobs, 30,822 of them for over 30 days, and ALL of
    # them fetching successfully (never_fetched = 0). They were consuming ~59.9%
    # of the lane's real completed-fetch capacity to re-confirm emptiness on a
    # daily cadence, while live boards sat at an ~8.7h effective revisit against
    # a 60-minute promise.
    #
    # 72h rather than 7d because these boards are not proven dead, only quiet:
    # a company that opens its first req should still be found within days, and
    # anything that posts jumps straight back to the 5-minute fast lane on its
    # next poll (last_new_job_at moves). Retirement stays off the table.
    pulse_dead_interval_hours: int = 72
    # PULSE_FAILURE_BACKOFF_CAP_HOURS — ceiling on the exponential backoff a
    # board gets after a REAL fetch failure. Split out from
    # pulse_dead_interval_hours, which it used to borrow: raising the zero-yield
    # cadence would otherwise have tripled the failure ceiling for every board
    # INCLUDING productive ones, which is a live-board cadence change smuggled
    # in behind a dead-board setting. Two meanings, two knobs.
    pulse_failure_backoff_cap_hours: int = 24
    pulse_active_days: int = 7             # "recently posted" = new job within N days
    pulse_tick_seconds: int = 60           # scheduler tick
    pulse_tick_max_seconds: int = 150      # HARD wall-clock cap per tick. The tick stops taking new work past this and reschedules the rest — so it always releases the lock promptly (a tick that ran serial LLM scoring for 20+ min once froze the whole lane). Keep < tick_seconds*3.
    pulse_max_boards_per_tick: int = 300   # PULSE_MAX_BOARDS_PER_TICK — hard cap per tick, and THE capacity lever for the hourly-floor promise. Sizing arithmetic (do it whenever live_boards grows): demand/hour ≈ live_boards × (60/floor_interval_minutes) + fast_boards × (60/fast_interval_minutes); capacity/hour ≈ this × (3600/tick_seconds), cut further whenever ticks overrun toward pulse_tick_max_seconds. The "~150 boards/min" note this comment used to carry dated from a ~9k-board registry; at 21,505 live boards the floor alone demands ~358 boards/min against a real capacity of ~120-300/min — which is how production reached 18,773 boards (87%) past the floor while the UI said "catching up". The dashboard now reports floor_holding/overdue_pct honestly; raising this (network fetches only, no LLM cost — but watch container CPU/RSS per docs/MEMORY.md) or lengthening the floor for low-yield boards are the two ways to make the promise true again.
    pulse_fetch_workers: int = 24          # concurrent board fetches per tick
    # PULSE_DEFERRED_RETRY_MINUTES — how soon a board the tick never actually
    # fetched comes back. A deferred board is NOT a polled board: it ran out of
    # tick capacity, nothing was learned about it, and advancing it by the full
    # cadence (what the code used to do) was the single biggest source of false
    # telemetry in the lane — ~88% of selected boards were deferred per tick,
    # every one of them stamped with a fresh next_poll_at as though it had been
    # checked. next_poll_at looked current, overdue_boards looked small, and the
    # dashboard reported a floor that was not being kept.
    #
    # Short, but NOT a hot retry loop: boards are submitted and collected in
    # selection order, so the deferred set is the TAIL of the batch — coming back
    # in ~2 ticks puts it at the head of the next selection, which is exactly the
    # rotation the floor needs. Jitter (derived from the board id, so it is
    # stable per board) spreads the returning tail instead of re-clumping it.
    pulse_deferred_retry_minutes: int = 2
    pulse_deferred_retry_jitter_seconds: int = 45
    # PULSE_FAILURE_BACKOFF_MINUTES — base for the exponential backoff applied to
    # a board whose fetch genuinely FAILED (as opposed to never running). Delay
    # is base * 2**(failure_count-1), capped at pulse_dead_interval_hours, so a
    # host having a bad afternoon stops consuming a slot every cadence while the
    # first failure still retries promptly. _mark_polled still counts the failure
    # and retires the board on a 404 or at BOARD_DEACTIVATE_AFTER_FAILURES.
    pulse_failure_backoff_minutes: int = 15
    pulse_fast_path_score_cap: int = 10    # max brand-new jobs LLM-scored per tick via the fast path (kept small so the tick stays short; the rest are scored by the 5-min matching lane)
    # Fraction of each hot-lane cycle spent bootstrapping never-polled boards.
    # The rest goes to proven yielders + productive boards. Kept low so tens of
    # thousands of dead seeded slugs can't eat the budget (they 404 and get
    # retired) and starve boards that actually post — env HOT_LANE_BOOTSTRAP_FRAC.
    hot_lane_bootstrap_frac: float = 0.2
    # Bulk registry seed from the open ats-scrapers slug dataset (~20K companies
    # across Greenhouse/Lever/Ashby/SmartRecruiters/Workable/Recruitee/Personio).
    open_dataset_seed_enabled: bool = True
    open_slug_dataset_base: str = "https://raw.githubusercontent.com/kalil0321/ats-scrapers/main/ats-companies"
    verify_links_on_shortlist: bool = True  # HEAD-check non-ATS links before they take a shortlist slot

    # Models
    scoring_model: str = "claude-haiku-4-5-20251001"
    # Résumé tailoring reorders and re-words existing bullets against a JD — it
    # does not write new content (grounding forbids that), so it does not need a
    # frontier model. Haiku does the keyword/emphasis work at a fraction of the
    # cost, which matters because tailoring is per-application. Set
    # TAILORING_MODEL=claude-sonnet-4-6 to go back to Sonnet.
    tailoring_model: str = "claude-haiku-4-5-20251001"
    cover_letter_model: str = "claude-haiku-4-5-20251001"  # cover letter — Haiku saves ~$0.012/app
    doctor_model: str = "claude-haiku-4-5-20251001"   # resume doctor quality check

    # Thresholds & Constraints
    min_embedding_score: float = 0.28    # lowered from 0.35 — was too aggressive
    qa_confidence_threshold: float = 0.7
    grounding_similarity_threshold: float = 0.5
    # What to do when the grounding check cannot RUN at all (grounding.py imports
    # sentence_transformers at module level, so a broken/absent ML stack or an
    # unreachable model download raises before any bullet is examined).
    #   False (default) — deliver the résumé, but report grounding_status as
    #     "unverified" rather than "passed" and put a warning on the application.
    #     The human review + Submit step is the backstop. Chosen as the default
    #     because failing closed here turns a transient ML hiccup into a total
    #     outage of the core tailoring feature.
    #   True — treat "could not verify" as a failure and block at ERROR.
    # Either way the check never reports "passed" for a résumé it did not read.
    grounding_required: bool = True   # GROUNDING_REQUIRED — a tailored résumé whose grounding check did not PASS is not delivered as verified. Default flipped to True before opening the app to real users: with it off, a broken ML stack meant the check threw, the résumé shipped as TAILORED anyway, and only a log line said otherwise. Anti-hallucination is the product's core promise; failing closed costs a retry, failing open costs a fabricated bullet in someone's real application.

    # score_hire_probability is a hot, in-DB step (called per reranked job while a
    # pooled DB connection + the matching lock are held). Its GitHub/Crunchbase
    # lookups fire blocking, UNCACHED HTTP per job — blowing GitHub's rate limit
    # after ~30 jobs and holding the connection across network I/O. OFF by default
    # so the function honors its documented "no HTTP" contract; only enable once
    # the calls are moved out-of-band and per-company cached.
    hire_probability_external_http: bool = False  # HIRE_PROBABILITY_EXTERNAL_HTTP

    ghost_score_threshold: float = 0.6   # jobs at or above this score are skipped as likely ghost postings

    # Submission Delays & Limits
    submission_jitter_min: float = 180.0
    submission_jitter_max: float = 480.0
    headless: bool = True

    # Browser memory guard. Every Playwright launch (autofill, preview, JD page
    # scrape, search-engine source) happens in the SAME container as torch +
    # the models + every lane, and each headless Chromium is a ~300-500MB child
    # process charged to the container's memory limit. Unbounded, three
    # concurrent launches are an OOM kill on their own. 1 = strictly serialized;
    # raise ONLY after raising the container's memory limit to match.
    browser_max_concurrency: int = 1      # BROWSER_MAX_CONCURRENCY
    browser_slot_wait_seconds: float = 120.0  # BROWSER_SLOT_WAIT_SECONDS — waiters give up (clear error) instead of queueing behind a hung browser

    # Browser SERVICE (browser-service/). When browser_service_url is set, the
    # three stateless render/search paths (JD page scrape, Google discovery,
    # search-engine source) run in a separate container and Chromium's ~400MB
    # never enters this one. Unset = unchanged local behaviour behind the gate
    # above. Autofill/preview deliberately stay local: they are stateful,
    # interactive sessions, and server-side autofill is founder-only today
    # (autofill_multi_user_enabled) while everyone else fills via the extension.
    browser_service_url: str = ""            # BROWSER_SERVICE_URL — e.g. http://browser-service.railway.internal:8080
    browser_service_token: str = ""          # BROWSER_SERVICE_TOKEN — shared bearer secret; MUST match the service
    browser_service_fallback_local: bool = True  # BROWSER_SERVICE_FALLBACK_LOCAL — on service outage, render locally (slower + ~400MB) rather than failing. Set 0 on a container with no room for a local Chromium.

    # Memory telemetry. An OOM kill leaves no traceback, so the only way to know
    # what was resident when the platform reaped us is to have logged it on the
    # way up. Cheap (two procfs reads); the watcher logs WARNING once container
    # usage crosses memory_warn_pct of the cgroup limit.
    memory_watch_interval_seconds: int = 120  # MEMORY_WATCH_INTERVAL_SECONDS — 0 disables the watcher
    memory_warn_pct: float = 85.0             # MEMORY_WARN_PCT — container usage share that flips the log line to WARNING

    # Discovery
    greenhouse_boards: str = ""
    lever_boards: str = ""
    ashby_boards: str = ""
    # FALLBACK-ONLY keyword list. Onboarding and the global scheduled pass always
    # pass explicit keywords (the user's own roles / the union of all users'
    # roles), so this list is used only when NO roles exist yet — a fresh deploy
    # or a role-less cold start. It used to be 100% AI/ML, which meant the shared
    # pool a brand-new non-tech user adopts from was all AI/ML — a mechanical or
    # nursing candidate saw almost nothing. Now it spans the major sectors so the
    # cold-start pool is broad; each user's real feed is still driven by THEIR roles.
    jobs_keywords: str = (
        "Software Engineer,Data Analyst,Machine Learning Engineer,Product Manager,"
        "Mechanical Engineer,Civil Engineer,Electrical Engineer,Manufacturing Engineer,"
        "Financial Analyst,Accountant,Registered Nurse,Healthcare Administrator,"
        "Marketing Manager,Sales Representative,Operations Manager,Project Manager,"
        "UX Designer,Business Analyst,Customer Success Manager,Human Resources Specialist,"
        "Supply Chain Analyst,Administrative Assistant"
    )

    @property
    def jobs_keywords_list(self) -> List[str]:
        return [k.strip() for k in self.jobs_keywords.split(",") if k.strip()]

    @property
    def greenhouse_boards_list(self) -> List[str]:
        return [b.strip() for b in self.greenhouse_boards.split(",") if b.strip()]

    @property
    def lever_boards_list(self) -> List[str]:
        return [b.strip() for b in self.lever_boards.split(",") if b.strip()]

    @property
    def ashby_boards_list(self) -> List[str]:
        return [b.strip() for b in self.ashby_boards.split(",") if b.strip()]

    # CORS configuration
    cors_allowed_origins: str = "http://localhost:5173,http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000,https://app.spotapply.ai,https://spotapply.ai"

    # Canonical-host redirect: requests hitting these hosts get a 301 to the
    # canonical host (path preserved). The bare domain spotapply.ai is the
    # canonical public face (landing, pricing, SEO); only www redirects to it.
    # app.spotapply.ai is intentionally NOT in the redirect list — it stays
    # live as the product host, so existing login/dashboard links keep working.
    canonical_host: str = "spotapply.ai"
    canonical_redirect_hosts: str = "www.spotapply.ai"

settings = Settings()

