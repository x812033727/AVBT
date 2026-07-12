"""One-time hard cutover: series / label tracking → studio tracking.

Deletes every ``kind=<from_kind>`` TrackedListing (``series`` or
``label``) and replaces it with the distinct studios (製作商) those
listings belong to, resolved from the persistent ``movie_detail_cache``
(dominant studio per listing). Existing ``kind='studio'`` rows and other
kinds are left untouched.

Destructive and user-gated: the admin endpoints default to ``dry_run``,
and a real run backs the SQLite DB up before mutating. Each source kind
is guarded by its own ``migrated:<from_kind>_to_studio`` app_meta flag so
it can't double-apply.
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

# Kinds that sit UNDER a studio in the JavBus hierarchy and can therefore
# be rolled up to their maker. ``MovieDetail`` exposes each as a same-named
# ``LinkRef`` attribute (``detail.series`` / ``detail.label``).
_ROLLUP_KINDS = ("series", "label")


def _flag_for(from_kind: str) -> str:
    return f"migrated:{from_kind}_to_studio"


async def _resolve_kind_to_studio(
    session, from_kind: str
) -> tuple[dict[str, str], list[dict], list]:
    """Return ({studio_id: studio_name}, unresolved_rows, source_rows).

    Dominant studio per tracked listing of ``from_kind``, read from the
    detail cache via the same-named ``MovieDetail`` attribute."""
    source_rows = (
        await session.execute(
            select(TrackedListing).where(TrackedListing.kind == from_kind)
        )
    ).scalars().all()
    source_ids = {r.id for r in source_rows}

    studio_by_source: dict[str, Counter] = defaultdict(Counter)
    studio_names: dict[str, str] = {}
    if source_ids:
        cache_rows = (
            await session.execute(select(MovieDetailCache.detail))
        ).all()
        for (detail_json,) in cache_rows:
            try:
                d = MovieDetail.model_validate_json(detail_json)
            except Exception:  # noqa: BLE001 — skip a corrupt row
                continue
            ref = getattr(d, from_kind, None)
            if not (ref and ref.id in source_ids):
                continue
            if d.studio and d.studio.id:
                studio_by_source[ref.id][d.studio.id] += 1
                studio_names[d.studio.id] = (d.studio.name or "").strip() or d.studio.id

    studios: dict[str, str] = {}
    unresolved: list[dict] = []
    for r in source_rows:
        counter = studio_by_source.get(r.id)
        if not counter:
            unresolved.append({"id": r.id, "name": r.name})
            continue
        sid = counter.most_common(1)[0][0]
        studios[sid] = studio_names.get(sid, sid)
    return studios, unresolved, source_rows


def _backup_db(from_kind: str) -> str | None:
    """Copy the live SQLite file before a destructive run. Returns the
    backup path, or ``None`` for an in-memory / missing DB (tests)."""
    db_path = engine.url.database
    if not db_path or db_path == ":memory:":
        return None
    src = Path(db_path)
    if not src.exists():
        return None
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(f"{src.name}.bak-{from_kind}-to-studio-{stamp}")
    shutil.copy2(src, dst)
    logger.info("%s→studio migration: DB backed up to %s", from_kind, dst)
    return str(dst)


async def migrate_kind_to_studio(
    from_kind: str, *, dry_run: bool = True, auto_send: bool = True
) -> dict[str, Any]:
    """Hard-cut ``from_kind`` (series|label) tracking → studio tracking.

    Idempotent via a per-kind app_meta flag. ``dry_run`` (default) reports
    the plan without mutating; a real run backs up the DB, upserts the new
    studios, deletes the source rows, and sets the flag."""
    if from_kind not in _ROLLUP_KINDS:
        raise ValueError(f"from_kind must be one of {_ROLLUP_KINDS}, got {from_kind!r}")
    flag = _flag_for(from_kind)

    async with engine.begin() as conn:
        already = await _meta_get(conn, flag) == "1"

    async with SessionLocal() as session:
        studios, unresolved, source_rows = await _resolve_kind_to_studio(
            session, from_kind
        )

        existing_studio_ids = set(
            (
                await session.execute(
                    select(TrackedListing.id).where(TrackedListing.kind == "studio")
                )
            ).scalars().all()
        )
        to_add = {
            sid: name for sid, name in studios.items() if sid not in existing_studio_ids
        }

        report: dict[str, Any] = {
            "dry_run": dry_run,
            "already_migrated": already,
            "source_kind": from_kind,
            "source_count": len(source_rows),
            "distinct_studios": len(studios),
            "studios": [{"id": k, "name": v} for k, v in sorted(studios.items())],
            "already_tracked_studios": sorted(existing_studio_ids & set(studios)),
            "unresolved": unresolved,
            "source_deleted": 0,
            "studios_added": 0,
            "backup_path": None,
            "auto_send": auto_send,
        }

        if dry_run:
            return report
        if already:
            report["note"] = "已執行過(app_meta flag 已設),未重複執行"
            return report

        report["backup_path"] = _backup_db(from_kind)

        for sid, sname in to_add.items():
            session.add(
                TrackedListing(kind="studio", id=sid, name=sname, auto_send=auto_send)
            )
        report["studios_added"] = len(to_add)

        for r in source_rows:
            await session.delete(r)
        report["source_deleted"] = len(source_rows)

        await session.commit()

    async with engine.begin() as conn:
        await _meta_set(conn, flag, "1")
    logger.info(
        "%s→studio migration done: -%d %s, +%d studios",
        from_kind, report["source_deleted"], from_kind, report["studios_added"],
    )
    return report


async def migrate_series_to_studio(
    *, dry_run: bool = True, auto_send: bool = True
) -> dict[str, Any]:
    """Backward-compatible wrapper for the series cutover. Adds the
    legacy ``series_*`` report aliases the original endpoint returned."""
    r = await migrate_kind_to_studio("series", dry_run=dry_run, auto_send=auto_send)
    r["series_count"] = r["source_count"]
    r["series_deleted"] = r["source_deleted"]
    r["unresolved_series"] = r["unresolved"]
    return r
