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
            "ALTER TABLE offline_task_log ADD COLUMN btih VARCHAR(64) DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS ix_offline_task_log_btih ON offline_task_log(btih)",
            # Speeds up the archive_once "pending unarchived" lookup +
            # the new idle-skip count peek.
            "CREATE INDEX IF NOT EXISTS ix_offline_task_log_archived ON offline_task_log(archived, file_id)",
            # Supports the periodic prune-old-archived sweep.
            "CREATE INDEX IF NOT EXISTS ix_offline_task_log_archived_at ON offline_task_log(archived, archived_at)",
            # Snapshot of the tracked listing at enqueue time so the
            # archiver can skip a JavBus fetch_detail for codes that
            # originated from the tracker.
            "ALTER TABLE offline_task_log ADD COLUMN tracked_kind VARCHAR(16) DEFAULT ''",
            "ALTER TABLE offline_task_log ADD COLUMN tracked_slug VARCHAR(64) DEFAULT ''",
            "ALTER TABLE offline_task_log ADD COLUMN tracked_name VARCHAR(128) DEFAULT ''",
            # Adaptive full-catalog scan counters for tracker auto-send.
            "ALTER TABLE tracked_listing ADD COLUMN quiet_ticks INTEGER DEFAULT 0",
            "ALTER TABLE tracked_listing ADD COLUMN last_full_scan_at DATETIME",
            # Records the missing-count from the last full scan so the
            # tracker can skip ticks on listings that are already complete.
            "ALTER TABLE tracked_listing ADD COLUMN last_missing_count INTEGER DEFAULT 0",
            # pCloud transfer queue indices.
            "CREATE INDEX IF NOT EXISTS ix_pcloud_transfer_status ON pcloud_transfer(status)",
            "CREATE INDEX IF NOT EXISTS ix_pcloud_transfer_parent ON pcloud_transfer(parent_id)",
            "CREATE INDEX IF NOT EXISTS ix_pcloud_transfer_pikpak_file ON pcloud_transfer(pikpak_file_id)",
        ):
            try:
                await conn.exec_driver_sql(ddl)
            except Exception:
                pass
        # Backfill tracked_listing from the old tracked_actresses table.
        try:
            await conn.exec_driver_sql(
                """
                INSERT INTO tracked_listing
                  (kind, id, name, avatar, uncensored, auto_send,
                   last_seen_code, last_checked_at, last_error, new_count, created_at)
                SELECT 'star', id, name, avatar, uncensored, auto_send,
                       last_seen_code, last_checked_at, last_error, new_count, created_at
                FROM tracked_actresses
                WHERE NOT EXISTS (
                    SELECT 1 FROM tracked_listing
                    WHERE kind = 'star' AND id = tracked_actresses.id
                )
                """
            )
        except Exception:
            pass

    # Backfill btih on existing rows in batches so a huge history table
    # doesn't blow up memory.
    try:
        from .scrapers.javbus import extract_btih  # local import: avoid cycles

        async with engine.begin() as conn:
            while True:
                rows = (
                    await conn.exec_driver_sql(
                        "SELECT id, magnet FROM offline_task_log "
                        "WHERE btih IS NULL OR btih = '' LIMIT 500"
                    )
                ).all()
                if not rows:
                    break
                for row_id, magnet in rows:
                    h = extract_btih(magnet or "")
                    await conn.exec_driver_sql(
                        "UPDATE offline_task_log SET btih = ? WHERE id = ?",
                        (h, row_id),
                    )
    except Exception:
        pass

    # Strip JavBus' "- <kind> - 影片" suffix from any tracked listing
    # names that were captured before clean_listing_name existed.
    try:
        from .services.jav_code import clean_listing_name  # local: avoid cycles

        async with engine.begin() as conn:
            rows = (
                await conn.exec_driver_sql(
                    "SELECT kind, id, name FROM tracked_listing "
                    "WHERE name IS NOT NULL AND name != ''"
                )
            ).all()
            for kind, slug, name in rows:
                cleaned = clean_listing_name(name)
                if cleaned and cleaned != name:
                    await conn.exec_driver_sql(
                        "UPDATE tracked_listing SET name = ? "
                        "WHERE kind = ? AND id = ?",
                        (cleaned, kind, slug),
                    )
    except Exception:
        pass


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
