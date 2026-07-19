# SpotApply — AI Job-Application Copilot

SpotApply discovers real, fresh roles straight from company ATS APIs and job feeds,
scores each one against **your** résumé with a multi-stage matching cascade, drafts
tailored résumés and cover letters (fact-checked against your real experience),
auto-fills application forms in your browser, and keeps you in control of the final
**Submit**.

> **You always click Submit.** SpotApply prepares and fills — it never silently fires
> off applications on your behalf. *The machine prepares, the human decides.*

It began as a single-user personal job agent and has grown into a multi-tenant web app
with accounts, per-user matching, a freshness engine, a browser extension, and a
Telegram review loop.

---

## Why SpotApply

- **Fresh, not stale.** A per-board polling scheduler (the *pulse lane*) checks the
  companies you follow every few minutes and sweeps every live board at least hourly —
  new postings reach your board (and your phone) while you can still be one of the
  first applicants. Alert timestamps use the posting's true publish time, so
  "posted 7m ago" is measured, not claimed.
- **Precision over volume.** A cheapest-first cascade (retrieval → rule/ghost/embedding
  gates → two-tier LLM scoring) means the expensive model only reads jobs that deserve
  it — and you only see roles that genuinely fit.
- **Grounded tailoring.** Every tailored résumé passes a grounding check (nothing
  invented beyond your real résumé), a résumé "Doctor" (ATS coverage, weak bullets,
  AI-tell fingerprints, recruiter's-read verdict), and a **lock layer** that pins
  immutable facts — your degree, school, and dates can never be altered by tailoring.
- **Ghost-job aware.** Inactive, re-posted, and aggregator-recycled postings are
  detected and filtered before they waste your time.
- **Visa-aware.** Sponsorship and work-authorization assessment (including a public
  H-1B filing record lookup) is built into scoring, not bolted on.
- **Measured, not claimed.** Job cards carry a Truth Strip — "Verified live
  12 min ago" comes from our own board polling, and the dashboard header shows
  the real last-sweep time instead of a decorative status light.
- **Runs without paid LLM keys.** If no LLM provider is configured (or your
  account runs out of credits), scoring falls back to free local models — the
  distilled scorer when trained, else a calibrated cross-encoder — clearly
  labeled as local estimates (`LOCAL_SCORE_FALLBACK`, on by default).

---

## How it works

```
Discover ─▶ Match cascade ─▶ Score & enrich ─▶ Tailor (grounded) ─▶ Auto-fill ─▶ You review & Submit
```

1. **Discover** — direct ATS boards (Greenhouse, Lever, Ashby, Workday,
   SmartRecruiters, and more) plus ~20 aggregators and feeds (RemoteOK, Remotive,
   WeWorkRemotely, The Muse, Adzuna, Jooble, Reed, YC, Hacker News "Who is hiring",
   search-engine discovery). Jobs are scraped once into a shared pool and served to
   every matching user — cost scales with boards, not users.
2. **Match** — the cascade below filters cheaply first, so LLM spend stays low.
3. **Score & enrich** — 0–100 fit with reasoning, hire-probability signals, ghost
   score, sponsorship assessment, urgency, and an independent "senior reviewer"
   second opinion.
4. **Tailor** — ATS-friendly résumé + cover letter, grounded in your real experience,
   quality-checked, with immutable facts locked.
5. **Auto-fill** — the browser extension (or Playwright) fills the form with
   human-like pacing; unanswered questions route to you.
6. **Review & submit** — in the dashboard or via Telegram. The final click is yours.

### The freshness engine

Three scheduled lanes keep the pool current without redundant work:

| Lane | Cadence | Job |
|------|---------|-----|
| **Pulse lane** | every tick (~1 min) | Polls boards on their own `next_poll_at` schedule: **followed companies & recently-posting boards every ~5 min**, **every live board at least hourly**, dead boards daily. Unchanged boards are skipped via a posting-list signature. Brand-new jobs take a per-job fast path: role match → prescore → Claude → shortlist → fresh alert, in minutes. |
| **Fresh lane** | every ~2 h | Boards-only rescan of the whole registry. |
| **Full discovery** | every ~6 h | Boards + all aggregators/feeds, one global pass for all users. |

An independent **matching lane** (every ~5 min) scores each user's unscored pool so a
stalled discovery can never stall matching.

### The matching cascade

Stages run cheapest-first; a job only advances if it survives the previous gate
(`app/matching/pipeline.py`):

| Stage | Module | What it does |
|-------|--------|--------------|
| 0. Retrieval | `matching/matcher.py` | BM25 + FAISS (`all-MiniLM-L6-v2`) over unscored jobs, newest first, with a freshness reserve so new postings always get ranked. |
| 1. Rule filter | `matching/filters/` | Title/seniority/location/job-type gates, per-company cap & cooldown. |
| 2. Ghost filter | `matching/filters/` | Drops postings that look inactive or fake. |
| 3. Embedding gate | `matching/filters/` | Cosine-similarity floor. |
| 4. LLM cascade | `matching/reranker.py` | **Tier 1:** a cheap model (GPT-4o-mini or Claude Haiku) bulk-prescores candidates and drains clear misfits. **Tier 2:** Claude produces the authoritative 0–100 score with reasoning, tuned to your profile. |
| 5. Hire probability | `matching/hire_probability.py` | Blends fit with hiring-intent signals into a priority score. |
| 6. Senior review | `intelligence/senior_reviewer.py` | Independent "senior engineer" verdict, on demand. |

### Tailoring integrity

- **Grounding check** (`tailoring/grounding.py`) — every bullet must be supported by
  your master résumé; flagged drafts are rebuilt with reviewer feedback.
- **Lock layer** (`tailoring/lock.py`) — Education facts (degree, school, dates) are
  restored verbatim from your master résumé after every draft. An altered credential
  is structurally impossible, not merely detected.
- **Résumé Doctor** (`tailoring/doctor.py`) — ATS keyword coverage, weak-bullet and
  banned-word scan, integrity anchors, an AI-writing fingerprint check with a
  "reads human" score, and a recruiter's-read verdict — all surfaced in the review UI.

---

## Tech stack

- **Backend:** Python 3.11, FastAPI, Uvicorn
- **Data:** Supabase Postgres in production, SQLite for local single-user dev (SQLModel)
- **Auth:** Supabase Auth (email/OAuth), JWT-verified server-side
- **Matching/ML:** `sentence-transformers` (all-MiniLM-L6-v2), FAISS, `rank-bm25`
- **LLM:** Anthropic Claude (primary) and OpenAI (Tier-1 prescoring) — both
  OPTIONAL: with no keys, free local models score everything
- **Automation:** Playwright (Chromium) + a Manifest-V3 Chrome extension
- **Docs:** `python-docx`, `pypdf` for résumé parsing/generation
- **Handoff:** `python-telegram-bot` review loop
- **Scheduling:** in-process asyncio lanes (pulse/fresh/full/matching) + APScheduler
  (harvester, validator, reports)
- **Frontend:** server-rendered Jinja + Tailwind (CDN) + Chart.js

---

## Project layout

```
app/
  api/            # FastAPI app: all routes, dashboard, auth, admin (server.py)
  db/             # SQLModel models, init/migrations, Supabase client
  discovery/      # ATS scrapers + title filter + dedupe pipeline
    sources/      # Aggregators & feeds (~20)
  matching/       # Cascade: matcher, filters, two-tier reranker, hire_probability
  tailoring/      # Tailor, ATS keywords, grounding, doctor, lock (immutable facts)
  autofill/       # Playwright form filler + answer pack
  intelligence/   # Sponsorship/H-1B, work auth, senior reviewer, urgency, referral,
                  # skill gap, job check
  strategy/       # pulse_lane (freshness), hot_lane (legacy), fresh_alerts,
                  # adoption, daily engine
  analytics/      # Funnel, cost dashboard, CRM, reporter
  qa_store/       # Canonical applicant answers + memory resolver
  telegram_bot/   # Async handoff / approval loop
  templates/      # landing, dashboard, pricing, auth, privacy, terms, extension
extension/        # Chrome extension (MV3): background, content, popup
scripts/          # CLI entrypoints: run_discovery, run_matching, seed_registry, ...
tests/            # pytest suite
data/             # Résumé master, FAISS index, generated docs, local SQLite
```

---

## Setup (local dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env          # fill in API keys (see below)
python -m app.db.init_db      # create tables
```

Populate and rank jobs:

```bash
python -m scripts.seed_registry     # seed company boards (optional)
python -m scripts.run_discovery     # pull jobs from sources
python -m scripts.run_matching      # score them against your résumé
```

Run the app:

```bash
# Production-style: API + internal scheduler lanes
uvicorn app.api.server:app --host 0.0.0.0 --port 8000

# Local all-in-one: API + Telegram bot + harvester/report schedulers
python -m app.main
```

Open **http://127.0.0.1:8000/** (landing) → **/dashboard** (your board).

### Environment

Configure via `.env` (see `.env.example` for the full list):

| Group | Keys | Notes |
|-------|------|-------|
| Database / Auth | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL` | Leave blank for local SQLite single-user mode. |
| LLM | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (both optional) | Best scoring quality with Claude; with neither key set, free local models score everything (`LOCAL_SCORE_FALLBACK`). |
| Payments | `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID_PRO`, `STRIPE_WEBHOOK_SECRET`, `PAYMENT_BANK_DETAILS` | All empty = payments off, everyone gets Pro free. Set the Stripe vars to enable $10/mo checkout; `PAYMENT_BANK_DETAILS` shows a manual bank-transfer option. |
| Freshness | `PULSE_LANE_ENABLED`, `PULSE_FAST_INTERVAL_MINUTES`, `PULSE_FLOOR_INTERVAL_MINUTES` | Defaults: on, 5 min fast lane, 60 min floor. |
| Matching | `MIN_MATCH_SCORE`, `TOP_K_RERANK`, `LLM_RERANK_CAP`, `DAILY_APPLY_LIMIT` | Cascade and volume tuning. |
| Discovery | `GREENHOUSE_BOARDS`, `LEVER_BOARDS`, `ASHBY_BOARDS` | Comma-separated slugs (registry-seeded boards cover the rest). |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optional review loop, locked to your chat. |

---

## Browser extension

`extension/` is a Manifest-V3 Chrome extension that fills application forms in your own
browser — you keep the session, and you control Submit. Load it unpacked via
`chrome://extensions` → Developer Mode → "Load unpacked" → select `extension/`, or
visit **/extension** in the app for guided install.

---

## Deployment

- **Docker:** `Dockerfile` installs dependencies + Playwright Chromium and runs Uvicorn.
- **Railway:** `railway.toml` (Docker builder, health check at `/health`).
- **Nixpacks / Heroku-style:** `nixpacks.toml` and `Procfile`.

The FastAPI server runs its scheduler lanes in-process — no external cron required.

---

## Testing

```bash
pytest                        # full suite
pytest tests/test_pulse_lane.py tests/test_cascade.py -q
ruff check app                # lint
```

The suite covers the matching cascade, dedupe, freshness scheduling, fresh alerts,
tailoring quality (grounding, doctor, lock layer), autofill verification, tenant
scoping, and funnel analytics.

---

## Compliance & ethics

- **Public sources only.** Discovery uses public ATS APIs and job feeds and respects
  `robots.txt`.
- **No LinkedIn/Indeed automation.** Their ToS prohibit it; listings from such sources
  are discovery-only links you open yourself.
- **Human review gate.** SpotApply prepares and fills; you approve and submit.
- **Grounded content.** Tailoring cannot fabricate experience or alter your
  credentials — enforced in code, not just prompts.
- **Your data, your control.** Profiles, résumés, and job pools belong to each user
  and can be edited, replaced, or cleared at any time.

---

## Pricing

Two plans, stated the same everywhere in the app: **Free** (5 tailored
resumes/day, 2 auto-fills/week, full discovery + scoring) and **Pro — $10/month**
(no caps). While Stripe is not configured the app runs in pre-revenue mode and
every account gets Pro for free.

## Product research

`docs/research/` holds the strategy work: a competitive analysis, a freshness
strategy, and a user-research-driven redesign plan
(`user-research-redesign-2026-07.md` + appendix) that maps the roadmap —
Truth Layer, Visa Odds, Outcome Loop, Trust Economics, Recruiter Bridge.

## Status

Actively developed. Operational today: discovery across direct ATS boards + feeds, the
pulse-lane freshness engine with the "My Companies" watchlist, the full matching
cascade with two-tier LLM scoring, grounded tailoring with the Doctor and lock layer,
per-user scoring and fresh alerts, dashboard (Pipeline / All Jobs / Ghost Jobs /
Skill Gap / Boards), accounts and plans, and the Telegram review loop. Auto-fill runs
under human supervision via the extension.
