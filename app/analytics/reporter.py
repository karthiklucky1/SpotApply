import logging
import httpx
from app.config import settings
from app.analytics.funnel import FunnelTracker

log = logging.getLogger(__name__)

class FunnelReporter:
    @staticmethod
    def generate_daily_report() -> str:
        summary = FunnelTracker.get_summary(days=1)  # last 24h
        
        disc = summary.get("discovered", 0)
        rule_f = summary.get("rule_filtered", {"passed": 0, "failed": 0})
        emb_f = summary.get("embedding_filtered", {"passed": 0, "failed": 0})
        scored = summary.get("scored", 0)
        shortlisted = summary.get("shortlisted", 0)
        tailored = summary.get("tailored", 0)
        applied = summary.get("applied", 0)
        responded = summary.get("responded", 0)
        
        rule_passed = rule_f["passed"]
        rule_failed = rule_f["failed"]
        emb_passed = emb_f["passed"]
        emb_failed = emb_f["failed"]
        
        msg = (
            "📊 *JobAgent Funnel Daily Report* 📊\n\n"
            f"🔍 *Discovered:* {disc} jobs\n"
            f"🚫 *Rule Filtered:* {rule_failed} rejected, {rule_passed} passed\n"
            f"📐 *Embedding Filtered:* {emb_failed} rejected, {emb_passed} passed\n"
            f"🤖 *LLM Scored:* {scored} scored ({shortlisted} shortlisted)\n"
            f"📝 *Tailored:* {tailored}\n"
            f"🚀 *Applied:* {applied}\n"
            f"💬 *Responded:* {responded}\n\n"
        )
        
        # Per-source breakdown
        sources = summary.get("sources", {})
        if sources:
            msg += "*Sourcing Breakdown:*\n"
            for src, count in sources.items():
                msg += f"- {src.title()}: {count} jobs\n"
            msg += "\n"
            
        # A/B Resume variant performance
        variants = FunnelTracker.get_variant_performance()
        if variants:
            msg += "*Resume Variant Performance:*\n"
            for var, metrics in variants.items():
                app_cnt = metrics["applied"]
                resp_cnt = metrics["responded"]
                rate = (resp_cnt / app_cnt * 100) if app_cnt > 0 else 0
                msg += f"- *{var}*: {app_cnt} apps, {resp_cnt} responses ({rate:.1f}% rate)\n"
                
        return msg

    @staticmethod
    def send_daily_report() -> None:
        """Fetch statistics and send the report to the Telegram chat."""
        try:
            msg = FunnelReporter.generate_daily_report()
            if not settings.telegram_bot_token or not settings.telegram_chat_id:
                log.warning("Telegram credentials missing. Skipping report sending.")
                return
            
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            resp = httpx.post(
                url, 
                json={
                    "chat_id": settings.telegram_chat_id, 
                    "text": msg, 
                    "parse_mode": "Markdown"
                }, 
                timeout=10
            )
            if resp.status_code == 200:
                log.info("Daily funnel report sent successfully.")
            else:
                log.error("Failed to send daily funnel report: %s", resp.text)
        except Exception as e:
            log.error("Error generating/sending daily funnel report: %s", e)
