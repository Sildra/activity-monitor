"""Direct async SQLite access.

No database service, no worker thread: a single async engine and session factory,
used straight from request handlers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_database_path = Path("activity.db")


async def init(database: str) -> None:
    global _engine, _sessionmaker, _database_path

    from activity_monitor import models  # noqa: F401  (registers tables with SQLModel metadata)

    _database_path = Path(database)
    _engine = create_async_engine(f"sqlite+aiosqlite:///{_database_path}", echo=False)
    _sessionmaker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    async with _engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)


async def dispose() -> None:
    global _engine, _sessionmaker

    if _engine is not None:
        await _engine.dispose()

    _engine = None
    _sessionmaker = None


def database_path() -> Path:
    return _database_path


def new_session() -> AsyncSession:
    if _sessionmaker is None:
        raise RuntimeError("Database is not initialized")

    return _sessionmaker()


async def get_session() -> AsyncIterator[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database is not initialized")

    async with _sessionmaker() as session:
        yield session
