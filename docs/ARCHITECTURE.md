# SpotApply — How It Actually Works

Read from the code, not the README. Every claim cites `file:line`. Numeric claims went
through an independent verification pass; the one that failed is called out inline (§7.2).

Companion docs: **[CAPACITY.md](CAPACITY.md)** (every cap + the arithmetic at those caps) ·
**[SCALING.md](SCALING.md)** (10 → 10,000 users) · **[MEMORY.md](MEMORY.md)** (the OOM post-mortem).

---

## 1. The system in one picture

```
                          ┌─────────────────────────────────────────┐
                          │  ONE uvicorn process (deliberately)     │
                          │  app/api/server.py                      │
                          └─────────────────────────────────────────┘
                                          │
        ┌──────────────┬──────────────┬───┴────────┬──────────────┬─────────────┐
        │              │              │            │              │             │
   ┌────▼────┐   ┌─────▼─────┐  ┌─────▼─────┐ ┌────▼─────┐  ┌─────▼──────┐ ┌────▼────┐
   │ web/API │   │ scheduler │  │  fresh    │ │  pulse   │  │  matching  │ │ scoring │
   │ FastAPI │   │   6 h     │  │  lane 2 h │ │ lane 60 s│  │  lane 5 min│ │ lane 90s│
   └─────────┘   └───────────┘  └───────────┘ └──────────┘  └────────────┘ └─────────┘
        │              └──────────────┴────────────┴──────────────┴─────────────┘
        │                                     │
        │                        ┌────────────▼─────────────┐
        │                        │  DISCOVERY → shared pool │  Job.user_id = "__shared__"
        │                        │  scrape once, serve many │  pipeline.py:155
        │                        └────────────┬─────────────┘
        │                                     │  adoption.py — per-user DB copy
        │                        ┌────────────▼─────────────┐
        │                        │  PER-USER POOL           │  Job.user_id = <uid>
        │                        └────────────┬─────────────┘
        │                                     │
        │                        ┌────────────▼─────────────┐
        │                        │  MATCH CASCADE (§4)      │  cheapest filter first
        │                        │  6 gates → 2 LLM tiers   │
        │                        └────────────┬─────────────┘
        │                                     │
        │                        ┌────────────▼─────────────┐
        └───────────────────────►│  SHORTLIST → Application │
                                 └────────────┬─────────────┘
                                              │
                          ┌───────────────────┼───────────────────┐
                          │                   │                   │
                    ┌─────▼─────┐      ┌──────▼──────┐     ┌──────▼──────┐
                    │  TAILOR   │      │  AUTOFILL   │     │ MV3 EXTENSION│
                    │ + grounding│     │ (founder    │     │ (everyone    │
                    │  + doctor │      │  only)      │     │  else)       │
                    └───────────┘      └─────────────┘     └──────────────┘
                                              │
                                    ┌─────────▼──────────┐
                                    │ HUMAN CLICKS SUBMIT│  never automated
                                    └────────────────────┘

External: Supabase (Postgres + Auth + Storage) · Anthropic · OpenAI · ~20 ATS/feed sources
          browser-service/ (optional separate container — see MEMORY.md)
```

**Why one process.** Every lane lock, LLM budget counter, in-flight claim, and rate limiter
lives in a module global (`reranker.py:98-100`, `common/inflight.py:19-20`,
`scoring_lane.py:51`). A second worker doubles scraping and LLM spend, so startup logs
CRITICAL if `WEB_CONCURRENCY > 1` (`server.py:398-409`). Extra *web* replicas are fine with
`LANES_ENABLED=0`; a second *lane* process is a re-architecture (SCALING.md §2.2).

---

## 2. The six background lanes

All started as asyncio tasks in `startup_event` (`server.py:391`). `app/main.py` adds only
the Telegram bot and registry cron — never these, or they double-run.

