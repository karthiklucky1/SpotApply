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

from app.autofill.agent import autofill, preview
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

# Tracks which application's questions the bot is currently asking.
# Prevents answers from crossing into a different application when two
# autofill runs queue pending questions concurrently.
_active_application_id: int | None = None

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
    
    yes_no_verbs = ["are you", "is your", "do you", "did you", "have you", "will you", "can you", "would you", "should you", "were you", "has you"]
    if any(low.startswith(p) for p in yes_no_verbs) or "have you ever" in low or "yes/no" in low or "yes or no" in low:
        return _YES_NO_BUTTONS

    return None  # free-text answer


def _make_keyboard(button_rows: list[list[str]]) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(text, callback_data=f"ans:{text}") for text in row]
        for row in button_rows
    ]
    return InlineKeyboardMarkup(keyboard)


async def _send_next_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    global _active_application_id
    with get_session() as session:
        # Prefer questions for the currently active application; fall back to any unanswered
        query = select(PendingQuestion).where(PendingQuestion.answer.is_(None))
        if _active_application_id is not None:
            query = query.where(PendingQuestion.application_id == _active_application_id)
        pq = session.exec(query).first()
        if pq is None and _active_application_id is not None:
            # Active app is done — pick the next available from any app
            _active_application_id = None
            pq = session.exec(
                select(PendingQuestion).where(PendingQuestion.answer.is_(None))
            ).first()
        if not pq:
            return False

        _active_application_id = pq.application_id

        # Count remaining for this application
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
    global _active_application_id
    from sqlalchemy.exc import IntegrityError
    with get_session() as session:
        pq = session.get(PendingQuestion, pq_id)
        if not pq:
            return True, 0
        pq.answer = answer
        pq.answered_at = datetime.utcnow()
        session.add(pq)

        # Cache in AnswerMemory (upsert with IntegrityError guard for concurrent
        # fills), scoped to the application's owner so one user's answers never
        # leak into another user's fills (matches POST /api/save-answer).
        owner_app = session.get(Application, pq.application_id)
        owner_id = owner_app.user_id if owner_app else None
        norm = pq.field_label.lower().strip()

        def _find_existing():
            q = select(AnswerMemory).where(AnswerMemory.label_normalized == norm)
            q = q.where(AnswerMemory.user_id == owner_id) if owner_id else q.where(AnswerMemory.user_id.is_(None))
            return session.exec(q).first()

        existing = _find_existing()
        if existing:
            existing.answer = answer
            existing.use_count += 1
            existing.last_used_at = datetime.utcnow()
            session.add(existing)
        else:
            try:
                session.add(AnswerMemory(
                    user_id=owner_id,
                    label_normalized=norm,
                    label_original=pq.field_label,
                    answer=answer,
                ))
                session.flush()
            except IntegrityError:
                session.rollback()
                existing = _find_existing()
                if existing:
                    existing.answer = answer
                    existing.use_count += 1
                    existing.last_used_at = datetime.utcnow()
                    session.add(existing)

        # Check remaining for this application only
        remaining = session.exec(
            select(PendingQuestion).where(
                PendingQuestion.application_id == pq.application_id,
                PendingQuestion.answer.is_(None),
            )
        ).all()
        if not remaining:
            app = session.get(Application, pq.application_id)
            app.status = ApplicationStatus.READY_TO_SUBMIT
            app.updated_at = datetime.utcnow()
            session.add(app)
            _active_application_id = None  # unlock for next application
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
        app.updated_at = datetime.utcnow()
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
    global _active_application_id
    if await _reject_if_not_owner(update):
        return
    with get_session() as session:
        q = select(PendingQuestion).where(PendingQuestion.answer.is_(None))
        if _active_application_id is not None:
            q = q.where(PendingQuestion.application_id == _active_application_id)
        pq = session.exec(q).first()
        if not pq:
            await update.message.reply_text("Nothing to skip.")
            return
        app = session.get(Application, pq.application_id)
        app.status = ApplicationStatus.SKIPPED
        app.updated_at = datetime.utcnow()
        _active_application_id = None
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

    if data.startswith("review:approve:"):
        app_id = int(data.split(":")[-1])
        from app.autofill.agent import _pending_events, _event_data, _pending_event_loops
        event_id = f"review_{app_id}"
        if event_id in _pending_events:
            _event_data[event_id] = "approve"
            ev = _pending_events[event_id]
            lp = _pending_event_loops.get(event_id)
            if lp and lp.is_running():
                lp.call_soon_threadsafe(ev.set)
            else:
                ev.set()
            await query.edit_message_caption("✅ *Approved!* Submitting application...", parse_mode="Markdown")
        else:
            await query.edit_message_caption("⚠️ *Timeout or already processed.*", parse_mode="Markdown")
        return
    elif data.startswith("review:reject:"):
        app_id = int(data.split(":")[-1])
        from app.autofill.agent import _pending_events, _event_data, _pending_event_loops
        event_id = f"review_{app_id}"
        if event_id in _pending_events:
            _event_data[event_id] = "reject"
            ev = _pending_events[event_id]
            lp = _pending_event_loops.get(event_id)
            if lp and lp.is_running():
                lp.call_soon_threadsafe(ev.set)
            else:
                ev.set()
            await query.edit_message_caption("❌ *Rejected.* Aborting application...", parse_mode="Markdown")
        else:
            await query.edit_message_caption("⚠️ *Timeout or already processed.*", parse_mode="Markdown")
        return
    elif data.startswith("captcha:solve:"):
        app_id = int(data.split(":")[-1])
        preview(app_id)
        await query.edit_message_caption("🔓 *Opening browser window* on your computer to solve the CAPTCHA...", parse_mode="Markdown")
        return
    elif data.startswith("captcha:solved:"):
        app_id = int(data.split(":")[-1])
        from app.autofill.agent import _pending_events, _event_data, _pending_event_loops
        event_id = f"captcha_{app_id}"
        if event_id in _pending_events:
            _event_data[event_id] = "solved"
            ev = _pending_events[event_id]
            lp = _pending_event_loops.get(event_id)
            if lp and lp.is_running():
                lp.call_soon_threadsafe(ev.set)
            else:
                ev.set()
            await query.edit_message_caption("🔓 *Solved.* Continuing...", parse_mode="Markdown")
        else:
            await query.edit_message_caption("⚠️ *Timeout or already processed.*", parse_mode="Markdown")
        return

    if not data.startswith("ans:"):
        return
    answer = data[4:]

    with get_session() as session:
        q = select(PendingQuestion).where(PendingQuestion.answer.is_(None))
        if _active_application_id is not None:
            q = q.where(PendingQuestion.application_id == _active_application_id)
        pq = session.exec(q).first()
        if not pq:
            await query.edit_message_text("No pending question to answer.")
            return
        pq_id = pq.id
        app_id = pq.application_id

    all_done, remaining = await _save_answer(pq_id, answer)
    await query.edit_message_text(f"✅ Saved: *{answer}*", parse_mode="Markdown")

    if all_done:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🎉 *All questions answered!* Resuming autofill and preparing pre-submit review...",
            parse_mode="Markdown",
        )
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, autofill, app_id, True)
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
        q = select(PendingQuestion).where(PendingQuestion.answer.is_(None))
        if _active_application_id is not None:
            q = q.where(PendingQuestion.application_id == _active_application_id)
        pq = session.exec(q).first()
        if not pq:
            await update.message.reply_text(
                "No pending question to answer. Use /next to fetch one."
            )
            return
        pq_id = pq.id
        app_id = pq.application_id

    all_done, remaining = await _save_answer(pq_id, text)
    await update.message.reply_text(f"Got it ✅  _{remaining} question(s) remaining._", parse_mode="Markdown")

    if all_done:
        await update.message.reply_text(
            "🎉 *All questions answered!* Resuming autofill and preparing pre-submit review...",
            parse_mode="Markdown",
        )
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, autofill, app_id, True)
    else:
        sent = await _send_next_question(context, update.effective_chat.id)
        if not sent:
            await update.message.reply_text("All caught up! ✅")


