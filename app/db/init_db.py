"""DB engine, session, init."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

engine = create_engine(
    settings.sqlite_url,
    echo=False,
    connect_args={"timeout": 30, "check_same_thread": False},
)


def init_db() -> None:
    """Create tables if they don't exist."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Importing models registers them with SQLModel.metadata
    from app.db import models  # noqa: F401
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {settings.sqlite_path}")
