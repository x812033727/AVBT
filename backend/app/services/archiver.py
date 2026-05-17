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
import random
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import OfflineTaskLog, TrackedListing
from ..scrapers import javbus as scraper
from .notify import send_webhook
from .pikpak import PikPakError, pikpak_service

logger = logging.getLogger(__name__)

_SAFE_CODE = re.compile(r"[^A-Za-z0-9_\-]+")

# Hierarchy priority — when a code belongs to multiple tracked listings,
# the leftmost match wins. Most-specific first.
_KIND_PRIORITY = ("series", "director", "label", "studio", "star")


def _safe_code(code: str) -> str:
    """Sanitise a code so it can be used as a folder name."""
    return _SAFE_CODE.sub("", code.strip())[:64]


# Delegate to the shared helper so missing-code services can compute the
# same path without importing the archiver (which would cycle).
from .jav_code import KIND_LABELS_CH, safe_folder_name as _safe_name  # noqa: E402


# A small per-pass cache so two completed tasks with the same code don't
# trigger two JavBus fetches.
_detail_cache: dict[str, object] = {}


def _detail_kinds(detail) -> dict[str, tuple[str, str]]:
    """From a MovieDetail, return {kind: (slug, name)} for whatever
    listing-kind attributes are populated."""
    out: dict[str, tuple[str, str]] = {}
    for kind in ("series", "director", "label", "studio"):
        ref = getattr(detail, kind, None)
        if ref and getattr(ref, "id", "") and getattr(ref, "name", ""):
            out[kind] = (ref.id, ref.name)
    for actress in getattr(detail, "actresses", None) or []:
        if actress.id and actress.name:
            out["star"] = (actress.id, actress.name)
            break  # use the first credited actress
    return out


async def _resolve_archive_path(code: str) -> str:
    """Pick the destination folder for ``code`` based on TrackedListing
    membership. Falls back to ``pikpak_archive_folder/<code>`` when no
    tracked listing matches (or detail lookup fails)."""
    safe_code = _safe_code(code)
    fallback = f"{settings.pikpak_archive_folder}/{safe_code}"

    detail = _detail_cache.get(code)
    if detail is None:
        try:
            detail = await scraper.fetch_detail(code)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_detail(%s) failed: %s", code, exc)
            return fallback
        _detail_cache[code] = detail

    kinds = _detail_kinds(detail)  # type: ignore[arg-type]
    if not kinds:
        return fallback

    async with SessionLocal() as session:
        for kind in _KIND_PRIORITY:
            ref = kinds.get(kind)
            if not ref:
                continue
            slug, name = ref
            row = await session.get(TrackedListing, (kind, slug))
            if row is None:
                continue
            safe = _safe_name(name, fallback=_safe_name(slug, fallback="unknown"))
            root = settings.pikpak_download_folder or "AVBT"
            # Use the Chinese label so the cloud layout reads naturally:
            # ``AVBT/系列/回胴錄/DAM-066`` instead of ``AVBT/series/...``.
            kind_dir = KIND_LABELS_CH.get(kind, kind)
            return f"{root}/{kind_dir}/{safe}/{safe_code}"

    return fallback


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
    # Reset per-pass detail cache.
    _detail_cache.clear()
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
            if not _safe_code(row.code):
                continue
            try:
                target_path = await _resolve_archive_path(row.code)
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
            # Newly-archived codes change which codes count as "present".
            try:
                from .pikpak_presence import presence_index  # avoid cycle
                presence_index.invalidate()
            except Exception:  # noqa: BLE001
                pass
            for msg in notifications:
                asyncio.create_task(send_webhook(msg))

    state.archived_total += moved
    return moved


async def run_loop() -> None:
    """Background loop. Sleeps between iterations; survives errors."""
    consecutive_errors = 0
    while True:
        try:
            await archive_once()
            consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            consecutive_errors += 1
            state.last_error = str(exc)
            logger.exception("archiver loop iteration failed")
        finally:
            state.last_run = datetime.utcnow()
        base = max(15, settings.archive_interval_seconds)
        backoff = min(4, 2 ** consecutive_errors) if consecutive_errors else 1
        jitter = random.uniform(0, min(10, base / 10))
        await asyncio.sleep(base * backoff + jitter)
