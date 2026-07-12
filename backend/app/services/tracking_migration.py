"""One-time hard cutover: series tracking → studio tracking.

Deletes every ``kind='series'`` TrackedListing and replaces it with the
distinct studios (製作商) those series belong to, resolved from the
persistent ``movie_detail_cache`` (dominant studio per series). Existing
``kind='studio'`` and ``kind='label'`` rows are left untouched.

Destructive and user-gated: the admin endpoint defaults to ``dry_run``,
and a real run backs the SQLite DB up before mutating. Guarded by the
``migrated:series_to_studio`` app_meta flag so it can't double-apply.
"""

from __future__ import annotations

import logging
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..database import SessionLocal, _meta_get, _meta_set, engine
from ..models import MovieDetailCache, TrackedListing
from ..schemas import MovieDetail

logger = logging.getLogger(__name__)

MIGRATION_FLAG = "migrated:series_to_studio"


async def _resolve_series_to_studio(session) -> tuple[dict[str, str], list[dict], list]:
    """Return ({studio_id: studio_name}, unresolved_series, series_rows).

    Dominant studio per tracked series, from the detail cache."""
    series_rows = (
        await session.execute(
            select(TrackedListing).where(TrackedListing.kind == "series")
        )
    ).scalars().all()
    series_ids = {r.id for r in series_rows}

    studio_by_series: dict[str, Counter] = defaultdict(Counter)
    studio_names: dict[str, str] = {}
    if series_ids:
        cache_rows = (
            await session.execute(select(MovieDetailCache.detail))
        ).all()
        for (detail_json,) in cache_rows:
            try:
                d = MovieDetail.model_validate_json(detail_json)
            except Exception:  # noqa: BLE001 — skip a corrupt row
                continue
            if not (d.series and d.series.id in series_ids):
                continue
            if d.studio and d.studio.id:
                studio_by_series[d.series.id][d.studio.id] += 1
                studio_names[d.studio.id] = (d.studio.name or "").strip() or d.studio.id

    studios: dict[str, str] = {}
    unresolved: list[dict] = []
    for r in series_rows:
        counter = studio_by_series.get(r.id)
        if not counter:
            unresolved.append({"id": r.id, "name": r.name})
            continue
        sid = counter.most_common(1)[0][0]
        studios[sid] = studio_names.get(sid, sid)
    return studios, unresolved, series_rows


def _backup_db() -> str | None:
    """Copy the live SQLite file before a destructive run. Returns the
    backup path, or ``None`` for an in-memory / missing DB (tests)."""
    db_path = engine.url.database
    if not db_path or db_path == ":memory:":
        return None
    src = Path(db_path)
    if not src.exists():
        return None
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(f"{src.name}.bak-series-to-studio-{stamp}")
    shutil.copy2(src, dst)
    logger.info("series→studio migration: DB backed up to %s", dst)
    return str(dst)


async def migrate_series_to_studio(
    *, dry_run: bool = True, auto_send: bool = True
) -> dict[str, Any]:
    """Hard-cut series tracking → studio tracking. Idempotent via the
    app_meta flag. ``dry_run`` (default) reports the plan without
    mutating; a real run backs up the DB, upserts studios, deletes the
    series rows, and sets the flag."""
    async with engine.begin() as conn:
        already = await _meta_get(conn, MIGRATION_FLAG) == "1"

    async with SessionLocal() as session:
        studios, unresolved, series_rows = await _resolve_series_to_studio(session)

        existing_studio_ids = set(
            (
                await session.execute(
                    select(TrackedListing.id).where(TrackedListing.kind == "studio")
                )
            ).scalars().all()
        )
        to_add = {sid: name for sid, name in studios.items() if sid not in existing_studio_ids}

        report: dict[str, Any] = {
            "dry_run": dry_run,
            "already_migrated": already,
            "series_count": len(series_rows),
            "distinct_studios": len(studios),
            "studios": [{"id": k, "name": v} for k, v in sorted(studios.items())],
            "already_tracked_studios": sorted(existing_studio_ids & set(studios)),
            "unresolved_series": unresolved,
            "series_deleted": 0,
            "studios_added": 0,
            "backup_path": None,
            "auto_send": auto_send,
        }

        if dry_run:
            return report
        if already:
            report["note"] = "已執行過(app_meta flag 已設),未重複執行"
            return report

        report["backup_path"] = _backup_db()

        for sid, sname in to_add.items():
            session.add(
                TrackedListing(kind="studio", id=sid, name=sname, auto_send=auto_send)
            )
        report["studios_added"] = len(to_add)

        for r in series_rows:
            await session.delete(r)
        report["series_deleted"] = len(series_rows)

        await session.commit()

    async with engine.begin() as conn:
        await _meta_set(conn, MIGRATION_FLAG, "1")
    logger.info(
        "series→studio migration done: -%d series, +%d studios",
        report["series_deleted"], report["studios_added"],
    )
    return report
