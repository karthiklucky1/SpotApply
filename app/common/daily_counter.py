"""Platform-wide daily allowances that survive restarts and replicas.

`reserve(name, cap)` takes ONE unit of today's allowance for `name`, or
refuses. The count lives in `PlatformCounter` (one row per name per UTC day)
and the reservation is a single conditional UPDATE — `count = count + 1 WHERE
count < cap` — so two processes racing for the last unit cannot both get it,
and a restart does not hand out a fresh day.

The alternative this replaces was a dict in process memory: it started at zero
on every deploy, and every replica kept its own, so a "400 calls/day" cap was
really 400 per process per uptime. The card-mint cap and the platform finals
cap still work that way (`lanes_enabled` documents why exactly one process
runs lanes); the geography verifier is the first caller that promised a
number as a platform-wide guarantee and so is the first to persist it.

Refusing is the safe failure: when the database cannot record a reservation
the call is NOT made (the caller treats it like a platform skip — deferred, not
charged to the posting). Money that cannot be accounted for is not spent.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)


def _today() -> date:
    return datetime.utcnow().date()


def reserve(name: str, cap: int, day: Optional[date] = None) -> bool:
    """Take one unit of today's allowance for ``name``. True when taken.

    ``cap <= 0`` means unlimited: the unit is still recorded, so the day's
    count stays observable. Never raises; a database failure refuses.
    """
    from sqlalchemy import text as _text
    from sqlalchemy.exc import IntegrityError
    from app.db.init_db import get_session
    from app.db.models import PlatformCounter

    day = day or _today()
    now = datetime.utcnow()
    for attempt in range(2):
        try:
            with get_session() as session:
                if cap > 0:
                    res = session.execute(
                        _text("UPDATE platform_counter SET count = COALESCE(count, 0) + 1, "
                              "updated_at = :now WHERE name = :n AND day = :d "
                              "AND COALESCE(count, 0) < :cap"),
                        {"n": name, "d": day, "cap": int(cap), "now": now})
                else:
                    res = session.execute(
                        _text("UPDATE platform_counter SET count = COALESCE(count, 0) + 1, "
                              "updated_at = :now WHERE name = :n AND day = :d"),
                        {"n": name, "d": day, "now": now})
                if res.rowcount:
                    session.commit()
                    return True
                # No row yet, or the cap is reached. Only the first case can
                # be fixed by inserting; the unique constraint settles a race
                # between two processes creating the row.
                if count(name, day, session=session) is not None:
                    session.rollback()
                    return False
                session.add(PlatformCounter(name=name, day=day, count=1, updated_at=now))
                session.commit()
                return True
        except IntegrityError:
            continue            # another process created the row: retry the UPDATE
        except Exception as e:
            log.warning("daily counter %s could not reserve a unit — refusing: %s", name, e)
            return False
    return False


def count(name: str, day: Optional[date] = None, session=None) -> Optional[int]:
    """Today's count for ``name`` — None when there is no row yet."""
    from sqlmodel import select
    from app.db.init_db import get_session
    from app.db.models import PlatformCounter

    day = day or _today()
    q = select(PlatformCounter.count).where(PlatformCounter.name == name,
                                            PlatformCounter.day == day)
    if session is not None:
        row = session.exec(q).first()
        return None if row is None else int(row or 0)
    try:
        with get_session() as s:
            row = s.exec(q).first()
    except Exception as e:
        log.debug("daily counter %s could not be read: %s", name, e)
        return None
    return None if row is None else int(row or 0)


def reset(name: str, day: Optional[date] = None) -> None:
    """Tests only: forget today's row for ``name``."""
    from sqlalchemy import delete as _delete
    from app.db.init_db import get_session
    from app.db.models import PlatformCounter

    day = day or _today()
    try:
        with get_session() as s:
            s.exec(_delete(PlatformCounter).where(PlatformCounter.name == name,
                                                  PlatformCounter.day == day))
            s.commit()
    except Exception as e:
        log.debug("daily counter %s could not be reset: %s", name, e)
