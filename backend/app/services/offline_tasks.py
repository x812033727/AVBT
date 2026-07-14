"""Shared helpers over ``offline_task_log`` for the organize pipeline.

PikPak materialises a torrent's files one by one — right after
submission a wrapper may show only its FIRST completed file, with the
rest not yet visible in any listing. Per-file phase guards can't see
files that don't exist yet, so a flatten in that window trashes the
wrapper and the remaining files are born straight into the recycler
(live incident: TRE-143 A extracted, B/C/D landed in trash). The only
reliable defence is time: leave freshly-submitted tasks' folders alone
for a grace window.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from ..database import SessionLocal
from ..models import OfflineTaskLog

logger = logging.getLogger(__name__)

SETTLE_GRACE = timedelta(minutes=15)


async def is_settling(file_id: str, grace: timedelta = SETTLE_GRACE) -> bool:
    """True when ``file_id`` belongs to a task submitted within the
    grace window."""
    if not file_id:
        return False
    cutoff = datetime.utcnow() - grace
    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    select(OfflineTaskLog.id).where(
                        OfflineTaskLog.file_id == file_id,
                        OfflineTaskLog.created_at > cutoff,
                    ).limit(1)
                )
            ).scalar()
    except Exception as exc:  # noqa: BLE001 — fail open, guard is best-effort
        logger.debug("is_settling(%s) failed: %s", file_id, exc)
        return False
    return row is not None