| Lane | Cadence | Does | Lock | Where |
|---|---|---|---|---|
| **Scheduler** | 6 h | Full global discovery (all sources) → adopt → match | `discovery_guard` | `server.py:969` |
| **Fresh** | 2 h | Registry boards + 7 keyless feeds (`phase="fresh"`) | `discovery_guard` | `server.py:955` |
| **Pulse** | 60 s | Per-board `next_poll_at` schedule; changed boards only; per-job fast path to alerts | `_TICK_LOCK` | `strategy/pulse_lane.py` |
| **Matching** | 5 min | FAISS retrieval + re-shortlist + self-heal backstop | `discovery_guard` | `server.py:884` |
| **Scoring** | 90 s | **The primary scorer.** Cross-user, parallel, lock-free | `_LANE_LOCK` | `strategy/scoring_lane.py` |
| **Registry** | daily/weekly | Seed, validate, retire boards | — | `server.py:462` |

**Pulse vs hot lane:** exactly one runs. `PULSE_LANE_ENABLED=1` (default) picks pulse; `0`
falls back to the legacy 20-min hot lane (`server.py:471-474`). Never both — they'd
double-fetch every board.

**Pulse cadence tiers** (`pulse_lane.py:94-120`): watchlist + recently-posting boards every
5 min; every live board within 60 min; dead boards daily. Unchanged boards short-circuit on
a `poll_hash` signature and do zero downstream work.

**Why scoring is separate from matching.** The matching lane iterates users *serially* under
the discovery lock with a 240 s budget, and one pass is ~130–190 s (FAISS rebuild + BM25 +
120 cross-encoder pairs). Only ~2 users fit per tick, and it always restarts from the head of
the list — so users deep in the list would never be scored. The scoring lane is lock-free,
holds no FAISS, and round-robins across all users, which is what actually makes multi-tenancy
work (`scoring_lane.py:8-23`).

---

## 3. Per-person: one user, end to end

This is the "how do we treat each person" answer. Every step is per-tenant.

### 3.1 Signup → first jobs on the board

