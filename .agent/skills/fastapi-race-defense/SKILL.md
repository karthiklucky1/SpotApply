---
name: fastapi-race-defense
description: "Secures Python/FastAPI endpoints against concurrent race conditions, transactional anomalies, and shared-state mutability bugs."
---

# FastAPI Race Defense Skill

## Goal
To design and build concurrent-safe APIs using FastAPI and SQLModel/SQLAlchemy, protecting the system against transactional anomalies, double-booking/double-submitting, data race conditions, and shared-state mutability bugs.

## Instructions

### 1. Enforce database-level locks (`select_for_update()`)
- When modifying shared resources (e.g., updating application statuses, deducting account balances, updating counters), do not use a standard `select` followed by an `update`.
- Use SQLAlchemy's `with_for_update()` (in SQLModel: `session.exec(select(Model).where(...).with_for_update()).first()`) within a transaction block. This issues a `SELECT ... FOR UPDATE` SQL query, which locks the affected rows until the transaction commits or rolls back.

### 2. ACID Transaction Compliance
- Always scope db sessions cleanly using context managers (e.g., `with get_session() as session:`).
- Commit transactions as late as possible to minimize lock duration, but commit explicitly before responding to the client.
- Wrap operations in try/except blocks to handle transactional failures and issue automatic rollbacks:
  ```python
  try:
      # Perform locked operation
      session.commit()
  except Exception:
      session.rollback()
      raise
  ```

### 3. Distributed Locking (Redis / Redlock)
- When dealing with distributed servers or multi-process environments where database locks are insufficient (e.g., preventing multiple scrapers or workers from starting the same external process concurrently), deploy a distributed lock.
- Use a lightweight library like `redis-py` or `pottery` to enforce atomic lock ownership. Set lock TTLs (Time-to-Live) to avoid deadlock scenarios if a node crashes.

### 4. Eliminate Mutable Global State
- **Critical Anti-Pattern:** Never store request-specific or session-specific state in global variables, class variables, or shared singletons.
- FastAPI endpoints run concurrently on multiple event loop tasks or thread pools. Sharing mutable state on global context objects will lead to data leaks and cross-request contamination.
- Use Dependency Injection (`Depends()`) to supply isolated connection pools, sessions, and context managers to endpoints.

---

## Examples

### 1. Database Lock Protection (Avoiding Double-Process Status Race)
```python
from fastapi import FastAPI, Depends, HTTPException, status
from sqlmodel import select, Session
from app.db.init_db import get_session
from app.db.models import Application, ApplicationStatus

app = FastAPI()

@app.post("/applications/{app_id}/autofill")
async def start_autofill(app_id: int, session: Session = Depends(get_session)):
    with session.begin(): # Begin transaction explicitly
        # Retrieve row with a database lock
        query = select(Application).where(Application.id == app_id).with_for_update()
        db_app = session.exec(query).first()
        
        if not db_app:
            raise HTTPException(status_code=404, detail="Application not found")
        
        # Guard against double-submitting race condition
        if db_app.status in [ApplicationStatus.AUTOFILLED, ApplicationStatus.READY_TO_SUBMIT]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, 
                detail="Application is already being processed or submitted."
            )
            
        # Update status immediately inside the locked transaction
        db_app.status = ApplicationStatus.AWAITING_USER
        session.add(db_app)
        
    # Transaction commits here, lock released. Safe to launch task.
    return {"status": "started"}
```

### 2. Distributed Locking with Redis
```python
import redis
from fastapi import FastAPI, HTTPException

app = FastAPI()
r = redis.Redis(host='localhost', port=6379, db=0)

@app.post("/run-pipeline")
def trigger_pipeline():
    # Attempt to acquire lock for 5 minutes
    lock = r.lock("jobagent:pipeline_run", timeout=300, blocking=False)
    acquired = lock.acquire()
    
    if not acquired:
        raise HTTPException(
            status_code=429, 
            detail="Pipeline is already running in another process."
        )
    
    try:
        # Run execution task safely knowing no concurrent process will run it
        # ...
        pass
    finally:
        lock.release()
```

---

## Constraints
- **Lock Contention:** Do not perform slow HTTP calls or CPU-bound tasks inside a `with_for_update()` transaction. Locks hold open database connections; blocking will exhaust connection pools and freeze your API.
- **Deadlock Avoidance:** Always acquire multiple locks in a deterministic order across all routes. For example, if updating two related rows, always acquire lock A then lock B, never B then A.
