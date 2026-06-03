import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.init_db import get_session
from app.db.models import Job, JobSource
from sqlmodel import select

def print_sources():
    with get_session() as session:
        jobs = session.exec(select(Job)).all()
        print(f"Total jobs in DB: {len(jobs)}")
        
        sources = {}
        for j in jobs:
            sources[j.source] = sources.get(j.source, 0) + 1
            
        print("\nJobs by source:")
        for src, count in sources.items():
            print(f"- {src.value}: {count} jobs")

if __name__ == "__main__":
    print_sources()
