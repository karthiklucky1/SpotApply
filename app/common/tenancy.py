"""Tenant identity helpers shared by the paths that can leak one user's data into another's.

`data/profiles/*.md` are the FOUNDER's résumé variants — real name, phone, email,
LinkedIn, GitHub — committed to the repo and shipped in the image. Several code
paths were written when SpotApply was a single-user agent and still treat those
files as "the résumé", with no tenant check. In a multi-tenant deployment that
turns them into another person's PII inside a paying user's deliverable.

`is_founder()` is the fail-closed gate for those paths: founder/local dev keeps
the historical single-user behaviour, everyone else is served strictly from
their own uploaded résumé. It mirrors `_is_autofill_founder` in
app/autofill/agent.py, which already applies exactly this rule to form-filling
identity — the same rule simply had not been applied to tailoring and review.
"""

from __future__ import annotations

from app.config import settings


def is_founder(user_id: str | None) -> bool:
    """True only for the local dev user or the configured founder account.

    `None` and `"local"` are the single-user/SQLite sentinels used throughout the
    codebase; both mean "there is no other tenant to leak to". Any real Supabase
    uid that is not `founder_user_id` returns False, and with `founder_user_id`
    unset (the default) EVERY real user is a non-founder — the safe direction.
    """
    if user_id is None or user_id == "local":
        return True
    return bool(settings.founder_user_id) and user_id == settings.founder_user_id
