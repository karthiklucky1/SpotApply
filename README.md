# HirePath — AI Job-Application Copilot

HirePath discovers real, fresh tech roles from company ATS APIs and job feeds, scores
each one against **your** résumé with a multi-stage matching cascade, drafts tailored
résumés and cover letters (fact-checked against your real experience), auto-fills the
application form in your browser, and keeps you in control of the final **Submit**.

It started as a single-user personal job agent and has grown into a multi-tenant web
app (HirePath) with accounts, per-user matching, a browser extension, and a Telegram
review loop. The guiding principle is unchanged: **the machine prepares, the human
decides.**

> **You always click Submit.** HirePath fills forms and prepares materials — it never
> silently fires off applications on your behalf.

---

## What it does

1. **Discovers** jobs from direct ATS boards (Greenhouse, Lever, Ashby, Workday,
   SmartRecruiters) plus aggregators and feeds (Indeed RSS, RemoteOK, Remotive,
   WeWorkRemotely, The Muse, Adzuna, Jooble, Reed, YC, Hacker News "Who is hiring",
   and search-engine discovery).
2. **Matches** every job to your résumé through a cascade — cheap filters first, the
   expensive LLM last — so only genuinely relevant roles reach you.
3. **Scores fit & hiring intent** — a 0–100 résumé fit score, a "senior reviewer"
   second opinion, ghost-posting detection, sponsorship/work-authorization assessment,
   and live hiring signals (fresh posting, funding/growth language, opening velocity).
4. **Tailors** an ATS-friendly résumé and cover letter, then runs a **grounding check**
   so nothing is invented beyond what your real résumé supports.
5. **Auto-fills** the application form (Greenhouse / Lever / Ashby / Workday) via the
   browser extension or Playwright, with human-like pacing.
6. **Hands off** to you for review — in the dashboard or via the Telegram bot — for
   approval, custom-question answers, or CAPTCHA solving before submission.

```
Discover ─▶ Match cascade ─▶ Score & enrich ─▶ Tailor (grounding check) ─▶ Auto-fill ─▶ You review & Submit
```

---

## The matching cascade

Stages run cheapest-first; a job only advances if it survives the previous gate, which
keeps LLM cost down at scale (see `app/matching/pipeline.py`):

| Stage | Module | What it does |
|-------|--------|--------------|
| 0. Retrieval | `matching/matcher.py` | BM25 + FAISS (`all-MiniLM-L6-v2`) pull the top-K candidate jobs for your résumé. |
| 1. Rule filter | `matching/filters/` | Hard gates: title/seniority, location, job-type (e.g. drops internships unless opted in), per-company cap & cooldown. |
| 2. Ghost filter | `discovery` / pipeline | Flags postings that look inactive or fake. |
| 3. Embedding gate | `matching/filters` | Drops jobs below a cosine-similarity floor. |
| 4. LLM reranker | `matching/reranker.py` | Claude scores fit 0–100 with reasoning, per-user profile aware. |
| 5. Hire probability | `matching/hire_probability.py` | Blends fit with hiring-intent signals into a priority score. |
| 6. Senior review | `intelligence/senior_reviewer.py` | An independent "senior engineer" verdict + 0–100 score. |

Enrichment layers add sponsorship/H-1B assessment (`intelligence/sponsorship.py`,
`h1b_data.py`, `work_auth.py`), urgency signals (`intelligence/urgency.py`), and
referral-message drafting (`intelligence/referral.py`).

---

## Tech stack

- **Backend:** Python 3.11, FastAPI, Uvicorn
- **Data:** Supabase Postgres in production (SQLite for local single-user dev), via SQLModel
- **Auth:** Supabase Auth (email/OAuth), JWT-verified server-side
- **Matching/ML:** `sentence-transformers` (all-MiniLM-L6-v2), FAISS, `rank-bm25`
- **LLM:** Anthropic Claude (primary) with OpenAI as an option — tailoring, reranking, grounding
- **Automation:** Playwright (Chromium) + a Chrome extension (`extension/`)
- **Docs:** `python-docx`, `pypdf` for résumé parsing/generation
- **Handoff:** `python-telegram-bot` for the review loop
- **Scheduling:** APScheduler (discovery/matching every ~6h; harvester/validator/reports daily/weekly)
- **Frontend:** Server-rendered Jinja templates + Tailwind (CDN) + Chart.js

---

## Project layout

