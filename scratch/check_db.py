import sqlite3
import os

def run():
    db_path = "data/jobagent.db"
    if not os.path.exists(db_path):
        print(f"Error: {db_path} does not exist!")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # List tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    print("Tables:", tables)

    # Let's count applications by status
    if "application" in tables:
        cursor.execute("SELECT status, COUNT(*) FROM application GROUP BY status;")
        print("\nApplications by status:")
        for status, count in cursor.fetchall():
            print(f"  {status}: {count}")

    # Let's count jobs by source
    if "job" in tables:
        cursor.execute("SELECT source, COUNT(*) FROM job GROUP BY source;")
        print("\nJobs by source:")
        for source, count in cursor.fetchall():
            print(f"  {source}: {count}")

    # Let's find shortlisted applications and join with jobs
    if "application" in tables and "job" in tables:
        cursor.execute("""
            SELECT j.id, j.company, j.title, j.source, j.similarity_score, j.rerank_score, a.status, a.apply_track
            FROM application a
            JOIN job j ON a.job_id = j.id
            WHERE a.status = 'shortlisted';
        """)
        shortlisted_jobs = cursor.fetchall()
        print(f"\nShortlisted applications ({len(shortlisted_jobs)}):")
        for job in shortlisted_jobs:
            print(f"  Job ID {job[0]}: {job[1]} - {job[2]} ({job[3]}) | similarity={job[4]} rerank={job[5]} status={job[6]} track={job[7]}")

    conn.close()

if __name__ == "__main__":
    run()
