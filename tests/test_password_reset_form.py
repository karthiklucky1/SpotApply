"""The "Forgot?" form must report a refused send.

supabase-js v2 never throws from `resetPasswordForEmail`; it RETURNS
`{ error }`. The form used to wrap the call in try/catch only, so a rate limit
or an unconfigured email provider still told the user "a reset link is on its
way" — and production had never sent a single recovery email
(`auth.users.recovery_sent_at` NULL for every account, 2026-09-26).
"""
from pathlib import Path
import re

AUTH = Path(__file__).resolve().parents[1] / "app" / "templates" / "auth.html"


def _forgot_handler() -> str:
    src = AUTH.read_text(encoding="utf-8")
    start = src.index("window.handleForgot")
    end = src.index("window.handleGoogle", start)
    return src[start:end]


def test_reset_reads_the_returned_error():
    body = _forgot_handler()
    assert re.search(r"=\s*await\s+sb\.auth\.resetPasswordForEmail", body), \
        "the result of resetPasswordForEmail must be kept, not discarded"
    assert ".error" in body


def test_success_message_is_not_unconditional():
    body = _forgot_handler()
    ok = body.index("reset link is on its way")
    guard = body.rfind("if (!error)", 0, ok)
    assert guard != -1, "the success message must be shown only when no error came back"


def test_failure_and_rate_limit_are_told_to_the_user():
    body = _forgot_handler()
    assert "429" in body and "Too many reset requests" in body
    assert "couldn't send the reset email" in body


def test_redirect_goes_to_the_reset_page():
    assert "window.location.origin + '/auth/reset'" in _forgot_handler()