| # | Step | Tech | Cost | Where |
|---|---|---|---|---|
| 1 | Sign up / in — entirely client-side; the server never sees a password | Supabase Auth (supabase-js) | 0 | `server.py:1582` |
| 2 | Every request resolves `user_id` from `Authorization: Bearer`, falling back to an `sb_token` cookie | `_get_user_id` | 0 | `server.py:228` |
| 3 | JWT verified against Supabase Auth; **only successes** cached 60 s (revoked tokens die fast, an Auth blip can't lock everyone out) | `supabase_client.py:75` | 1 HTTPS/token/min | `supabase_client.py:32-44` |
| 4 | `last_active_at` stamped, throttled to 1 UPDATE / user / 15 min — feeds the dormancy gate | SQLModel | ≤1 UPDATE/15 min | `server.py:272` |
| 5 | `UserProfile` created lazily, **fail-closed**: a missing uid raises rather than returning some other user's profile | SQLModel | 1 SELECT | `answer_pack.py:65-81` |
| 6 | Résumé uploaded to Supabase Storage at `resume/{uid}/resume.{ext}` | supabase-py Storage, `anyio.to_thread` | 1 PUT | `server.py:1618` |
| 7 | **LLM #1** — résumé → structured profile JSON (contact, education, experience, skills, 4–6 suggested roles) | `claude-haiku-4-5-20251001`, max_tokens 2500 | 1 Haiku call | `server.py:1741` |
| 8 | Falls back to a free deterministic parser on any failure, so signup never yields an empty profile | `resume_basic_extract` | 0 | `server.py:1754` |
| 9 | `target_roles` auto-seeded (≤6) — **never overwrites roles the user chose** | string work | 0 | `server.py:1810` |
| 10 | Background fan-out: `seed_new_user`, GitHub/LinkedIn harvest, cross-profile alignment | BackgroundTasks | 1–2 Haiku | `server.py:1855` |
| 11 | **Adoption** — shared pool → this user's own rows (§3.2) | SQLModel + MiniLM | CPU only | `adoption.py:107` |
| 12 | If still under `onboarding_min_jobs`=25 on-role jobs, actively scrape *their* roles (the shared pool skews to existing users' roles) | scrapers | network | `adoption.py:295` |
| 13 | `run_matching` under the lock; if busy, poll `run_scoring_lane()` ≤3× at 20 s so the board isn't empty | — | ≤3 cycles | `adoption.py:202-255` |

### 3.2 Adoption — how a shared posting becomes *your* row

"Scrape once, serve many": scheduled lanes write postings **once** to
`Job.user_id == "__shared__"` (`pipeline.py:155`). Adoption then makes each user a physical copy.

```
shared pool (≤3000 rows, ≤21 days old, load_only on RawJob columns)
   │                                        adoption.py:148-168
   ├── title_hits   ← role_title_match(title, user's roles)     ALL taken
   │                  title_filter.py:324 — alias/token aware
   └── others       ← semantic extras, only if room remains
                      need = min(limit - len(title_hits), ADOPTION_SEMANTIC_MAX_EXTRAS=50)
                      query vec = MiniLM(", ".join(roles) + résumé[:3000])
                      encode ≤1500 off-title jobs, keep cosine ≥ 0.30, top `need`
                                                             adoption.py:34-79
   ▼
truncate to ADOPT_MAX_JOBS = 400  ──►  _upsert() into the user's pool
                adoption.py:31                pipeline.py:201
```

Two things worth knowing:

- **A role-less user once adopted the whole shared pool** (115k jobs), because
  `role_title_match` accepts *every* title when roles are empty. Hence the
  `_suggest_roles(profile)` fallback at `adoption.py:131-145`.
- CLAUDE.md calls adoption "a cheap DB copy." It also runs **up to 1,501 MiniLM encodes per
  user per pass** (`adoption.py:55-79`) — real CPU, ~23 s, ×16 passes/day.

### 3.3 What state each user owns

| State | Where | Bound |
|---|---|---|
| `UserProfile` row | Postgres | 1 |
| Résumé file | Supabase Storage `resume/{uid}/` | 1 |
| **Per-user FAISS index** `data/jobs_{uid}.faiss` | container disk | `IndexFlatIP`, 384-dim, **⚠️ unbounded — see §7.2** |
| Copied `Job` rows (full text, incl. description) | Postgres | 100–400/day, **no per-user age-close** |
| `Application`, `AnswerMemory`, `UserPersonalMemory`, `UserNotification`, `UserUsage`, `UserSubscription` | Postgres | per-user |

A single shared `jobs.faiss` used to let tenants overwrite each other's vectors — hence the
per-user file (`matcher.py:186-198`).

### 3.4 Two things that bound per-user cost

- **Dormancy gate** — `DORMANT_USER_GRACE_DAYS=21` (`config.py:276`). Users idle >21 days
  drop out of every lane's work list, so they consume no adoption, no scoring, no budget.
  This is what makes "N users" mean *active*, not *registered*.
- **Global LLM budget, divided fairly** — `LLM_DAILY_FINAL_CAP=1500` platform-wide, split
  round-robin (`scoring_lane.py:446-459`). Fair, but **not bigger**: each user gets ~1500/N
  authoritative scores per day. This is the system's binding constraint (CAPACITY.md §4).

---

## 4. The ranking cascade — how a job gets its score

Ordered cheapest-first so LLM spend only touches survivors. Funnel at defaults:

```
per-user FAISS index          ≤ 4,000 vectors      matcher.py:41
   ↓
retrieval corpus              ≤ 2,000 UNSCORED     matcher.py:363
   ↓  ① BM25 + FAISS → RRF
cross-encoder                 = 120 exactly        config.py:165   ← CPU wall
   ↓  ② rule  ③ ghost  ④ embedding  ⑤ door
Tier-1 prescore               ≤ 600 (never binds)  config.py:186
   ↓  advance if ≥ 35
Tier-2 Claude                 ≤ 100                config.py:174
   ↓  ≥ 35, daily limit, company cap
SHORTLISTED Application
```

### Stage ① Retrieval — BM25 + FAISS, fused by RRF

**Only unscored jobs.** `rerank_score IS NULL`, newest first, cap 2,000
(`matcher.py:380-423`). Scored jobs re-shortlist via a direct query instead — letting them
compete starved fresh postings of cross-encoder slots.

- **Query side:** the résumé is split on `#`/`##` headers, chunks >50 chars kept, plus one
  synthesized summary chunk built **only from this user's** roles/location/skills/summary
  (`matcher.py:75-98`) — never another candidate's data.
- **Document side:** `_job_text` = title + title again (deliberate 2× weight) + company |
  location + first 800 chars of the JD (`matcher.py:211-220`). 800, not 4000, because encode
  time scales with length and the LLM sees the full text later.
- **Lexical:** `rank_bm25.BM25Okapi`, naive whitespace tokenizer, max-similarity across résumé
  chunks (`matcher.py:432-449`).
- **Semantic:** `faiss.IndexFlatIP` over `all-MiniLM-L6-v2` 384-dim L2-normalized vectors, so
  inner product *is* cosine. Full-index search per chunk (`matcher.py:451-469`).
- **Fusion — Reciprocal Rank Fusion, k=60, equal weight, verbatim at `matcher.py:476`:**

  ```
  rrf_score = 1.0/(60.0 + bm25_rank) + 1.0/(60.0 + faiss_rank)
  ```

  Raw BM25 and cosine magnitudes are **discarded** — only ranks matter, which is what makes
  two incomparable scales safely combinable.
- **Freshness reserve:** of the 120 cross-encoder slots, **60 are reserved for the freshest**
  postings and 60 go by relevance (`fresh_budget.py:51-72`). Without it, relevance ordering
  buries new postings — and being early is the product.
- **Cross-encoder:** `mixedbread-ai/mxbai-rerank-xsmall-v1` locally (~1 s/pair — **the CPU
  bottleneck**) or the Jina API (`jina-reranker-v2-base-multilingual`, ~300–800 ms for the
  whole batch) when `RERANK_PROVIDER=jina`. Gate: score ≥ `MIN_MATCH_SCORE`=0.15, with a soft
  floor of 0.09 admitting the top 5 if the batch zeroes out (score scales differ per backend).

### Stages ②–⑤ The cheap gates

Each rejection **stamps a synthetic `rerank_score`** so the job leaves the unscored corpus
permanently instead of being re-read every pass.

| Gate | Logic | Stamps | Where |
|---|---|---|---|
| **② Rule** | 7 ordered rules, first failure wins: job type (internship) → seniority → title → location/country → company cap → … Pure regex, no network | `10` | `filters/rule_filter.py:164-314` |
| **③ Ghost** | 7 additive local signals: closed → 1.0; age ≥60 d → +0.40, ≥45 d → +0.20; stale `last_seen`; aggregator repost; … cutoff **0.60 hardcoded** | `5` | `filters/ghost_detector.py:84-143` |
| **④ Embedding** | cosine(résumé chunks, job) ≥ `MIN_EMBEDDING_SCORE`=0.28 | `15` | `filters/embedding_filter.py:19-46` |
| **⑤ Door** | Undocumented 5th gate. Derives a `RoleBar` (years, axis, domain, onsite, pedigree) from the JD and classifies the candidate against it — all local heuristics despite living under `intelligence/` | `20` | `pipeline.py:620-636` |

> ⚠️ `GHOST_SCORE_THRESHOLD` in config is a **dead setting** — the real cutoff is hardcoded in
> `GhostResult.is_ghost` (`config.py:383` vs `ghost_detector.py:70`).

> ⚠️ The embedding gate re-encodes **all résumé chunks per candidate**
> (`embedding_filter.py:32-36`) — ~(chunks+1) × 120 MiniLM encodes per pass, roughly 7× more
> work than necessary. Hidden CPU cost of the matching lane.

### Stage ⑥ Tier-1 prescore — cheap bulk triage

- **Model:** `gpt-4o-mini` (`config.py:185`), 16 workers, `max_tokens=120`,
  `response_format={"type":"json_object"}`.
- **Prompt:** a tiny contract (`{"score": 0-100, "reason": "<max 15 words>"}`) plus this
  user's roles, skills, years, country, sponsorship need. JD truncated to 1,800 chars.
- **Gate:** `min(PRESCORE_ADVANCE_THRESHOLD, SHORTLIST_SCORE_THRESHOLD)` = **35** — clamped so
  nothing that could plausibly shortlist is dropped without an authoritative look.
- **Below the gate:** stamped with the prescore and drained. This is what stops the unscored
  backlog being re-read forever.
- **Cost:** ~$0.0002/prescore.

### Stage ⑦ Tier-2 final — the authoritative score

- **Model:** `claude-haiku-4-5-20251001` (`config.py:365`), 12–20 workers, output 0–100 + reasoning.
- **Prompt caching is the cost lever.** The user-stable half (rubric + résumé[:16000]) is a
  cached block; the per-job half is not. **If that block is under 15,500 chars it is padded
  with a labeled verbatim résumé repetition** purely to clear Haiku 4.5's **4,096-token cache
  minimum** — below it, Anthropic silently ignores `cache_control` and every call re-bills the
  full résumé at 1× (`reranker.py:379-415`).

  This padding cuts steady-state cost **$0.0075 → $0.0033 per final, ~56%**. It is load-bearing,
  not cargo cult. (Caveat: a résumé under ~2,580 chars gets no padding and no caching — a 2.3×
  penalty falling exactly on new users.)
- **Budget:** `score()` **raises** rather than returning 0.0 when the cap is hit, so the job
  stays `rerank_score IS NULL` and is retried tomorrow instead of being silently buried at 0.

### Stage ⑧ Post-score ranking

| Field | Meaning |
|---|---|
| `rerank_score` | 0–100 authoritative fit — the Tier-2 output |
| `blended_score` | Fit + hiring-intent signals — **orders the user's board** |
| `hire_probability_signals` | JSON of the intent signals |
| `senior_fit_score` / `senior_verdict` | Independent "senior engineer" review (§7.1) |
| `ghost_score` / `ghost_flags` | Liveness |

Shortlisting: score ≥ `SHORTLIST_SCORE_THRESHOLD`=35, best-first, under
`DAILY_SHORTLIST_LIMIT`=200 and the **company cap** — 3 active apps/company, 40-day cooldown.
A new job outscoring the weakest merely-SHORTLISTED cap-holder by ≥5 displaces it; TAILORED
and beyond are never displaced (`config.py:280-287`).

---

## 5. The apply path

```
SHORTLISTED
   │
   ├─► TAILOR  (user-triggered, Sonnet 4.6)
   │     ├─ lock layer     — Education/degree/dates restored verbatim from the master,
   │     │                   so an altered credential is structurally impossible
   │     ├─ grounding      — MiniLM cosine per bullet vs the master; a bullet with a metric
   │     │                   ("43%", "2,500 req/min") absent from its source is force-checked
   │     │                   by an LLM, because similarity alone waves fabrications through
   │     └─ doctor         — ATS-format + honesty verdict
   │
   ├─► AUTOFILL (server-side Playwright) ── FOUNDER ONLY
   │     autofill_multi_user_enabled = False; _set_fill_owner refuses everyone else
   │     rather than submit under the wrong identity        agent.py:624
   │
   └─► MV3 CHROME EXTENSION ── everyone else, in their own browser, zero server cost
         Greenhouse, Lever, Ashby, LinkedIn, Indeed, Workday   extension/content.js

                        THE HUMAN ALWAYS CLICKS SUBMIT
```

That founder-only gate is the single most load-bearing fact about the apply path: the
extension, not server-side Playwright, is the real multi-tenant autofill path. It's also why
moving *autofill* to the browser service would have been the wrong call — see MEMORY.md.

---

## 6. Tech stack, by stage

| Stage | Technology |
|---|---|
| Web / API | FastAPI, Uvicorn (1 worker), Jinja2, Tailwind, Chart.js |
| Auth | Supabase Auth (client-side), JWT verified server-side, 60 s positive-only cache |
| DB | Supabase Postgres + SQLModel/SQLAlchemy (SQLite fallback locally), pool 10+20 |
| Storage | Supabase Storage (résumés, generated docs) |
| Discovery | httpx, feedparser, BeautifulSoup, 14 keyless ATS APIs, ~20 aggregators |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2`, 384-dim, L2-normalized |
| Lexical retrieval | `rank_bm25.BM25Okapi` |
| Vector retrieval | `faiss.IndexFlatIP`, per-user index files |
| Fusion | Reciprocal Rank Fusion, k=60 |
| Rerank | `mixedbread-ai/mxbai-rerank-xsmall-v1` (local) or Jina `jina-reranker-v2-base-multilingual` |
| Tier-1 LLM | `gpt-4o-mini` |
| Tier-2 LLM | `claude-haiku-4-5-20251001` with ephemeral prompt caching |
| Tailoring LLM | `claude-sonnet-4-6` |
| Browser | Playwright Chromium — gated (`common/browser.py`) or remote (`browser-service/`) |
| Scheduling | asyncio tasks in-process; APScheduler only in `app/main.py` |
| Notifications | python-telegram-bot, in-app `UserNotification` |

---

## 7. Known issues found while mapping

### 7.1 Fixed — the founder's résumé reached other tenants

`data/profiles/{backend,ai_agents,fullstack}.md` are the founder's CV variants (real name,
phone, email), committed and shipped. `SeniorReviewer` sent all three as the cached system
block of **every** tenant's review, and the recommended variant was then read by `tailor.py`
as that tenant's **tailoring master** — putting the founder's identity into résumés other
users download and send. Grounding passes it, because the output *is* grounded in that master;
grounding cannot catch a wrong master.

Fixed by `app/common/tenancy.is_founder`, applied in three places. Non-founders are reviewed
and tailored strictly from their own résumé. Fails closed: with `founder_user_id` unset, every
real uid is a non-founder. Tests: `tests/test_founder_profile_leak.py`.

### 7.2 Open — per-user FAISS index growth is unbounded

`REBUILD_MAX_JOBS=4000` bounds only a **from-scratch** rebuild (`matcher.py:265`, `:302`). The
incremental branch appends with `existing_index.add()` + `np.concatenate` and **never evicts**
(`matcher.py:328-340`), so an index only shrinks when `force_rebuild` fires. The oft-quoted
"≤6.1 MB/user" is not an upper bound. *(This is the one numeric claim the verification pass
overturned.)*

### 7.3 Open — other items worth knowing

| Issue | Where | Why it matters |
|---|---|---|
| Tailoring bypasses the LLM budget entirely | `tailor.py` never calls `_register_final_call()` | 150 tailors/day/user × $0.045–0.17 = **$6.75–25.50/user/day** — one account can outspend the whole scoring budget 1–5× |
| Senior review fires per drawer-open, uncapped, synchronously | `server.py:3640-3649` | Browsing 50 jobs = $0.32–0.56 |
| Pulse fast path keeps paying Tier-1 after the budget trips | `pulse_lane.py` has no cycle-level budget check | 4,110–14,400 wasted prescores/day |
| Successful Claude finals record **nothing** in `LlmSpend` | `scoring_lane.py:179-180` | The dominant cost line is invisible — raising caps would be flying blind |
| `funnel_events` has no retention and no `user_id`; `get_summary` is an N+1 | `analytics/funnel.py:35-81` | Grows forever, scanned per analytics view |
| Account deletion leaves 9+ tables of tenant data + FAISS files | `server.py:7943-7962` | Blocks any GDPR/EU story |
| `_lane_user_ids` does a blocking Storage `list()` **per user per tick** | `server.py:330-346` | O(users) HTTPS inside a 150 s tick budget |

---

## 8. Where to look next

- **"How many users can this serve?"** → [CAPACITY.md](CAPACITY.md) §4. Short answer: ~10–15
  comfortably, degrading to ~30, structurally broken past that. Binding constraint is
  `LLM_DAILY_FINAL_CAP=1500`, and it is one config line.
- **"What do we do as we grow?"** → [SCALING.md](SCALING.md) §6 has the ordered roadmap with
  effort sizing, and §5 separates config changes from genuine re-architecture.
- **"Why did it OOM?"** → [MEMORY.md](MEMORY.md).
