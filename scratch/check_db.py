import sys
import os

# Add the project directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.init_db import get_session
from app.db.models import AnswerMemory, Application, PendingQuestion
from sqlmodel import select

def check_db():
    print("--- AnswerMemory Entries ---")
    with get_session() as session:
        entries = session.exec(select(AnswerMemory)).all()
        if not entries:
            print("No entries in AnswerMemory yet.")
        for idx, entry in enumerate(entries, 1):
            print(f"{idx}. Original: '{entry.label_original}'")
            print(f"   Normalized: '{entry.label_normalized}'")
            print(f"   Answer: '{entry.answer}'")
            print(f"   Use Count: {entry.use_count}")
            print("-" * 40)

        print("\n--- PendingQuestion Entries ---")
        pqs = session.exec(select(PendingQuestion)).all()
        if not pqs:
            print("No pending questions.")
        for idx, pq in enumerate(pqs, 1):
            print(f"{idx}. App ID: {pq.application_id} | Label: '{pq.field_label}'")
            print(f"   Answer: '{pq.answer}'")
            print("-" * 40)

if __name__ == "__main__":
    check_db()
