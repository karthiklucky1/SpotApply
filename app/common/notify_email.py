"""Email notifications — the dashboard bell, delivered to the inbox.

Why this exists (2026-10-15): notifications lived only in the dashboard bell,
which polls while the dashboard is open. Of 312 notifications created in the 30
days before this module, 2 were ever read — 225 fresh-job alerts to 15 users
drew 1 read, and all 25 "your search is paused" notices drew 0, because a
notice meant for someone who stopped visiting can only be read by visiting.

What it does: every ``EMAIL_DISPATCH_INTERVAL_MINUTES`` the lanes process runs
``dispatch_pending()``, which collects each user's UNREAD notifications of the
types in ``EMAIL_NOTIFICATION_TYPES`` (fresh_job, high_match, feed_paused by
default) created within ``EMAIL_MAX_AGE_HOURS``, and sends ONE email per user
summarising them. Rules, in the order they are checked:

  * never without ``RESEND_API_KEY`` and a link-signing key, and never while
    ``EMAIL_NOTIFICATIONS_ENABLED`` is off (kill switch);
  * never to a user who turned emails off or unsubscribed (``skipped``);
  * not while the user is on the dashboard (``last_active_at`` within
    ``EMAIL_SKIP_IF_ACTIVE_MINUTES``) — they see the bell; deferred, so the
    email still goes if they leave without reading it;
  * at most ``EMAIL_MAX_PER_USER_DAILY`` a day and ``EMAIL_MIN_GAP_MINUTES``
    apart — later notifications wait and are batched into the next one;
  * a platform-wide daily reservation (``EMAIL_PLATFORM_DAILY_CAP``, persisted
    in ``platform_counter``) so a burst can't run past the provider's plan.

Daily digest (2026-10-28, the default): a user on ``email_frequency`` "daily"
gets no per-match email — their fresh_job / high_match notifications are
marked ``digest`` and left to the bell — and instead ``dispatch_digests()``
sends ONE email a day, at the hour they chose LOCAL to their time zone, with
their best new shortlisted jobs (at most ``EMAIL_DIGEST_MAX_JOBS``, best fit
first, only jobs that reached the board since the last digest). No new jobs,
no email. Status notices (feed_paused) still go out as they happen.

The recipient is the CONFIRMED login email from Supabase Auth — never
``UserProfile.email``, which is the applicant address read from a resume (it
differed from the login for 12 of 26 users). No DB session is held across the
Auth lookup or the send. Every email carries a signed one-click unsubscribe
link, RFC 8058 ``List-Unsubscribe`` headers and the postal address.
"""
from __future__ import annotations

import hashlib
import hmac
import html as _html
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import quote

from app.config import settings

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_BASE_URL = "https://app.spotapply.ai"
PLATFORM_COUNTER = "email_notifications"
JOB_TYPES = ("fresh_job", "high_match")
MAX_ITEMS_SHOWN = 10
_SELECT_LIMIT = 500

FREQ_DAILY = "daily"
FREQ_INSTANT = "instant"
FREQUENCIES = (FREQ_DAILY, FREQ_INSTANT)
#: The hours Settings offers (any 0-23 is accepted by the route).
DIGEST_HOURS = {9: "Morning (9 AM)", 12: "Midday (12 PM)", 18: "Evening (6 PM)"}
#: A time zone for a user whose browser never told us one: their search
#: country's main zone. Falls back to US Eastern (most users).
_COUNTRY_TZ = {
    "united states": "America/New_York", "canada": "America/Toronto",
    "india": "Asia/Kolkata", "united kingdom": "Europe/London", "ireland": "Europe/Dublin",
    "germany": "Europe/Berlin", "france": "Europe/Paris", "netherlands": "Europe/Amsterdam",
    "spain": "Europe/Madrid", "portugal": "Europe/Lisbon", "poland": "Europe/Warsaw",
    "ukraine": "Europe/Kyiv", "australia": "Australia/Sydney", "singapore": "Asia/Singapore",
    "japan": "Asia/Tokyo", "philippines": "Asia/Manila", "pakistan": "Asia/Karachi",
    "nigeria": "Africa/Lagos", "brazil": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires", "mexico": "America/Mexico_City",
}
_FALLBACK_TZ = "America/New_York"


