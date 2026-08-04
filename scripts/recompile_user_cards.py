#!/usr/bin/env python3
"""Force-recompile every stale (pre-v2) UserCard NOW instead of "on next score".

Why this exists (2026-08-04 CardRace audit, finding #4): a version bump only
recompiles a user's card the next time their jobs get scored — so INACTIVE
users never migrate, and every shadow row written for them keeps measuring the
OLD candidate representation (v1 = bare skill tokens, no claims). At audit time
8 of 10 cards were still v1, which means most of the ledger's clean-population
disagreement is measured against a representation that has already been
replaced. Recompiling all of them at once (~$0.005/card ≈ $0.04 total) gives
the ledger ONE seam instead of a per-user smear, which is what makes
`build_calibration --since <seam-date>` meaningful.

Read-only until you pass --yes. Skips users with no resume (nothing to
compile). Prints the seam date to pass to build_calibration afterwards.

    python -m scripts.recompile_user_cards            # report only
    python -m scripts.recompile_user_cards --yes      # actually recompile
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from sqlmodel import select

from app.db.init_db import get_session, init_db
from app.db.models import UserCardRow, UserProfile


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually recompile (default: report)")
    args = ap.parse_args()

    init_db()
    from app.matching.cards import USER_CARD_VERSION, get_or_compile_user_card

    with get_session() as session:
        rows = session.exec(select(UserCardRow)).all()
        profiles = {p.user_id: p for p in session.exec(select(UserProfile)).all()}
        for r in rows:
            session.expunge(r)

    stale = [r for r in rows if r.version != USER_CARD_VERSION]
    print(f"UserCards: {len(rows)} total, {len(stale)} stale (version != {USER_CARD_VERSION})")
    for r in stale:
        print(f"  {r.user_id}: v{r.version}")
    if not stale:
        print("Nothing to do.")
        return 0
    if not args.yes:
        print(f"\nDry run. Re-run with --yes to recompile {len(stale)} card(s) "
              f"(~${0.005 * len(stale):.2f}).")
        return 0

    from app.matching.pipeline import _load_resume
    ok = failed = skipped = 0
    for r in stale:
        uid = r.user_id
        prof = profiles.get(uid)
        try:
            resume = _load_resume(user_id=None if uid == "local" else uid)
        except Exception as e:
            print(f"  {uid}: no resume ({e}) — skipped (card stays v{r.version})")
            skipped += 1
            continue
        try:
            # get_or_compile sees the version mismatch and recompiles in place.
            card = get_or_compile_user_card(uid, prof, resume, allow_mint=True)
            if card and card.get("_version") == USER_CARD_VERSION:
                claims = len(card.get("evidence") or [])
                print(f"  {uid}: recompiled -> v{USER_CARD_VERSION} ({claims} claims)")
                ok += 1
            else:
                print(f"  {uid}: compile returned {'no card' if not card else 'stale version'} "
                      "(mint cap or LLM failure) — will retry on next score")
                failed += 1
        except Exception as e:
            print(f"  {uid}: FAILED ({e})")
            failed += 1

    seam = datetime.utcnow().strftime("%Y-%m-%d")
    print(f"\nDone: {ok} recompiled, {failed} failed, {skipped} skipped.")
    if ok:
        print(f"THE SEAM IS NOW {seam}. Shadow rows from before today mix old cards "
              f"and the old Tier-2 contract; calibrate with:\n"
              f"  python -m scripts.build_calibration --since {seam} --dry-run")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
