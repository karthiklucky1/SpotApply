import logging
from typing import Optional, Tuple
from app.db.models import Job, AnswerMemory

log = logging.getLogger(__name__)

class AnswerMemoryLearner:
    def __init__(self):
        log.info("AnswerMemoryLearner initialized (stub).")

    def record_human_answer(self, label: str, answer: str) -> None:
        """Learn and persist a human answered question to database memory for future auto-answering."""
        norm = label.lower().strip()
        log.info("Learned new answer from user: '%s' -> '%s'", norm, answer)

    def query_memory(self, label: str) -> Optional[Tuple[str, float]]:
        """Query memory for a similar answered question and return (answer, confidence)."""
        return None