async def list_manual_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/manual — list manual-track applications waiting for human apply."""
    if await _reject_if_not_owner(update):
        return
    with get_session() as session:
        rows = session.exec(
            select(Application, Job)
            .join(Job)
            .where(Application.apply_track == "manual")
            .where(Application.status.in_([
                ApplicationStatus.SHORTLISTED,
                ApplicationStatus.TAILORED,
                ApplicationStatus.AWAITING_USER,
            ]))
            .order_by(Application.updated_at.desc())
        ).all()
    if not rows:
        await update.message.reply_text("No manual-track applications pending. ✅")
        return
    lines = [f"*Manual Apply Queue* ({len(rows)} jobs):\n"]
    for app_row, job_row in rows[:15]:
        score = job_row.rerank_score or 0
        source = job_row.source.value.upper()
        apply_url = app_row.apply_url or job_row.url
        lines.append(
            f"• [{job_row.company} — {job_row.title}]({apply_url})\n"
            f"  Score: {score:.0f} | {source} | ID: `{app_row.id}`"
        )
    lines.append("\nClick a link to open the application page directly.")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


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
            .order_by(Job.discovered_at.desc())
        ).all()
    if not rows:
        await update.message.reply_text("No shortlisted applications yet. Use /discover then /match.")
        return
    lines = ["*Top Applications:*\n"]
    for app_row, job_row in rows[:10]:
        rerank = job_row.rerank_score or 0
        senior = app_row.senior_fit_score
        score_str = f"{rerank:.0f}"
        if senior is not None:
            score_str = f"rerank:{rerank:.0f} | sr:{senior:.0f}"
        profile_tag = f" | `{app_row.profile_variant}`" if app_row.profile_variant else ""
        date_str = (job_row.posted_at or job_row.discovered_at).strftime("%b %d")
        lines.append(
            f"• *{job_row.company}* — {job_row.title}\n"
            f"  {score_str}{profile_tag} | {app_row.status.value} | ID: `{app_row.id}` | 📅 {date_str}"
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
    loop.run_in_executor(None, autofill, app_id, True)


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
    app.add_handler(CommandHandler("manual", list_manual_jobs))
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
