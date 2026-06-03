# JobAgent — Personal Job Application Pipeline

JobAgent is a private, single-user job application assistant built for Karthik's own job search. It discovers AI/ML roles from company career APIs, matches them against the master resume in `data/resume_master.md`, drafts tailored application materials, autofills forms where allowed, and pings the owner on Telegram when an answer or final review is needed.

This is not a SaaS product, multi-user tool, or fully autonomous application submitter. It is a personal cockpit for finding, preparing, and tracking job applications while keeping the final decision with the applicant.

## Architecture

```
Discovery → Matching → Tailoring → Autofill → Telegram handoff → Karthik reviews/submits
```

Stages run independently and are cron-triggered. Results land in local SQLite. The Telegram bot is the only always-on component, and it should be locked to the configured owner chat ID.

## Personal-use scope

- **Single applicant:** all settings, generated documents, and answer memory are for Karthik only.
- **Local-first:** the dashboard is intended to run on localhost, not as a public web app.
- **Owner-only Telegram:** set `TELEGRAM_CHAT_ID` so the bot ignores messages from any other chat.
- **No account system:** there are no teams, tenants, shared profiles, or admin roles.
- **Manual final submit:** JobAgent can prepare and fill, but the applicant reviews and submits.

## Compliance & ethics

- **No LinkedIn / Indeed automation.** Their ToS prohibits it and detection is real. Listings from these sources are discovery-only (links you open manually).
- **No auto-submit.** The agent fills the form; Karthik clicks Submit. This is a hard constraint, not a config toggle.
- **Respects robots.txt** on any site we crawl.

## Stack

- Python 3.11+, FastAPI, SQLite (via SQLModel)
- `sentence-transformers` (all-MiniLM-L6-v2) + FAISS for matching
- Anthropic API (Claude) for tailoring + reranking
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
  matching/       # FAISS embeddings + Claude reranking
  tailoring/      # Resume + cover letter generation
  autofill/       # Playwright-based form filler
  telegram_bot/   # Async handoff loop
  db/             # SQLModel schemas
  api/            # FastAPI endpoints
scripts/          # CLI entrypoints
data/             # Resume, FAISS index, generated docs
```

## Status

Scaffold for a personal workflow. Discovery + matching are the most complete; tailoring and autofill have working skeletons that should be tuned around Karthik's target companies and application preferences.
