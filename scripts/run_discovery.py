"""CLI: python -m scripts.run_discovery"""
from app.discovery.pipeline import run_discovery
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
print(f"Inserted {run_discovery()} new jobs.")
