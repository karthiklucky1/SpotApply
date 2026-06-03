import sys
import os

# Automatically resolve and inject virtualenv site-packages if running outside virtualenv
venv_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".venv"))
if os.path.exists(venv_dir):
    lib_dir = os.path.join(venv_dir, "lib")
    if os.path.exists(lib_dir):
        for py_ver in os.listdir(lib_dir):
            site_packages = os.path.join(lib_dir, py_ver, "site-packages")
            if os.path.exists(site_packages) and site_packages not in sys.path:
                sys.path.insert(0, site_packages)
                break
    venv_bin = os.path.join(venv_dir, "bin")
    if os.path.exists(venv_bin) and venv_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = venv_bin + os.path.pathsep + os.environ.get("PATH", "")
        os.environ["VIRTUAL_ENV"] = venv_dir

# Add the project directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.init_db import get_session
from app.db.models import Job, Application, ApplicationStatus
from sqlmodel import select, col

def check_funnel():
    with get_session() as session:
        # 1. Total jobs
        total_jobs = len(session.exec(select(Job)).all())
        print(f"1. Total scraped jobs in DB: {total_jobs}")
        
        # 2. Distinct companies
        companies = len(session.exec(select(Job.company).distinct()).all())
        print(f"2. Companies/Boards actually populated: {companies}")
        
        # 3. Embedded vs unembedded
        embedded = len(session.exec(select(Job).where(col(Job.embedding_id).is_not(None))).all())
        print(f"3. Jobs embedded in FAISS: {embedded} | Unembedded: {total_jobs - embedded}")
        
        # 4. Score distribution
        scores = session.exec(select(Job.similarity_score)).all()
        score_bands = {"0.8+": 0, "0.6-0.8": 0, "0.4-0.6": 0, "<0.4": 0, "None": 0}
        for s in scores:
            if s is None:
                score_bands["None"] += 1
            elif s >= 0.8:
                score_bands["0.8+"] += 1
            elif s >= 0.6:
                score_bands["0.6-0.8"] += 1
            elif s >= 0.4:
                score_bands["0.4-0.6"] += 1
            else:
                score_bands["<0.4"] += 1
                
        print("\n4. Similarity Score Bands:")
        for band, count in score_bands.items():
            print(f"   - {band}: {count} jobs")
            
        # 5. Rerank distribution
        rerank_scores = session.exec(select(Job.rerank_score)).all()
        rerank_bands = {"70+": 0, "50-70": 0, "<50": 0, "None": 0}
        for r in rerank_scores:
            if r is None:
                rerank_bands["None"] += 1
            elif r >= 70:
                rerank_bands["70+"] += 1
            elif r >= 50:
                rerank_bands["50-70"] += 1
            else:
                rerank_bands["<50"] += 1
                
        print("\n5. Rerank Score Bands (Claude):")
        for band, count in rerank_bands.items():
            print(f"   - {band}: {count} jobs")
            
        # 6. Applications grouped by status
        apps = session.exec(select(Application)).all()
        app_status_counts = {}
        for app in apps:
            app_status_counts[app.status] = app_status_counts.get(app.status, 0) + 1
            
        print(f"\n6. Applications in DB: {len(apps)}")
        print("   Status Grouping:")
        for status, count in app_status_counts.items():
            print(f"   - {status.value}: {count} applications")

if __name__ == "__main__":
    check_funnel()
