"""Private Telegram handoff bot for the owner.

Two flows:
1. Outbound: when autofill hits an unknown field, the agent enqueues a
   PendingQuestion and the bot DMs the owner with the question + context.
2. Inbound: your reply gets matched to the open question, stored as the answer,
   and cached in AnswerMemory so the next time the same question shows up it
   auto-fills.

Inbound-message matching uses the most recent unanswered PendingQuestion for
the configured TELEGRAM_CHAT_ID. If multiple are queued, the bot asks them one
at a time.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlmodel import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application as TgApplication,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.autofill.agent import autofill
from app.config import settings
from app.db.init_db import get_session
from app.db.models import (
    AnswerMemory,
    Application,
    ApplicationStatus,
    Job,
    PendingQuestion,
)
from app.discovery.pipeline import run_discovery
from app.matching.pipeline import run_matching
from app.tailoring.tailor import tailor_all_shortlisted

log = logging.getLogger(__name__)

# Common yes/no options for typical HR questions
_YES_NO_KEYWORDS = [
    "require visa", "visa sponsorship", "sponsorship", "relocation", "open to reloc",
    "interviewed before", "open to working", "in-person", "authorized to work",
    "legally authorized", "require employment",
]
_YES_NO_BUTTONS = [["Yes", "No"]]
_OPEN_TO_BUTTONS = [["Yes, happy to", "No, remote only", "Flexible / hybrid"]]


def _owner_chat_id() -> int | None:
    if not settings.telegram_chat_id:
        return None
    try:
        return int(settings.telegram_chat_id)
    except ValueError:
        log.warning("TELEGRAM_CHAT_ID must be an integer; got %r", settings.telegram_chat_id)
        return None


async def _reject_if_not_owner(update: Update) -> bool:
    """Return True when this update should be ignored."""
    owner = _owner_chat_id()
    chat = update.effective_chat
    if owner is None or chat is None or chat.id == owner:
        return False

    log.warning("Rejected Telegram update from non-owner chat_id=%s", chat.id)
    if update.callback_query:
        await update.callback_query.answer("This bot is private.", show_alert=True)
    elif update.message:
        await update.message.reply_text("This JobAgent bot is private.")
    return True


def _detect_buttons(label: str, options_json: str | None) -> list[list[str]] | None:
    """Return button rows for a question label, or None if free text."""
    low = label.lower()

    # Explicit options from DB (select/radio)
    if options_json:
        try:
            opts = json.loads(options_json)
            if opts:
                # Group into rows of 3
                rows = [opts[i:i+3] for i in range(0, len(opts), 3)]
                return rows
        except Exception:
            pass

    # Heuristic: yes/no type
    if any(kw in low for kw in _YES_NO_KEYWORDS):
        return _YES_NO_BUTTONS
    if "open to" in low or "willing to" in low:
        return _OPEN_TO_BUTTONS
    if "have you ever" in low or "did you" in low or "do you" in low:
        return _YES_NO_BUTTONS

    return None  # free-text answer


def _make_keyboard(button_rows: list[list[str]]) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(text, callback_data=f"ans:{text}") for text in row]
        for row in button_rows
    ]
    return InlineKeyboardMarkup(keyboard)


async def _send_next_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    with get_session() as session:
        pq = session.exec(
            select(PendingQuestion).where(PendingQuestion.answer.is_(None))
        ).first()
        if not pq:
            return False

        # Count remaining
        total_unanswered = len(session.exec(
            select(PendingQuestion)
            .where(PendingQuestion.application_id == pq.application_id)
            .where(PendingQuestion.answer.is_(None))
        ).all())

        app = session.get(Application, pq.application_id)
        job = session.get(Job, app.job_id)

        msg = (
            f"📋 *{job.company}* — {job.title}\n\n"
            f"*{pq.field_label}*\n\n"
            f"_{total_unanswered} question(s) remaining_"
        )

        button_rows = _detect_buttons(pq.field_label, pq.options)
        if button_rows:
            # Add a "Type my own" option at the bottom
            keyboard = _make_keyboard(button_rows)
            await context.bot.send_message(
                chat_id=chat_id, text=msg, parse_mode="Markdown", reply_markup=keyboard
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg + "\n\n_(Type your answer below)_",
                parse_mode="Markdown",
            )
    return True


async def _save_answer(pq_id: int, answer: str) -> tuple[bool, int]:
    """Save answer to PendingQuestion and AnswerMemory. Returns (all_done, remaining_count)."""
    with get_session() as session:
        pq = session.get(PendingQuestion, pq_id)
        if not pq:
            return True, 0
        pq.answer = answer
        pq.answered_at = datetime.utcnow()
        session.add(pq)

        # Cache in AnswerMemory
        norm = pq.field_label.lower().strip()
        existing = session.exec(
            select(AnswerMemory).where(AnswerMemory.label_normalized == norm)
        ).first()
        if existing:
            existing.answer = answer
            existing.use_count += 1
            existing.last_used_at = datetime.utcnow()
            session.add(existing)
        else:
            session.add(AnswerMemory(
                label_normalized=norm,
                label_original=pq.field_label,
                answer=answer,
            ))

        # Check remaining
        remaining = session.exec(
            select(PendingQuestion).where(
                PendingQuestion.application_id == pq.application_id,
                PendingQuestion.answer.is_(None),
            )
        ).all()
        if not remaining:
            app = session.get(Application, pq.application_id)
            app.status = ApplicationStatus.READY_TO_SUBMIT
            session.add(app)
        session.commit()
        return len(remaining) == 0, len(remaining)


# ---- handlers ----

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_not_owner(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"JobAgent online. Your chat_id is `{chat_id}`. "
        f"Add it to TELEGRAM_CHAT_ID in your .env if you haven't.",
        parse_mode="Markdown",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_not_owner(update):
        return
    with get_session() as session:
        counts = {}
        for st in ApplicationStatus:
            n = session.exec(select(Application).where(Application.status == st)).all()
            counts[st.value] = len(n)
        lines = [f"• {k}: {v}" for k, v in counts.items() if v]
    await update.message.reply_text("Application status:\n" + "\n".join(lines or ["(none yet)"]))


async def next_q(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_not_owner(update):
        return
    sent = await _send_next_question(context, update.effective_chat.id)
    if not sent:
        await update.message.reply_text("No pending questions. ✅")


async def unskip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restore a skipped application back to AWAITING_USER."""
    if await _reject_if_not_owner(update):
        return
    with get_session() as session:
        app = session.exec(
            select(Application).where(Application.status == ApplicationStatus.SKIPPED)
        ).first()
        if not app:
            await update.message.reply_text("No skipped applications found.")
            return
        job = session.get(Job, app.job_id)
        app.status = ApplicationStatus.AWAITING_USER
        session.add(app)
        # Un-skip all pending questions for this app
        pqs = session.exec(
            select(PendingQuestion).where(PendingQuestion.application_id == app.id)
        ).all()
        restored = 0
        for pq in pqs:
            if pq.answer == "[SKIPPED]":
                pq.answer = None
                pq.answered_at = None
                session.add(pq)
                restored += 1
        session.commit()
        await update.message.reply_text(
            f"✅ Restored *{job.company}* — {job.title}\n"
            f"_{restored} question(s) back in queue._\n\n"
            f"Send /next to continue answering.",
            parse_mode="Markdown",
        )


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_not_owner(update):
        return
    with get_session() as session:
        pq = session.exec(select(PendingQuestion).where(PendingQuestion.answer.is_(None))).first()
        if not pq:
            await update.message.reply_text("Nothing to skip.")
            return
        app = session.get(Application, pq.application_id)
        app.status = ApplicationStatus.SKIPPED
        session.add(app)
        for q in session.exec(
            select(PendingQuestion).where(PendingQuestion.application_id == app.id)
        ).all():
            if q.answer is None:
                q.answer = "[SKIPPED]"
                q.answered_at = datetime.utcnow()
                session.add(q)
        session.commit()
    await update.message.reply_text(
        "Skipped. Send /unskip to restore it later, or /next for the next question."
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    if await _reject_if_not_owner(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("ans:"):
        return
    answer = data[4:]

    with get_session() as session:
        pq = session.exec(
            select(PendingQuestion).where(PendingQuestion.answer.is_(None))
        ).first()
        if not pq:
            await query.edit_message_text("No pending question to answer.")
            return
        pq_id = pq.id

    all_done, remaining = await _save_answer(pq_id, answer)
    await query.edit_message_text(f"✅ Saved: *{answer}*", parse_mode="Markdown")

    if all_done:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🎉 All questions answered! Application is *Ready to Submit*.\nOpen the dashboard and click Verify Form.",
            parse_mode="Markdown",
        )
    else:
        sent = await _send_next_question(context, query.message.chat_id)
        if not sent:
            await context.bot.send_message(chat_id=query.message.chat_id, text="All caught up! ✅")


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-text replies."""
    if await _reject_if_not_owner(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    with get_session() as session:
        pq = session.exec(
            select(PendingQuestion).where(PendingQuestion.answer.is_(None))
        ).first()
        if not pq:
            await update.message.reply_text(
                "No pending question to answer. Use /next to fetch one."
            )
            return
        pq_id = pq.id

    all_done, remaining = await _save_answer(pq_id, text)
    await update.message.reply_text(f"Got it ✅  _{remaining} question(s) remaining._", parse_mode="Markdown")

    if all_done:
        await update.message.reply_text(
            "🎉 *All questions answered!* Application is *Ready to Submit*.\n"
            "Open the dashboard and click Verify Form.",
            parse_mode="Markdown",
        )
    else:
        sent = await _send_next_question(context, update.effective_chat.id)
        if not sent:
            await update.message.reply_text("All caught up! ✅")


async def list_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List top shortlisted/tailored applications."""
    if await _reject_if_not_owner(update):
        return
    with get_session() as session:
        rows = session.exec(
            select(Application, Job)
            .join(Job)
            .where(Application.status.in_([
                ApplicationStatus.SHORTLISTED,
                ApplicationStatus.TAILORED,
                ApplicationStatus.AUTOFILLED,
                ApplicationStatus.AWAITING_USER,
                ApplicationStatus.READY_TO_SUBMIT,
            ]))
            .order_by(Job.rerank_score.desc())
        ).all()
    if not rows:
        await update.message.reply_text("No shortlisted applications yet. Use /discover then /match.")
        return
    lines = ["*Top Applications:*\n"]
    for app_row, job_row in rows[:10]:
        score = job_row.rerank_score or 0
        lines.append(
            f"• *{job_row.company}* — {job_row.title}\n"
            f"  Score: {score:.0f} | {app_row.status.value} | ID: `{app_row.id}`"
        )
    lines.append("\nUse `/apply <id>` to start autofill.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def apply_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/apply <application_id> — trigger autofill for one application."""
    if await _reject_if_not_owner(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /apply <application_id>")
        return
    app_id = int(context.args[0])
    await update.message.reply_text(f"Starting autofill for application `{app_id}`… 🤖", parse_mode="Markdown")
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, autofill, app_id)


def _notify_owner_sync(msg: str) -> None:
    """Helper to send telegram notifications outside event loops."""
    try:
        import httpx
        if settings.telegram_bot_token and settings.telegram_chat_id:
            httpx.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
    except Exception as e:
        log.warning("Telegram notification failed: %s", e)


async def cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/discover — kick off job discovery now."""
    if await _reject_if_not_owner(update):
        return
    await update.message.reply_text("Discovery started… this takes a few minutes. ⚙️")
    
    def _run():
        try:
            new_jobs = run_discovery()
            _notify_owner_sync(f"✅ *Discovery complete!*\nFound *{new_jobs}* new jobs.")
        except Exception as e:
            log.exception("Discovery failed: %s", e)
            _notify_owner_sync(f"❌ *Discovery failed*:\n{e}")

    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run)


async def cmd_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/match — run matching + tailor all shortlisted."""
    if await _reject_if_not_owner(update):
        return
    await update.message.reply_text("Matching + tailoring started… ⚙️")

    def _run():
        try:
            shortlisted_ids = run_matching()
            tailored_count = tailor_all_shortlisted()
            _notify_owner_sync(
                f"✅ *Matching & Tailoring complete!*\n"
                f"• Shortlisted: *{len(shortlisted_ids)}* jobs\n"
                f"• Tailored: *{tailored_count}* applications"
            )
        except Exception as e:
            log.exception("Matching + tailoring failed: %s", e)
            _notify_owner_sync(f"❌ *Matching & Tailoring failed*:\n{e}")

    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run)


def build_app() -> TgApplication:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")
    app = TgApplication.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("jobs", list_jobs))
    app.add_handler(CommandHandler("apply", apply_job))
    app.add_handler(CommandHandler("discover", cmd_discover))
    app.add_handler(CommandHandler("match", cmd_match))
    app.add_handler(CommandHandler("next", next_q))
    app.add_handler(CommandHandler("skip", skip))
    app.add_handler(CommandHandler("unskip", unskip))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer))
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = build_app()
    log.info("Telegram bot starting…")
    app.run_polling()
