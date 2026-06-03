"""CLI: python -m scripts.run_matching"""
from app.matching.pipeline import run_matching
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
ids = run_matching()
print(f"Shortlisted {len(ids)} applications: {ids}")
