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
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ..database import SessionLocal
from ..models import OfflineTaskLog

logger = logging.getLogger(__name__)

SETTLE_GRACE = timedelta(minutes=15)


def recently_created(entries, grace: timedelta = SETTLE_GRACE) -> bool:
    """True when any entry's PikPak ``created_time`` falls within the
    grace window — the folder is still actively receiving files.

    Complements :func:`is_settling`: the DB grace is anchored on
    submission time, so a slow (non-cached) torrent that keeps
    materialising files hours later would slip past it. File birth
    times don't lie. Unparseable timestamps are treated as recent
    (fail closed — this guards permanent deletes)."""
    cutoff = datetime.now(UTC) - grace
    for e in entries:
        raw = getattr(e, "created_time", None)
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except ValueError:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts > cutoff:
            return True
    return False


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
