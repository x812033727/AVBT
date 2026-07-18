"""Periodic prune of long-archived / long-abandoned offline_task_log rows.

The table grows ~1 row per PikPak submit and is never truncated by any
other path. Left alone it slows the linear-scan queries in
``archive_once`` and the ``_load_sent_hashes`` set build. Two terminal
states are pruned once older than the retention window: rows archived
and older than the window (keyed on ``archived_at`` — the file has long
since been moved out of the TASK folder and the BTIH dedup window only
matters for in-flight submissions), and rows dead-lettered as
``abandoned`` (keyed on ``created_at`` since they have no
``archived_at``) — those are terminal too and would otherwise
accumulate forever."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_

from ..config import settings
from ..database import SessionLocal
from ..models import OfflineTaskLog

logger = logging.getLogger(__name__)


async def prune_offline_task_log(older_than_days: int) -> int:
    if older_than_days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    async with SessionLocal() as session:
        result = await session.execute(
            delete(OfflineTaskLog).where(
                or_(
                    and_(
                        OfflineTaskLog.archived.is_(True),
                        OfflineTaskLog.archived_at.is_not(None),
                        OfflineTaskLog.archived_at < cutoff,
                    ),
                    and_(
                        OfflineTaskLog.abandoned.is_(True),
                        OfflineTaskLog.created_at < cutoff,
                    ),
                )
            )
        )
        await session.commit()
        return result.rowcount or 0


async def run_loop(interval_seconds: int = 86_400) -> None:
    """Daily prune. Defers the first run by ``interval_seconds`` to keep
    startup quiet and lets the rest of the lifespan tasks settle first."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            retention = settings.offline_log_retention_days
            if retention <= 0:
                continue
            n = await prune_offline_task_log(retention)
            if n:
                logger.info("pruned %d offline_task_log rows older than %d days", n, retention)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("log cleanup iteration failed")
