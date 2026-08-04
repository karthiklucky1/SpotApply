"""CLI: one discovery pass over the configured boards and feeds.

    python -m scripts.run_discovery

Writes new postings to the shared pool. Network + DB writes, no LLM calls.
The body sits behind main() because `print(f"{run_discovery()}")` at module
level meant importing this module — or asking it for --help — ran a full pass.
"""
from __future__ import annotations

import argparse
import logging


def main() -> int:
    argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    from app.discovery.pipeline import run_discovery

    print(f"Inserted {run_discovery()} new jobs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
