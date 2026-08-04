"""CLI: one matching pass.

    python -m scripts.run_matching --user <supabase-user-id>
    python -m scripts.run_matching                # local dev: data/resume_master.md

THIS SPENDS MONEY. A pass runs the full cascade — prescore then Claude finals —
and writes scores, applications and shadow rows. It is not a status command.

Without --user the résumé comes from ``settings.resume_path``, which exists in
local dev and NOT in the deployed container, so a bare run there dies at the
résumé load. Pass --user to pull that tenant's résumé from Supabase Storage.
"""
from __future__ import annotations

import argparse
import logging


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", metavar="USER_ID",
                    help="tenant to score for; omit for the local dev résumé file")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (for non-interactive runs)")
    args = ap.parse_args()

    # argparse has already handled --help by here. Everything that costs money
    # lives below this line and behind a __main__ guard: this module used to run
    # a full pass at IMPORT time, so `--help` — and any import of the module —
    # started scoring jobs before argparse ever saw the flag.
    if not args.yes:
        who = args.user or "local dev résumé"
        resp = input(f"Run a full matching pass for {who}? This calls the LLM "
                     f"and writes to the DB. [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted — nothing ran.")
            return 1

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    from app.matching.pipeline import run_matching

    ids = run_matching(user_id=args.user)
    print(f"Shortlisted {len(ids)} applications: {ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
