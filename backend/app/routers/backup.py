"""JSON backup / restore for the user's data: collection, tracked
actresses and offline-submission history."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import CollectedMovie, OfflineTaskLog, TrackedListing
from ..scrapers.javbus import extract_btih
from ..services import auto_backup

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/auto/status")
async def auto_backup_status():
    return await auto_backup.status()


@router.post("/auto/run")
async def auto_backup_run():
    try:
        dest = await auto_backup.run_backup()
    except Exception as exc:  # noqa: BLE001 — surface as API error
        raise HTTPException(status_code=500, detail=f"備份失敗: {exc}") from exc
    return {"ok": True, "file": dest.name, "status": await auto_backup.status()}


def _row_to_dict(row: Any, keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        v = getattr(row, k, None)
        if isinstance(v, datetime):
            v = v.isoformat()
        out[k] = v
    return out


COLLECTION_KEYS = [
    "code", "title", "cover", "release_date", "duration",
    "actresses", "genres", "note", "status", "created_at", "updated_at",
]
TRACKED_KEYS = [
    "kind", "id", "name", "avatar", "uncensored", "auto_send",
    "last_seen_code", "last_checked_at", "last_error",
    "new_count", "created_at",
]
HISTORY_KEYS = [
    "code", "magnet", "task_id", "file_id", "name", "phase",
    "message", "archived", "archived_at", "created_at",
]


@router.get("")
async def export_backup(session: AsyncSession = Depends(get_session)):
    collection = (await session.execute(select(CollectedMovie))).scalars().all()
    tracked = (await session.execute(select(TrackedListing))).scalars().all()
    history = (await session.execute(select(OfflineTaskLog))).scalars().all()

    payload = {
        "version": 1,
        "exported_at": datetime.utcnow().isoformat(),
        "counts": {
            "collection": len(collection),
            "tracked": len(tracked),
            "history": len(history),
        },
        "collection": [_row_to_dict(r, COLLECTION_KEYS) for r in collection],
        "tracked": [_row_to_dict(r, TRACKED_KEYS) for r in tracked],
        "history": [_row_to_dict(r, HISTORY_KEYS) for r in history],
    }

    filename = f"avbt-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", ""))
    except ValueError:
        return None


@router.post("/restore")
async def import_backup(
    payload: dict = Body(...),
    overwrite: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    if not isinstance(payload, dict) or "version" not in payload:
        raise HTTPException(status_code=400, detail="不像是合法的備份 JSON")

    stats = {
        "collection": {"added": 0, "updated": 0, "skipped": 0},
        "tracked": {"added": 0, "updated": 0, "skipped": 0},
        "history": {"added": 0, "skipped": 0},
    }
    now = datetime.utcnow()

    for item in payload.get("collection", []) or []:
        code = (item.get("code") or "").strip().upper()
        if not code:
            stats["collection"]["skipped"] += 1
            continue
        existing = await session.get(CollectedMovie, code)
        if existing:
            if not overwrite:
                stats["collection"]["skipped"] += 1
                continue
            existing.title = item.get("title") or existing.title
            existing.cover = item.get("cover") or existing.cover
            existing.release_date = item.get("release_date") or existing.release_date
            existing.duration = item.get("duration") or existing.duration
            existing.actresses = item.get("actresses") or existing.actresses
            existing.genres = item.get("genres") or existing.genres
            existing.note = item.get("note", existing.note)
            existing.status = item.get("status") or existing.status
            existing.updated_at = now
            stats["collection"]["updated"] += 1
        else:
            row = CollectedMovie(
                code=code,
                title=item.get("title") or "",
                cover=item.get("cover") or "",
                release_date=item.get("release_date") or "",
                duration=item.get("duration") or "",
                actresses=item.get("actresses") or [],
                genres=item.get("genres") or [],
                note=item.get("note") or "",
                status=item.get("status") or "wishlist",
                created_at=_parse_dt(item.get("created_at")) or now,
                updated_at=_parse_dt(item.get("updated_at")) or now,
            )
            session.add(row)
            stats["collection"]["added"] += 1

    for item in payload.get("tracked", []) or []:
        slug = (item.get("id") or "").strip()
        kind = (item.get("kind") or "star").strip()  # old backups had no kind
        if not slug:
            stats["tracked"]["skipped"] += 1
            continue
        existing = await session.get(TrackedListing, (kind, slug))
        if existing:
            if not overwrite:
                stats["tracked"]["skipped"] += 1
                continue
            existing.name = item.get("name") or existing.name
            existing.avatar = item.get("avatar") or existing.avatar
            existing.uncensored = bool(item.get("uncensored"))
            existing.auto_send = bool(item.get("auto_send"))
            stats["tracked"]["updated"] += 1
        else:
            row = TrackedListing(
                kind=kind,
                id=slug,
                name=item.get("name") or "",
                avatar=item.get("avatar") or "",
                uncensored=bool(item.get("uncensored")),
                auto_send=bool(item.get("auto_send")),
                last_seen_code=item.get("last_seen_code") or "",
                last_checked_at=_parse_dt(item.get("last_checked_at")),
                last_error=item.get("last_error") or "",
                new_count=int(item.get("new_count") or 0),
                created_at=_parse_dt(item.get("created_at")) or now,
            )
            session.add(row)
            stats["tracked"]["added"] += 1

    # History rows have no natural primary key beyond their auto-increment
    # id, so we de-dup by (task_id) or (magnet+created_at).
    existing_task_ids = set(
        (await session.execute(select(OfflineTaskLog.task_id))).scalars().all()
    )
    existing_task_ids.discard("")

    for item in payload.get("history", []) or []:
        magnet = item.get("magnet") or ""
        if not magnet:
            stats["history"]["skipped"] += 1
            continue
        task_id = item.get("task_id") or ""
        if task_id and task_id in existing_task_ids:
            stats["history"]["skipped"] += 1
            continue
        row = OfflineTaskLog(
            code=item.get("code") or "",
            magnet=magnet,
            btih=extract_btih(magnet),
            task_id=task_id,
            file_id=item.get("file_id") or "",
            name=item.get("name") or "",
            phase=item.get("phase") or "",
            message=item.get("message") or "",
            archived=bool(item.get("archived")),
            archived_at=_parse_dt(item.get("archived_at")),
            created_at=_parse_dt(item.get("created_at")) or now,
        )
        session.add(row)
        if task_id:
            existing_task_ids.add(task_id)
        stats["history"]["added"] += 1

    await session.commit()
    # Restored rows can change which studios count as tracked — the
    # archiver's tracked-name cache must not keep routing on the old set.
    from ..services import archiver as archiver_svc
    archiver_svc._tracked_name_cache.clear()
    return {"ok": True, "stats": stats}
