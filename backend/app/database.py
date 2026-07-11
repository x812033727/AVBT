import logging
import os
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)


def attach_sqlite_pragmas(target) -> None:
    """WAL + busy_timeout on every pooled connection.

    Concurrent writers (download_queue ×5, pcloud_transfer, tracker,
    archiver, log_cleanup, auto_backup) on the default rollback journal
    with busy_timeout=0 fail fast with "database is locked"; WAL lets
    readers proceed under a writer and busy_timeout makes contending
    writers wait instead of erroring. journal_mode persists in the db
    file but busy_timeout is per-connection, hence the connect hook.
    Listens on ``sync_engine`` — the async engine itself doesn't emit
    pool events."""

    @event.listens_for(target.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


_ensure_sqlite_dir(settings.database_url)

_is_sqlite = settings.database_url.startswith("sqlite")
engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    # aiosqlite forwards this to sqlite3.connect — connection-level
    # busy wait as a second line of defence under the PRAGMA below.
    connect_args={"timeout": 30} if _is_sqlite else {},
)
if _is_sqlite:
    attach_sqlite_pragmas(engine)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# ---------------------------------------------------------------------------
# app_meta helpers — raw SQL because they run inside migration connections
# before the ORM session machinery is worth involving.

async def _meta_get(conn, key: str) -> str | None:
    row = (
        await conn.exec_driver_sql("SELECT value FROM app_meta WHERE key = ?", (key,))
    ).first()
    return row[0] if row else None


async def _meta_set(conn, key: str, value: str) -> None:
    await conn.exec_driver_sql(
        "INSERT INTO app_meta (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = CURRENT_TIMESTAMP",
        (key, value),
    )


async def _run_once(flag: str, fn) -> None:
    """Run a one-time migration guarded by an app_meta flag.

    On failure the flag stays unset so the migration retries next boot —
    but unlike the old blanket ``except: pass`` the error is logged."""
    async with engine.begin() as conn:
        if await _meta_get(conn, flag) == "1":
            return
    try:
        await fn()
    except Exception as exc:  # noqa: BLE001 — must not block startup
        logger.warning("one-time migration %r failed (will retry next boot): %s", flag, exc)
        return
    async with engine.begin() as conn:
        await _meta_set(conn, flag, "1")
    logger.info("one-time migration %r completed", flag)


_MIGRATION_DDL = (
    "ALTER TABLE offline_task_log ADD COLUMN archived BOOLEAN DEFAULT 0",
    "ALTER TABLE offline_task_log ADD COLUMN archived_at DATETIME",
    "ALTER TABLE offline_task_log ADD COLUMN btih VARCHAR(64) DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS ix_offline_task_log_btih ON offline_task_log(btih)",
    # Speeds up the archive_once "pending unarchived" lookup +
    # the new idle-skip count peek.
    "CREATE INDEX IF NOT EXISTS ix_offline_task_log_archived ON offline_task_log(archived, file_id)",
    # Supports the periodic prune-old-archived sweep.
    "CREATE INDEX IF NOT EXISTS ix_offline_task_log_archived_at ON offline_task_log(archived, archived_at)",
    # History page orders by created_at DESC with OFFSET pagination;
    # phase feeds the dashboard stats + history phase filter.
    "CREATE INDEX IF NOT EXISTS ix_offline_task_log_created_at ON offline_task_log(created_at)",
    "CREATE INDEX IF NOT EXISTS ix_offline_task_log_phase ON offline_task_log(phase)",
    # Collection list filters on status and orders on updated_at.
    "CREATE INDEX IF NOT EXISTS ix_collected_movies_status ON collected_movies(status)",
    "CREATE INDEX IF NOT EXISTS ix_collected_movies_updated_at ON collected_movies(updated_at)",
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
    "CREATE INDEX IF NOT EXISTS ix_pcloud_transfer_created_at ON pcloud_transfer(created_at)",
    # Change-password revokes tokens issued before this moment.
    "ALTER TABLE auth_account ADD COLUMN password_changed_at DATETIME",
    # pCloud transfer auto-retry bookkeeping.
    "ALTER TABLE pcloud_transfer ADD COLUMN attempts INTEGER DEFAULT 0",
    "ALTER TABLE pcloud_transfer ADD COLUMN next_retry_at DATETIME",
    # Supports an age-based prune of the persistent detail cache.
    "CREATE INDEX IF NOT EXISTS ix_movie_detail_cache_fetched_at "
    "ON movie_detail_cache(fetched_at)",
)


async def _backfill_tracked_actresses() -> None:
    """Copy rows from the legacy tracked_actresses table (pre-listing
    era) into tracked_listing. The legacy physical table is kept as a
    safety net; only the ORM model was removed."""
    async with engine.begin() as conn:
        exists = (
            await conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracked_actresses'"
            )
        ).first()
        if not exists:
            return
        # quiet_ticks / last_missing_count are NOT NULL on freshly
        # created tables (ORM default is Python-side only), so supply
        # them explicitly — the legacy table predates those columns.
        await conn.exec_driver_sql(
            """
            INSERT INTO tracked_listing
              (kind, id, name, avatar, uncensored, auto_send,
               last_seen_code, last_checked_at, last_error, new_count,
               quiet_ticks, last_missing_count, created_at)
            SELECT 'star', id, name, avatar, uncensored, auto_send,
                   last_seen_code, last_checked_at, last_error, new_count,
                   0, 0, created_at
            FROM tracked_actresses
            WHERE NOT EXISTS (
                SELECT 1 FROM tracked_listing
                WHERE kind = 'star' AND id = tracked_actresses.id
            )
            """
        )


async def _backfill_btih() -> None:
    """Fill btih on legacy rows in batches so a huge history table
    doesn't blow up memory. extract_btih returns '' for unparsable
    magnets, which we still write so the row isn't re-selected."""
    from .scrapers.javbus import extract_btih  # local import: avoid cycles

    async with engine.begin() as conn:
        # Cursor on id (not just the WHERE) so rows whose magnet yields
        # no hash — written back as '' — can't be re-selected forever.
        last_id = 0
        while True:
            rows = (
                await conn.exec_driver_sql(
                    "SELECT id, magnet FROM offline_task_log "
                    "WHERE (btih IS NULL OR btih = '') AND id > ? "
                    "ORDER BY id LIMIT 500",
                    (last_id,),
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
                last_id = row_id


async def _cleanup_listing_names() -> None:
    """Strip JavBus' "- <kind> - 影片" suffix from tracked listing names
    captured before clean_listing_name existed."""
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
                    "UPDATE tracked_listing SET name = ? WHERE kind = ? AND id = ?",
                    (cleaned, kind, slug),
                )


async def init_db() -> None:
    from . import models  # noqa: F401  – ensure tables are registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight migrations: add columns/indexes introduced after
        # the table was first created. "duplicate column" just means the
        # ALTER already ran — anything else is a real problem and gets
        # logged instead of silently swallowed.
        for ddl in _MIGRATION_DDL:
            try:
                await conn.exec_driver_sql(ddl)
            except Exception as exc:  # noqa: BLE001 — must not block startup
                if "duplicate column name" in str(exc).lower():
                    continue
                logger.warning("migration DDL failed %r: %s", ddl, exc)

    # One-time data migrations. Each is guarded by an app_meta flag so
    # steady-state startup skips the full-table scans entirely.
    await _run_once("migrated:tracked_actresses_backfill", _backfill_tracked_actresses)
    await _run_once("migrated:btih_backfill", _backfill_btih)
    await _run_once("migrated:listing_name_cleanup", _cleanup_listing_names)


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