class EmailSendError(RuntimeError):
    """The provider refused or could not be reached."""


# ── configuration ───────────────────────────────────────────────────────────

def notification_types() -> set[str]:
    return {t.strip() for t in (settings.email_notification_types or "").split(",") if t.strip()}


def base_url() -> str:
    return (settings.public_base_url or "").rstrip("/") or DEFAULT_BASE_URL


def logo_url() -> str:
    return base_url() + "/static/apple-touch-icon.png"


def _link_key() -> Optional[bytes]:
    secret = (settings.email_link_secret or "").strip()
    if secret:
        return secret.encode("utf-8")
    srk = (settings.supabase_service_role_key or "").strip()
    if srk:
        # Derived, so a link never reveals anything about the key itself.
        return hashlib.sha256(b"spotapply-email-links-v1:" + srk.encode("utf-8")).digest()
    return None


def is_configured() -> bool:
    """True when emails can actually go out: switch on, API key, signing key."""
    return bool(settings.email_notifications_enabled
                and (settings.resend_api_key or "").strip()
                and _link_key() is not None)


# ── per-user schedule ───────────────────────────────────────────────────────

def frequency_of(profile) -> str:
    v = (getattr(profile, "email_frequency", None) or "").strip().lower()
    if v in FREQUENCIES:
        return v
    d = (settings.email_default_frequency or "").strip().lower()
    return d if d in FREQUENCIES else FREQ_DAILY


def digest_hour_of(profile) -> int:
    v = getattr(profile, "email_digest_hour", None)
    if isinstance(v, int) and 0 <= v <= 23:
        return v
    d = int(settings.email_digest_default_hour or 9)
    return d if 0 <= d <= 23 else 9


def valid_timezone(name) -> Optional[str]:
    """The IANA name when zoneinfo knows it, else None. Bounded: never a path."""
    name = str(name or "").strip()
    if not name or len(name) > 64 or ".." in name or name.startswith("/"):
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return name
    except Exception:
        return None


def timezone_of(profile):
    """The user's zone: what their browser said, else their search country's,
    else US Eastern. Always a usable tzinfo (UTC if zone data is missing)."""
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo
    candidates = [getattr(profile, "email_timezone", "") or ""]
    country = (getattr(profile, "preferred_country", "") or "").strip().lower()
    candidates += [_COUNTRY_TZ.get(country, ""), _FALLBACK_TZ]
    for name in candidates:
        if valid_timezone(name):
            return ZoneInfo(name)
    return _tz.utc


def digest_due(profile, now: datetime) -> Optional[str]:
    """The user's LOCAL date when today's digest is due now, else None: inside
    [hour, hour + EMAIL_DIGEST_WINDOW_HOURS) local time, and not yet sent today."""
    from datetime import timezone as _tz
    local = now.replace(tzinfo=_tz.utc).astimezone(timezone_of(profile))
    hour = digest_hour_of(profile)
    window = max(1, int(settings.email_digest_window_hours or 6))
    if not (hour <= local.hour < hour + window):
        return None
    day = local.date().isoformat()
    if (getattr(profile, "last_digest_day", "") or "") == day:
        return None
    return day


# ── unsubscribe links ───────────────────────────────────────────────────────

