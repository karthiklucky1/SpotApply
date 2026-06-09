# JobAgent — Personal Job Application Pipeline

JobAgent is a private, single-user job application assistant built for Karthik's own job search. It discovers AI/ML roles from company career APIs, filters them using a 3-tier cascade, matches them against the master resume in `data/resume_master.md`, drafts tailored application materials, autofills forms, and pings the owner on Telegram when an answer or final review is needed.

This is not a SaaS product, multi-user tool, or fully autonomous application submitter. It is a personal cockpit for finding, preparing, and tracking job applications while keeping the final decision with the applicant.

## Architecture

```
Discovery → 3-Tier Match Cascade (Rules → Embedding similarity → LLM reranker) → Tailoring (Grounding Check) → Autofill (Human-like pacing) → Telegram handoff → Karthik reviews/submits
```

Stages run independently and are cron-triggered. Results land in local SQLite. The Telegram bot is the only always-on component, and it should be locked to the configured owner chat ID.

## Personal-use scope

- **Single applicant:** all settings, generated documents, and answer memory are for Karthik only.
- **Local-first:** the dashboard is intended to run on localhost, not as a public web app.
- **Owner-only Telegram:** set `TELEGRAM_CHAT_ID` so the bot ignores messages from any other chat.
- **No account system:** there are no teams, tenants, shared profiles, or admin roles.
- **Manual final submit:** JobAgent can prepare and fill, but the applicant reviews and submits (or approves programmatically via Telegram).

## Compliance & ethics

- **Compliance focus:** Sources exclusively from public ATS APIs. Does not automate LinkedIn or any platform prohibiting automation.
- **No LinkedIn / Indeed automation.** Their ToS prohibits it and detection is real. Listings from these sources are discovery-only (links you open manually).
- **Review gate:** The agent fills the form and takes a screenshot, allowing Karthik to click `/approve` or `/reject` from Telegram before submitting.
- **Respects robots.txt** on any site we crawl.

## Stack

- Python 3.11+, FastAPI, SQLite (via SQLModel)
- `sentence-transformers` (all-MiniLM-L6-v2) + FAISS for matching & grounding verification
- Anthropic API (Claude) or OpenAI (GPT-4o) for tailoring, reranking, and grounding validation
- Playwright for browser automation
- python-telegram-bot for the handoff loop
- APScheduler for cron jobs

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # fill in API keys
python -m app.db.init_db
```

Then run discovery once to populate jobs:
```bash
python -m scripts.run_discovery
python -m scripts.run_matching
```

To start the private Telegram bot + local API:
```bash
python -m app.main
```

Dashboard: `http://127.0.0.1:8000/dashboard`

## Project layout

```
app/
  discovery/      # Greenhouse, Lever, Ashby, Workday scrapers
  matching/       # Cascade filtering: rules, embeddings, LLM reranking
  tailoring/      # Resume + cover letter generation with Volta Grounding Check
  autofill/       # Playwright-based form filler with human-like interactions
  qa_store/       # Canonical applicant answers resolver
  analytics/      # Funnel analytics and reporter
  telegram_bot/   # Async handoff loop & user approval flow
  db/             # SQLModel schemas
  api/            # FastAPI endpoints
  intelligence/   # Sponsorship & company intelligence
  strategy/       # Daily application scoring & limits engine
scripts/          # CLI entrypoints
data/             # Resume, FAISS index, generated docs
```

## Status

Fully functional personal workflow. Discovery, cascade matching, and resume tailoring (featuring Volta grounding check) are operational; autofill runs programmatically under Telegram supervision (Approve/Reject actions and CAPTCHA handoffs). Future-feature stubs are in place for sponsorship tracking, memory learning, and CRM dashboards.
