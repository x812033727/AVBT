"""One-off / manually-triggered reconcile of fossil ``offline_task_log``
rows: historical submissions that were never archived and never
abandoned, but whose code is already accounted for elsewhere — a real
video/container already sits on PikPak (presence index), or a sibling
row for the same code already finished ``finalize``. Those rows hold
``archive_once``'s cheap pending peek open and get re-listed by the
finalize retry / reap passes forever (see services/archiver.py), so
this marks them ``superseded`` once evidence says their work is done.

Strongest evidence first: an actual video/container file beats a merely
-finalized sibling row, whose own finalize could in principle have been
undone by a later re-download. No evidence → left untouched; a human
can always look at it later, deleting a row would break btih dedup."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select

from ..database import SessionLocal
from ..models import OfflineTaskLog, PresenceEntry
from .jav_code import CONTAINER_EXTS, VIDEO_EXTS, ext_of

logger = logging.getLogger(__name__)

_MEDIA_EXTS = VIDEO_EXTS | CONTAINER_EXTS

# Batch commits so a large backlog (~10-20k fossil rows) doesn't hold one
# giant transaction open the whole pass.
_COMMIT_BATCH = 500

_RULE_PRESENCE_VIDEO = "presence has video"
_RULE_FINALIZED_SIBLING = "finalized sibling"


def _has_video_path(paths: list[str]) -> bool:
    """Whether any of a code's presence paths is an actual media file —
    a folder-only listing (leaf == the code itself, no extension) does
    not count."""
    for p in paths:
        leaf = p.rsplit("/", 1)[-1]
        if ext_of(leaf) in _MEDIA_EXTS:
            return True
    return False


async def reconcile_superseded(
    *, dry_run: bool = True, older_than: str = "2026-07-01", limit: int = 5000
) -> dict:
    """Scan candidate fossil rows and mark the ones with evidence their
    code is already handled elsewhere as ``superseded``.

    ``older_than`` is a hard-required absolute date (``YYYY-MM-DD``) —
    deliberately never a relative "N days ago" window, so a caller can't
    accidentally sweep in-flight submissions by mistyping a delta.
    Raises ``ValueError`` if it doesn't parse.
    """
    cutoff = datetime.strptime(older_than, "%Y-%m-%d")

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(OfflineTaskLog)
                .where(
                    OfflineTaskLog.finalized.is_(False),
                    OfflineTaskLog.abandoned.is_(False),
                    OfflineTaskLog.superseded.is_(False),
                    OfflineTaskLog.code != "",
                    OfflineTaskLog.created_at < cutoff,
                )
                .order_by(OfflineTaskLog.created_at.asc())
                .limit(limit)
            )
        ).scalars().all()

        counts = {"presence_video": 0, "finalized_sibling": 0, "untouched": 0}
        if not rows:
            return {"scanned": 0, "dry_run": dry_run, **counts}

        codes = sorted({r.code for r in rows})

        # Rule 1: the code already has a real video/container on PikPak.
        # One grouped select over every candidate code, grouped in Python.
        presence_paths: dict[str, list[str]] = {}
        for code, path in (
            await session.execute(
                select(PresenceEntry.code, PresenceEntry.path).where(
                    PresenceEntry.code.in_(codes)
                )
            )
        ).all():
            presence_paths.setdefault(code, []).append(path)
        video_codes = {
            c for c, paths in presence_paths.items() if _has_video_path(paths)
        }

        # Rule 2: a finalized sibling row already exists for the code.
        # Only worth checking for codes rule 1 didn't already claim.
        remaining = [c for c in codes if c not in video_codes]
        sibling_codes: set[str] = set()
        if remaining:
            sibling_codes = set(
                (
                    await session.execute(
                        select(OfflineTaskLog.code)
                        .where(
                            OfflineTaskLog.code.in_(remaining),
                            OfflineTaskLog.finalized.is_(True),
                        )
                        .distinct()
                    )
                ).scalars().all()
            )

        pending = 0
        for row in rows:
            if row.code in video_codes:
                rule_label, key = _RULE_PRESENCE_VIDEO, "presence_video"
            elif row.code in sibling_codes:
                rule_label, key = _RULE_FINALIZED_SIBLING, "finalized_sibling"
            else:
                counts["untouched"] += 1
                continue
            counts[key] += 1
            if not dry_run:
                row.superseded = True
                row.message = f"superseded: {rule_label}"
                pending += 1
                if pending >= _COMMIT_BATCH:
                    await session.commit()
                    pending = 0
        if not dry_run and pending:
            await session.commit()

        logger.info(
            "fossil reconcile (dry_run=%s): scanned=%d presence_video=%d "
            "finalized_sibling=%d untouched=%d",
            dry_run, len(rows), counts["presence_video"],
            counts["finalized_sibling"], counts["untouched"],
        )

    return {"scanned": len(rows), "dry_run": dry_run, **counts}