def unsubscribe_token(user_id: str) -> Optional[str]:
    key = _link_key()
    if not key or not user_id:
        return None
    return hmac.new(key, f"unsubscribe:{user_id}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify_unsubscribe(user_id: str, token: str) -> bool:
    expected = unsubscribe_token(user_id or "")
    if not (expected and token):
        return False
    # Bytes, so a non-ASCII token is a mismatch, not a TypeError (a 500).
    return hmac.compare_digest(expected.encode("utf-8"), str(token).encode("utf-8"))


def unsubscribe_url(user_id: str) -> Optional[str]:
    tok = unsubscribe_token(user_id)
    if not tok:
        return None
    return f"{base_url()}/email/unsubscribe?u={quote(user_id, safe='')}&t={tok}"


# ── provider + recipient ────────────────────────────────────────────────────

def send_email(to: str, subject: str, html_body: str, text_body: str,
               headers: Optional[dict] = None) -> str:
    """Send one email through Resend. Returns the provider's id; raises
    ``EmailSendError`` on any refusal. Never logs the address."""
    import httpx
    payload = {
        "from": settings.email_from,
        "to": [to],
        "subject": subject,
        "html": html_body,
        "text": text_body,
        "headers": headers or {},
    }
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to
    try:
        resp = httpx.post(RESEND_URL, json=payload, timeout=15.0,
                          headers={"Authorization": f"Bearer {settings.resend_api_key}"})
    except Exception as e:
        raise EmailSendError(f"resend unreachable: {type(e).__name__}") from e
    if resp.status_code >= 300:
        # Status + Resend's error NAME only: its message text can quote the
        # recipient's address, and this string is logged.
        try:
            name = str((resp.json() or {}).get("name") or "")[:60]
        except Exception:
            name = ""
        raise EmailSendError(f"resend {resp.status_code} {name}".strip())
    try:
        return str((resp.json() or {}).get("id") or "")
    except Exception:
        return ""


def recipient_for(user_id: str) -> Optional[str]:
    """The user's CONFIRMED login email from Supabase Auth; None when the
    account has no confirmed address. Raises when Auth could not be asked —
    the caller defers instead of skipping."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return None
    from app.db.supabase_client import service_client
    resp = service_client().auth.admin.get_user_by_id(user_id)
    user = getattr(resp, "user", resp)
    email = (getattr(user, "email", None) or "").strip()
    # email_confirmed_at ONLY: Auth's `confirmed_at` is the earlier of the email
    # and phone confirmations, so a confirmed phone would vouch for an address
    # nobody proved they own.
    confirmed = getattr(user, "email_confirmed_at", None)
    return email if (email and confirmed) else None


# ── content ─────────────────────────────────────────────────────────────────

def _job_label(item: dict) -> str:
    """'Backend Engineer at Stripe' out of a fresh_job / high_match message."""
    msg = (item.get("message") or "").strip()
    if item.get("type") == "fresh_job":
        head = msg.split(" — ")[0]
        return head.replace(" @ ", " at ").strip()
    if item.get("type") == "high_match":
        return msg.split(" matches your profile")[0].strip()
    return ""


def _abs_link(link: Optional[str]) -> str:
    link = (link or "").strip()
    if not link.startswith("/") or link.startswith("//"):
        link = "/dashboard"
    return base_url() + link


def subject_for(items: list[dict]) -> str:
    jobs = [i for i in items if i.get("type") in JOB_TYPES]
    if not jobs:
        if any(i.get("type") == "feed_paused" for i in items):
            return "Your SpotApply job search is paused"
        return items[0].get("title") or "News from SpotApply"
    if len(jobs) == 1:
        label = _job_label(jobs[0])
        return f"New job match: {label}"[:120] if label else "A new job match for you"
    return f"{len(jobs)} new job matches for you"


def compose(items: list[dict], unsub_url: str) -> tuple[str, str, str]:
    """(subject, html, text) for one user's batch. Everything is escaped."""
    esc = _html.escape
    subject = subject_for(items)
    shown, more = items[:MAX_ITEMS_SHOWN], max(0, len(items) - MAX_ITEMS_SHOWN)

    rows_html = "".join(
        '<tr><td style="padding:14px 0;border-bottom:1px solid #DCEAF6;">'
        f'<div style="font-weight:700;color:#0C2A3E;font-size:15px;">{esc(i.get("title") or "")}</div>'
        f'<div style="color:#21455C;font-size:14px;line-height:1.5;margin-top:4px;">{esc(i.get("message") or "")}</div>'
        f'<a href="{esc(_abs_link(i.get("link")))}" style="display:inline-block;margin-top:8px;color:#0369A1;font-weight:700;font-size:14px;">Open in SpotApply →</a>'
        '</td></tr>'
        for i in shown)
    more_html = (f'<p style="color:#4A6075;font-size:14px;">…and {more} more on your dashboard.</p>'
                 if more else "")
    lines = [subject, ""]
    for i in shown:
        lines += [i.get("title") or "", i.get("message") or "", _abs_link(i.get("link")), ""]
    if more:
        lines += [f"…and {more} more on your dashboard.", ""]
    body_html = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                 f'{rows_html}</table>{more_html}')
    return _assemble(subject, "", body_html, lines, "Open your dashboard", unsub_url)


