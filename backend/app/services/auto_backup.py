"""Periodic SQLite backup.

Uses the stdlib ``sqlite3`` online-backup API (safe against a live
writer, unlike a plain file copy) to snapshot the DB into
``<data-dir>/backups/avbt-YYYYMMDD-HHMMSS.db``, keeping the newest
``auto_backup_keep`` files. Credential files (auth secret, cloud
tokens) are mirrored into ``backups/credentials/`` on every run —
latest copy only; they aren't versioned data, the copy just survives
an accidental delete/corruption of the originals. The last run's
timestamp/result is stored in app_meta so the settings page can
display it."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..database import SessionLocal
from ..models import AppMeta

logger = logging.getLogger(__name__)

_META_KEY = "auto_backup:last"

# Relative to the data dir (= the DB file's parent).
_CREDENTIAL_FILES = ("auth_secret.txt", "pikpak_token.txt", "pcloud_token.json")


def _db_file() -> Path | None:
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    path = url.split("///", 1)[-1]
    if not path or path == ":memory:":
        return None
    return Path(path)


def backup_dir() -> Path | None:
    db = _db_file()
    return db.parent / "backups" if db else None


def _backup_sync(src: Path, dest: Path) -> None:
    with sqlite3.connect(src) as source, sqlite3.connect(dest) as target:
        source.backup(target)


def _prune_sync(directory: Path, keep: int) -> int:
    files = sorted(directory.glob("avbt-*.db"), key=lambda p: p.name, reverse=True)
    removed = 0
    for old in files[max(1, keep):]:
        old.unlink(missing_ok=True)
        removed += 1
    return removed


async def run_backup() -> Path:
    """Create one backup + prune. Raises on failure (caller logs)."""
    src = _db_file()
    if src is None or not src.exists():
        raise RuntimeError(f"找不到資料庫檔案: {settings.database_url}")
    directory = src.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"avbt-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.db"
    await asyncio.to_thread(_backup_sync, src, dest)
    removed = await asyncio.to_thread(_prune_sync, directory, settings.auto_backup_keep)
    copied = await asyncio.to_thread(_copy_credentials_sync, src.parent, directory)
    logger.info(
        "database backed up to %s (pruned %d old, %d credential file(s))",
        dest, removed, copied,
    )
    await _record(f"ok:{dest.name}")
    return dest


def _copy_credentials_sync(data_dir: Path, backup_root: Path) -> int:
    cred_dir = backup_root / "credentials"
    copied = 0
    for name in _CREDENTIAL_FILES:
        src = data_dir / name
        if not src.is_file():
            continue
        try:
            cred_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, cred_dir / name)  # copy2 keeps the 0600 mode
            copied += 1
        except OSError as exc:
            logger.warning("credential backup failed for %s: %s", name, exc)
    return copied


async def _record(value: str) -> None:
    try:
        async with SessionLocal() as session:
            row = await session.get(AppMeta, _META_KEY)
            if row is None:
                row = AppMeta(key=_META_KEY)
                session.add(row)
            row.value = value
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — status record is best-effort
        logger.warning("auto backup status record failed: %s", exc)


async def status() -> dict:
    last_value = ""
    last_at = None
    try:
        async with SessionLocal() as session:
            row = await session.get(AppMeta, _META_KEY)
        if row is not None:
            last_value = row.value
            last_at = row.updated_at
    except Exception as exc:  # noqa: BLE001
        logger.warning("auto backup status read failed: %s", exc)
    directory = backup_dir()
    files = []
    if directory and directory.is_dir():
        files = sorted((p.name for p in directory.glob("avbt-*.db")), reverse=True)
    return {
        "enabled": settings.auto_backup_enabled,
        "interval_hours": settings.auto_backup_interval_hours,
        "keep": settings.auto_backup_keep,
        "last_result": last_value,
        "last_at": last_at,
        "files": files,
    }


async def run_loop() -> None:
    """Hourly-granularity loop: first backup fires one interval after
    startup (a boot loop must not thrash the disk with snapshots)."""
    while True:
        interval = max(1, settings.auto_backup_interval_hours) * 3600
        await asyncio.sleep(interval)
        if not settings.auto_backup_enabled:
            continue
        try:
            await run_backup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto backup iteration failed")
            await _record(f"error:{exc}")
