import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    from . import models  # noqa: F401  – ensure tables are registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight migration: add columns introduced after the table was
        # first created. SQLite raises on duplicate column → swallow.
        for ddl in (
            "ALTER TABLE offline_task_log ADD COLUMN archived BOOLEAN DEFAULT 0",
            "ALTER TABLE offline_task_log ADD COLUMN archived_at DATETIME",
        ):
            try:
                await conn.exec_driver_sql(ddl)
            except Exception:
                pass


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