def _ago(when: Optional[datetime], now: datetime) -> str:
    if when is None:
        return ""
    mins = max(0, int((now - when).total_seconds() // 60))
    if mins < 60:
        return "just now" if mins < 5 else f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def digest_subject(jobs: list[dict]) -> str:
    if len(jobs) == 1:
        j = jobs[0]
        label = f"{j.get('title') or 'A new job'} at {j.get('company') or 'a company'}"
        return f"Your new match today: {label}"[:120]
    return f"Your {len(jobs)} best new job matches today"


def compose_digest(jobs: list[dict], total_new: int, unsub_url: str,
                   now: Optional[datetime] = None) -> tuple[str, str, str]:
    """(subject, html, text) for one user's daily digest: their best new
    shortlisted jobs, best fit first. ``jobs`` items carry title, company,
    location, remote, score, posted (datetime|None) and link. Everything is
    escaped; links stay on our site."""
    esc = _html.escape
    now = now or datetime.utcnow()
    subject = digest_subject(jobs)
    more = max(0, total_new - len(jobs))
    intro = ("Picked from today's new jobs and checked against your resume. "
             "Tailor your resume and apply from your dashboard.")

    def meta(j: dict) -> str:
        bits = []
        place = "Remote" if j.get("remote") else (j.get("location") or "").strip()
        if place:
            bits.append(place[:60])
        if j.get("score") is not None:
            bits.append(f"{int(j['score'])}% fit")
        posted = _ago(j.get("posted"), now)
        if posted:
            bits.append(f"posted {posted}")
        return " · ".join(bits)

    rows_html = "".join(
        '<tr><td style="padding:14px 0;border-bottom:1px solid #DCEAF6;">'
        f'<div style="font-weight:700;color:#0C2A3E;font-size:15px;">{esc(j.get("title") or "")}</div>'
        f'<div style="color:#21455C;font-size:14px;margin-top:2px;">{esc(j.get("company") or "")}</div>'
        f'<div style="color:#4A6075;font-size:13px;margin-top:4px;">{esc(meta(j))}</div>'
        f'<a href="{esc(_abs_link(j.get("link")))}" style="display:inline-block;margin-top:8px;color:#0369A1;font-weight:700;font-size:14px;">View job →</a>'
        '</td></tr>'
        for j in jobs)
    more_html = (f'<p style="color:#4A6075;font-size:14px;">…and {more} more new '
                 f'match{"es" if more != 1 else ""} on your dashboard.</p>' if more else "")
    body_html = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                 f'{rows_html}</table>{more_html}')
    lines = [subject, ""]
    for j in jobs:
        lines += [f"{j.get('title') or ''} — {j.get('company') or ''}", meta(j),
                  _abs_link(j.get("link")), ""]
    if more:
        lines += [f"…and {more} more new match{'es' if more != 1 else ''} on your dashboard.", ""]
    return _assemble(subject, intro, body_html, lines, "Open your shortlist", unsub_url)


def _assemble(subject: str, intro: str, body_html: str, text_lines: list[str],
              cta: str, unsub_url: str) -> tuple[str, str, str]:
    """The one email frame: logo, heading, body, button, footer with the
    settings link, one-click unsubscribe and the postal address. ``body_html``
    is already escaped by the caller; ``intro`` and ``subject`` are escaped here."""
    esc = _html.escape
    dashboard = base_url() + "/dashboard"
    settings_url = base_url() + "/dashboard?settings=notifications"
    address = f"SpotApply LLC · {settings.business_address}, United States"
    intro_html = (f'<p style="color:#21455C;font-size:14px;line-height:1.5;margin:0 0 4px;">{esc(intro)}</p>'
                  if intro else "")
    html_body = (
        '<!doctype html><html><body style="margin:0;background:#F5FAFF;font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5FAFF;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background:#FFFFFF;border:1px solid #DCEAF6;border-radius:16px;">'
        '<tr><td style="padding:24px 24px 8px;">'
        # The app icon as a PNG on our own host: email clients do not render
        # SVG, and an image served from anywhere else would be a third party.
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="padding-right:10px;"><img src="{esc(logo_url())}" width="36" height="36" alt="SpotApply" '
        'style="display:block;border:0;border-radius:9px;"></td>'
        '<td style="font-weight:800;font-size:18px;color:#0077C2;">SpotApply</td>'
        '</tr></table>'
        f'<h1 style="font-size:20px;color:#0C2A3E;margin:12px 0 4px;">{esc(subject)}</h1>'
        f'{intro_html}'
        '</td></tr>'
        f'<tr><td style="padding:0 24px;">{body_html}</td></tr>'
        '<tr><td style="padding:16px 24px 24px;">'
        f'<a href="{esc(dashboard)}" style="display:inline-block;background:#BAE6FD;border:1px solid #7DD3FC;'
        'color:#0C2A3E;font-weight:700;font-size:14px;padding:12px 20px;border-radius:12px;text-decoration:none;">'
        f'{esc(cta)}</a>'
        '</td></tr></table>'
        '<p style="max-width:560px;color:#4A6075;font-size:12px;line-height:1.6;margin:16px auto 0;">'
        'You are getting this because you have a SpotApply account with email notifications on. '
        f'<a href="{esc(settings_url)}" style="color:#0369A1;">Email settings</a> · '
        f'<a href="{esc(unsub_url)}" style="color:#0369A1;">Unsubscribe</a><br>{esc(address)}</p>'
        '</td></tr></table></body></html>')

    lines = list(text_lines)
    if intro:
        lines[1:1] = [intro]
    lines += [f"{cta}: {dashboard}", "",
              "You are getting this because you have a SpotApply account with email notifications on.",
              f"Email settings: {settings_url}",
              f"Unsubscribe: {unsub_url}",
              address]
    return subject, html_body, "\n".join(lines)


def list_unsubscribe_headers(unsub_url: str) -> dict:
    """RFC 2369 + RFC 8058: mail clients show their own Unsubscribe button and
    POST to the URL (one click, no page)."""
    mailto = f"<mailto:{settings.email_reply_to}?subject=unsubscribe>" if settings.email_reply_to else ""
    value = f"<{unsub_url}>" + (f", {mailto}" if mailto else "")
    return {"List-Unsubscribe": value, "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


# ── the dispatcher ──────────────────────────────────────────────────────────

def _decide(profile, now: datetime) -> str:
    """'send' | 'skip' (never for these notifications) | 'defer' (try later)."""
    if profile is None:
        return "skip"
    if profile.email_notifications is False or profile.email_unsubscribed_at is not None:
        return "skip"
    active = profile.last_active_at
    if active is not None and now - active < timedelta(minutes=max(0, settings.email_skip_if_active_minutes)):
        return "defer"
    last = profile.last_notification_email_at
    if last is not None and now - last < timedelta(minutes=max(0, settings.email_min_gap_minutes)):
        return "defer"
    today = now.date().isoformat()
    if (profile.notification_email_day == today
            and (profile.notification_emails_today or 0) >= max(0, settings.email_max_per_user_daily)):
        return "defer"
    return "send"


def _mark(ids: list[int], state: str, now: datetime) -> None:
    if not ids:
        return
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserNotification
    with get_session() as session:
        for row in session.exec(select(UserNotification).where(UserNotification.id.in_(ids))).all():
            row.email_state = state
            row.emailed_at = now
            session.add(row)
        session.commit()


def _count_sent(user_id: str, now: datetime) -> None:
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserProfile
    today = now.date().isoformat()
    with get_session() as session:
        row = session.exec(select(UserProfile).where(UserProfile.user_id == user_id)).first()
        if row is None:
            return
        row.notification_emails_today = (row.notification_emails_today or 0) + 1 \
            if row.notification_email_day == today else 1
        row.notification_email_day = today
        row.last_notification_email_at = now
        session.add(row)
        session.commit()


def dispatch_pending(now: Optional[datetime] = None, *,
                     send: Optional[Callable[..., str]] = None,
                     lookup: Optional[Callable[[str], Optional[str]]] = None,
                     reserve: Optional[Callable[[], bool]] = None,
                     only_users: Optional[list[str]] = None) -> dict:
    """One pass. Returns aggregate counts only — nothing that names a person.
    ``only_users`` narrows the pass (tests; a manual resend for one account)."""
    stats = {"users": 0, "sent": 0, "skipped": 0, "deferred": 0, "failed": 0,
             "platform_capped": False, "configured": is_configured()}
    if not stats["configured"]:
        return stats
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserNotification, UserProfile

    send = send or send_email
    lookup = lookup or recipient_for
    if reserve is None:
        from app.common import daily_counter
        reserve = lambda: daily_counter.reserve(PLATFORM_COUNTER, settings.email_platform_daily_cap)  # noqa: E731
    now = now or datetime.utcnow()
    types = notification_types()
    if not types:
        return stats
    cutoff = now - timedelta(hours=max(1, settings.email_max_age_hours))

    with get_session() as session:
        q = (select(UserNotification.id, UserNotification.user_id, UserNotification.title,
                    UserNotification.message, UserNotification.link, UserNotification.type)
             .where(UserNotification.email_state.is_(None),
                    UserNotification.read == False,  # noqa: E712
                    UserNotification.type.in_(sorted(types)),
                    UserNotification.created_at >= cutoff,
                    UserNotification.user_id.is_not(None),
                    UserNotification.user_id != "local"))
        if only_users is not None:
            q = q.where(UserNotification.user_id.in_(list(only_users)))
        rows = session.exec(q.order_by(UserNotification.created_at).limit(_SELECT_LIMIT)).all()
        by_user: dict[str, list[dict]] = {}
        for r in rows:
            by_user.setdefault(r.user_id, []).append(
                {"id": r.id, "title": r.title, "message": r.message, "link": r.link, "type": r.type})
        profiles = {}
        if by_user:
            for p in session.exec(select(UserProfile).where(UserProfile.user_id.in_(list(by_user)))).all():
                session.expunge(p)
                profiles[p.user_id] = p

    for uid, items in by_user.items():
        stats["users"] += 1
        ids = [i["id"] for i in items]
        decision = _decide(profiles.get(uid), now)
        if decision == "skip":
            _mark(ids, "skipped", now)
            stats["skipped"] += 1
            continue
        if profiles.get(uid) is not None and frequency_of(profiles[uid]) == FREQ_DAILY:
            # Daily-digest users: new matches wait for the digest (it reads
            # the board, not these rows); only status notices go out now.
            jobs = [i["id"] for i in items if i.get("type") in JOB_TYPES]
            if jobs:
                _mark(jobs, "digest", now)
                stats["to_digest"] = stats.get("to_digest", 0) + len(jobs)
            items = [i for i in items if i.get("type") not in JOB_TYPES]
            ids = [i["id"] for i in items]
            if not items:
                continue
        if decision == "defer":
            stats["deferred"] += 1
            continue
        try:
            to = lookup(uid)
        except Exception as e:
            log.warning("Email notifications: recipient lookup failed (%s) — deferred",
                        type(e).__name__)
            stats["deferred"] += 1
            continue
        if not to:
            _mark(ids, "skipped", now)
            stats["skipped"] += 1
            continue
        unsub = unsubscribe_url(uid)
        if not unsub:
            stats["deferred"] += 1
            continue
        if not reserve():
            stats["platform_capped"] = True
            break
        subject, html_body, text_body = compose(items, unsub)
        try:
            send(to, subject, html_body, text_body, list_unsubscribe_headers(unsub))
        except Exception as e:
            log.warning("Email notifications: send failed: %s", str(e)[:200])
            _mark(ids, "failed", now)
            stats["failed"] += 1
            continue
        _mark(ids, "sent", now)
        _count_sent(uid, now)
        stats["sent"] += 1
    return stats


def _digest_jobs(user_id: str, since: datetime, limit: int) -> tuple[list[dict], int]:
    """(best new shortlisted jobs, how many new ones there are) since `since`:
    still SHORTLISTED, delivered by SpotApply (never an email import), posting
    still open. Best fit first, newest first on ties. Projected columns only."""
    from sqlalchemy import func
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import Application, ApplicationStatus, Job

    where = (Application.user_id == user_id,
             Application.status == ApplicationStatus.SHORTLISTED,
             Application.apply_track != "email_import",
             Application.created_at >= since,
             Job.is_closed == False)  # noqa: E712
    with get_session() as session:
        total = session.exec(
            select(func.count(Application.id)).join(Job, Application.job_id == Job.id).where(*where)
        ).one()
        total = int(total[0] if isinstance(total, tuple) else total)
        if not total:
            return [], 0
        rows = session.exec(
            select(Job.id, Job.title, Job.company, Job.location, Job.remote, Job.rerank_score,
                   Job.posted_at, Job.first_seen, Job.discovered_at)
            .join(Application, Application.job_id == Job.id)
            .where(*where)
            .order_by(Job.rerank_score.desc(), Application.created_at.desc())
            .limit(max(1, limit))
        ).all()
    jobs = [{"title": r.title, "company": r.company, "location": r.location,
             "remote": bool(r.remote), "score": r.rerank_score,
             "posted": r.posted_at or r.first_seen or r.discovered_at,
             "link": f"/dashboard?fresh_job={r.id}"} for r in rows]
    return jobs, total


def _stamp_digest(user_id: str, day: str, now: datetime) -> None:
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserProfile
    with get_session() as session:
        row = session.exec(select(UserProfile).where(UserProfile.user_id == user_id)).first()
        if row is None:
            return
        row.last_digest_day = day
        row.last_digest_at = now
        session.add(row)
        session.commit()


def dispatch_digests(now: Optional[datetime] = None, *,
                     send: Optional[Callable[..., str]] = None,
                     lookup: Optional[Callable[[str], Optional[str]]] = None,
                     reserve: Optional[Callable[[], bool]] = None,
                     only_users: Optional[list[str]] = None) -> dict:
    """The daily digest pass (runs beside ``dispatch_pending``). For each
    daily-digest user whose local hour has come and who has had no digest
    today: their best new shortlisted jobs since the last digest (at most a
    day back), one email. Nothing new = no email, and the next pass inside the
    window asks again. Aggregate counts only."""
    stats = {"due": 0, "sent": 0, "empty": 0, "skipped": 0, "deferred": 0, "failed": 0,
             "platform_capped": False, "configured": is_configured()}
    if not stats["configured"]:
        return stats
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserProfile

    send = send or send_email
    lookup = lookup or recipient_for
    if reserve is None:
        from app.common import daily_counter
        reserve = lambda: daily_counter.reserve(PLATFORM_COUNTER, settings.email_platform_daily_cap)  # noqa: E731
    now = now or datetime.utcnow()
    limit = max(1, int(settings.email_digest_max_jobs or 10))

    # Projected: the schedule columns only, never a whole profile row (resume
    # text, histories) for every user every ten minutes.
    from types import SimpleNamespace
    cols = (UserProfile.user_id, UserProfile.email_notifications, UserProfile.email_frequency,
            UserProfile.email_digest_hour, UserProfile.email_timezone,
            UserProfile.preferred_country, UserProfile.last_digest_day, UserProfile.last_digest_at)
    with get_session() as session:
        from sqlalchemy import or_
        daily = UserProfile.email_frequency == FREQ_DAILY
        if frequency_of(None) == FREQ_DAILY:
            daily = or_(daily, UserProfile.email_frequency.is_(None))
        q = (select(*cols)
             .where(UserProfile.user_id.is_not(None), UserProfile.user_id != "local",
                    UserProfile.email_unsubscribed_at.is_(None),
                    or_(UserProfile.email_notifications.is_(None),
                        UserProfile.email_notifications == True),  # noqa: E712
                    daily))
        if only_users is not None:
            q = q.where(UserProfile.user_id.in_(list(only_users)))
        profiles = [SimpleNamespace(**{c.key: v for c, v in zip(cols, row)})
                    for row in session.exec(q).all()]

    for prof in profiles:
        if prof.email_notifications is False or frequency_of(prof) != FREQ_DAILY:
            continue
        day = digest_due(prof, now)
        if day is None:
            continue
        stats["due"] += 1
        uid = prof.user_id
        since = max(now - timedelta(hours=24), prof.last_digest_at or (now - timedelta(hours=24)))
        try:
            jobs, total = _digest_jobs(uid, since, limit)
        except Exception as e:
            log.warning("Daily digest: job lookup failed (%s) — deferred", type(e).__name__)
            stats["deferred"] += 1
            continue
        if not jobs:
            stats["empty"] += 1
            continue
        try:
            to = lookup(uid)
        except Exception as e:
            log.warning("Daily digest: recipient lookup failed (%s) — deferred", type(e).__name__)
            stats["deferred"] += 1
            continue
        if not to:
            _stamp_digest(uid, day, now)     # no confirmed address: nothing to retry today
            stats["skipped"] += 1
            continue
        unsub = unsubscribe_url(uid)
        if not unsub:
            stats["deferred"] += 1
            continue
        if not reserve():
            stats["platform_capped"] = True
            break
        subject, html_body, text_body = compose_digest(jobs, total, unsub, now)
        # The day is recorded BEFORE the send: one attempt a day, never a retry
        # storm. Stamping after it meant a failed write re-sent the same digest
        # every pass for the rest of the window.
        try:
            _stamp_digest(uid, day, now)
        except Exception as e:
            log.warning("Daily digest: could not record the day (%s) — deferred", type(e).__name__)
            stats["deferred"] += 1
            continue
        try:
            send(to, subject, html_body, text_body, list_unsubscribe_headers(unsub))
        except Exception as e:
            log.warning("Daily digest: send failed: %s", str(e)[:200])
            stats["failed"] += 1
            continue
        try:
            _count_sent(uid, now)
        except Exception as e:                # the email went out; the counter is advisory
            log.warning("Daily digest: send counter not updated (%s)", type(e).__name__)
        stats["sent"] += 1
    return stats


def unsubscribe(user_id: str) -> bool:
    """Turn email notifications off for ``user_id``. True when a profile exists."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import UserProfile
    with get_session() as session:
        row = session.exec(select(UserProfile).where(UserProfile.user_id == user_id)).first()
        if row is None:
            return False
        row.email_notifications = False
        if row.email_unsubscribed_at is None:
            row.email_unsubscribed_at = datetime.utcnow()
        session.add(row)
        session.commit()
    return True