```
app/
  api/            # FastAPI app: all routes, dashboard, auth, admin (server.py)
  db/             # SQLModel models, init, Supabase client
  discovery/      # ATS scrapers (greenhouse/lever/ashby/workday/smartrecruiters)
    sources/      # Aggregators & feeds (indeed_rss, remoteok, hn_whoishiring, yc, ...)
  matching/       # Cascade: matcher, rule/embedding filters, reranker, hire_probability
  tailoring/      # Résumé + cover-letter generation, ATS keywords, grounding check
  autofill/       # Playwright form filler + answer pack
  intelligence/   # Sponsorship/H1B, work auth, senior reviewer, urgency, referral
  strategy/       # Daily application scoring & limits engine
  analytics/      # Funnel, cost dashboard, CRM, daily reporter
  qa_store/       # Canonical applicant answers + memory resolver
  telegram_bot/   # Async handoff / approval loop
  templates/      # landing, dashboard, pricing, auth, privacy, terms, extension
  static/         # Assets
extension/        # Chrome extension (manifest v3): background, content, popup
scripts/          # CLI entrypoints: run_discovery, run_matching, seed_registry, ...
tests/            # pytest suite (matching, tailoring, grounding, autofill, funnel, ...)
data/             # Résumé master, FAISS index, generated docs, local SQLite
```

---

## Key data models (`app/db/models.py`)

`Job`, `Application`, `UserProfile`, `UserSubscription` / `UserUsage` / `PlanTier`,
`CompanyRegistry`, `DiscoveryRun`, `FunnelEvent`, `PendingQuestion`, `AnswerMemory`,
`UserPersonalMemory`, `H1BSponsor`, `UserNotification`, plus referrals/coupons
(`UserReferralReward`, `TrialGrant`, `Coupon`, `CouponRedemption`).

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
# Production-style: API only (the server has its own 6h scheduler)
uvicorn app.api.server:app --host 0.0.0.0 --port 8000

# Local all-in-one: API + Telegram bot + harvester/report schedulers
python -m app.main
```

Open **http://127.0.0.1:8000/** (landing) → **/dashboard** (the job board).

### Environment

Configure via `.env` (see `.env.example` for the full list):

- **Database/Auth:** `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
  `DATABASE_URL`. Leave Supabase blank to fall back to local SQLite single-user mode.
- **LLM:** `ANTHROPIC_API_KEY` (required for tailoring/reranking).
- **Telegram (optional):** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — lock the bot to
  your own chat ID.
- **Matching knobs:** `MIN_MATCH_SCORE`, `TOP_K_RERANK`, `DAILY_APPLY_LIMIT`.
- **Discovery targets:** `GREENHOUSE_BOARDS`, `LEVER_BOARDS`, `ASHBY_BOARDS` (comma-separated slugs).

---

## Browser extension

`extension/` is a Manifest-V3 Chrome extension that fills application forms in your own
browser (so you keep the session and control Submit). Load it unpacked via
`chrome://extensions` → Developer Mode → "Load unpacked" → select the `extension/`
folder, or visit **/extension** in the app for install instructions.

---

## Deployment

- **Docker:** `Dockerfile` installs deps + Playwright Chromium and runs Uvicorn.
- **Railway:** `railway.toml` (Docker builder, health check at `/health`).
- **Nixpacks / Heroku-style:** `nixpacks.toml` and `Procfile`
  (`web: uvicorn app.api.server:app`).

The FastAPI server runs an internal asyncio scheduler that triggers discovery and
matching roughly every 6 hours in both local and production environments.

---

## Testing

```bash
pytest                       # full suite
pytest tests/test_grounding.py tests/test_hire_probability.py -q
ruff check app               # lint
```

The suite covers matching filters, dedup, tailoring/cover-letter quality, the grounding
check, hire-probability scoring, autofill verification, the reject/approval flow, and
funnel analytics.

---

## Compliance & ethics

- **Public sources only.** Discovery uses public ATS APIs and job feeds, and respects
  `robots.txt`.
- **No LinkedIn/Indeed automation.** Those platforms' ToS prohibit it; listings from
  such sources are discovery-only links you open yourself.
- **Human review gate.** HirePath prepares and fills, but you approve and submit — in
  the dashboard or via Telegram (`/approve`, `/reject`).
- **Grounded content.** The tailoring grounding check prevents fabricating experience
  beyond what your résumé supports.
- **Your data, your control.** Profiles, résumés, and job pools belong to each user and
  can be edited, replaced, or cleared at any time.

---

## Status

Actively developed. Discovery, the full matching cascade, résumé/cover-letter tailoring
with grounding, per-user scoring, the dashboard (Pipeline / All Jobs / Boards views, fit
distribution, discovery-source insights), accounts, plans, and the Telegram review loop
are operational. Auto-fill runs under human supervision via the extension and Telegram.
