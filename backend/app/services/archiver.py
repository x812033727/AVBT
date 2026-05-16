"""Periodically move completed PikPak offline files to a per-code folder.

Runs as a background asyncio task started by FastAPI's lifespan.

For every row in ``offline_task_log`` where:
    * the task has a code (so we know where to put it)
    * the PikPak task is in PHASE_TYPE_COMPLETE
    * the row is not yet ``archived``

we ensure ``<pikpak_archive_folder>/<code>/`` exists and ``file_batch_move``
the resulting file_id there. The row is then flagged archived so we don't
move it twice.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import OfflineTaskLog
from .pikpak import PikPakError, pikpak_service

logger = logging.getLogger(__name__)


async def _notify(message: str) -> None:
    if not settings.webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            await cli.post(settings.webhook_url, json={"content": message})
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook failed: %s", exc)

_SAFE_CODE = re.compile(r"[^A-Za-z0-9_\-]+")


def _safe_code(code: str) -> str:
    """Sanitise a code so it can be used as a folder name."""
    return _SAFE_CODE.sub("", code.strip())[:64]


class ArchiverState:
    def __init__(self) -> None:
        self.enabled: bool = settings.archive_enabled
        self.last_run: datetime | None = None
        self.archived_total: int = 0
        self.last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": settings.archive_interval_seconds,
            "archive_folder": settings.pikpak_archive_folder,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "archived_total": self.archived_total,
            "last_error": self.last_error,
        }


state = ArchiverState()


async def archive_once() -> int:
    """Run one archive pass. Returns the number of files moved."""
    state.last_error = ""
    if not state.enabled or not settings.pikpak_username:
        return 0

    try:
        tasks = await pikpak_service.list_tasks(size=200)
    except PikPakError as exc:
        state.last_error = f"list_tasks failed: {exc}"
        return 0
    except Exception as exc:  # noqa: BLE001
        state.last_error = str(exc)
        return 0

    completed = {
        t.file_id: t
        for t in tasks
        if t.file_id and t.phase == "PHASE_TYPE_COMPLETE"
    }
    if not completed:
        return 0

    moved = 0
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(OfflineTaskLog).where(
                    OfflineTaskLog.file_id.in_(list(completed.keys())),
                    OfflineTaskLog.archived.is_(False),
                    OfflineTaskLog.code != "",
                )
            )
        ).scalars().all()

        notifications: list[str] = []
        for row in rows:
            code = _safe_code(row.code)
            if not code:
                continue
            target_path = f"{settings.pikpak_archive_folder}/{code}"
            try:
                target_id = await pikpak_service.folder_id(target_path)
                if not target_id:
                    continue
                await pikpak_service.move_files([row.file_id], target_id)
                row.archived = True
                row.archived_at = datetime.utcnow()
                moved += 1
                notifications.append(
                    f"📦 已歸檔 `{row.code}` ({row.name or row.file_id}) → `{target_path}`"
                )
                logger.info("archived %s -> %s", row.file_id, target_path)
            except Exception as exc:  # noqa: BLE001
                state.last_error = f"move {row.file_id} failed: {exc}"
                logger.warning("archive %s failed: %s", row.file_id, exc)

        if moved:
            await session.commit()
            for msg in notifications:
                asyncio.create_task(_notify(msg))

    state.archived_total += moved
    return moved


async def run_loop() -> None:
    """Background loop. Sleeps between iterations; survives errors."""
    while True:
        try:
            await archive_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            state.last_error = str(exc)
            logger.exception("archiver loop iteration failed")
        finally:
            state.last_run = datetime.utcnow()
        await asyncio.sleep(max(15, settings.archive_interval_seconds))
