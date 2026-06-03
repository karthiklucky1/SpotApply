"""Single-process orchestrator: FastAPI + scheduler + Telegram bot.

For Karthik's personal job-search workflow. The API binds to localhost by
default; the Telegram bot is the external handoff surface.
"""
from __future__ import annotations

import asyncio
import logging
import threading

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from app.api.server import app as fastapi_app
from app.config import settings
from app.discovery.pipeline import run_discovery
from app.matching.pipeline import run_matching
from app.tailoring.tailor import tailor_all_shortlisted
from app.telegram_bot.bot import build_app as build_tg

log = logging.getLogger(__name__)


def run_harvester_sync():
    import asyncio
    from app.discovery.registry import harvest_common_crawl
    try:
        asyncio.run(harvest_common_crawl())
    except Exception as e:
        log.error("Registry weekly harvest job failed: %s", e)


def run_validator_sync():
    import asyncio
    from app.discovery.registry import run_validation_loop
    try:
        asyncio.run(run_validation_loop())
    except Exception as e:
        log.error("Registry daily validator job failed: %s", e)


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(daemon=True)
    # Discovery every 6h
    sched.add_job(run_discovery, "interval", hours=6, id="discovery")
    # Matching daily at 7am local
    sched.add_job(run_matching, "cron", hour=7, minute=0, id="matching")
    # Tailoring 30 min after matching
    sched.add_job(tailor_all_shortlisted, "cron", hour=7, minute=30, id="tailoring")
    # Harvester weekly (Sundays at 2 AM)
    sched.add_job(run_harvester_sync, "cron", day_of_week="sun", hour=2, minute=0, id="harvester")
    # Validator daily (Daily at 3 AM)
    sched.add_job(run_validator_sync, "cron", hour=3, minute=0, id="validator")
    sched.start()
    log.info("Scheduler started.")
    return sched


def start_api() -> None:
    uvicorn.run(fastapi_app, host=settings.api_host, port=settings.api_port, log_level="info")


def start_bot() -> None:
    """python-telegram-bot needs its own event loop on a non-main thread."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    tg = build_tg()
    tg.run_polling(close_loop=False, stop_signals=())


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    start_scheduler()
    # Bot on a worker thread
    threading.Thread(target=start_bot, daemon=True, name="tg-bot").start()
    # API on main thread (blocking)
    start_api()


if __name__ == "__main__":
    main()
