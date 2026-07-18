import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import SessionLocal, get_session
from ..models import CollectedMovie, OfflineTaskLog
from ..schemas import (
    CollectionIn,
    CollectionOut,
    HistoryItem,
    HistoryPage,
    SendAllOptions,
    SendAllResult,
)
from ..scrapers.javbus import JavbusBlocked
from ..services import bulk
from ..services.download_queue import all_sent_hashes
from ..services.pikpak_presence import presence_index

router = APIRouter(prefix="/api/collection", tags=["collection"])


def _to_out(row: CollectedMovie) -> CollectionOut:
    return CollectionOut(
        code=row.code,
        title=row.title,
        cover=row.cover,
        release_date=row.release_date,
        duration=row.duration,
        actresses=row.actresses or [],
        genres=row.genres or [],
        note=row.note,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[CollectionOut])
async def list_items(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(CollectedMovie).order_by(CollectedMovie.updated_at.desc())
    if status:
        stmt = stmt.where(CollectedMovie.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=CollectionOut)
async def upsert_item(payload: CollectionIn, session: AsyncSession = Depends(get_session)):
    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="缺少 code")
    existing = await session.get(CollectedMovie, code)
    now = datetime.utcnow()
    if existing:
        existing.title = payload.title or existing.title
        existing.cover = payload.cover or existing.cover
        existing.release_date = payload.release_date or existing.release_date
        existing.duration = payload.duration or existing.duration
        existing.actresses = payload.actresses or existing.actresses
        existing.genres = payload.genres or existing.genres
        existing.note = payload.note if payload.note != "" else existing.note
        existing.status = payload.status or existing.status
        existing.updated_at = now
        row = existing
    else:
        row = CollectedMovie(
            code=code,
            title=payload.title,
            cover=payload.cover,
            release_date=payload.release_date,
            duration=payload.duration,
            actresses=payload.actresses,
            genres=payload.genres,
            note=payload.note,
            status=payload.status,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.delete("/{code}")
async def delete_item(code: str, session: AsyncSession = Depends(get_session)):
    code = code.strip().upper()
    row = await session.get(CollectedMovie, code)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


_STATUS_RANK = {"wishlist": 0, "downloading": 1, "done": 2}


@router.post("/sync-status")
async def sync_status(session: AsyncSession = Depends(get_session)):
    """Reconcile collection statuses with cloud reality.

    Forward-only: wishlist → downloading (a submitted OfflineTaskLog
    row exists) → done (the code is present in PikPak per the presence
    index, or its log row is archived). Never downgrades — a manually
    set「完成」stays even if the file was later removed."""
    presence = await presence_index.get()

    rows = (await session.execute(select(CollectedMovie))).scalars().all()
    sent_codes = set(
        (
            await session.execute(
                select(OfflineTaskLog.code).where(OfflineTaskLog.code != "").distinct()
            )
        ).scalars().all()
    )
    archived_codes = set(
        (
            await session.execute(
                select(OfflineTaskLog.code)
                .where(OfflineTaskLog.code != "", OfflineTaskLog.archived == True)  # noqa: E712
                .distinct()
            )
        ).scalars().all()
    )

    updated = {"downloading": 0, "done": 0}
    now = datetime.utcnow()
    for row in rows:
        code = (row.code or "").upper()
        target = None
        if code in presence or code in archived_codes:
            target = "done"
        elif code in sent_codes:
            target = "downloading"
        if target and _STATUS_RANK.get(target, 0) > _STATUS_RANK.get(row.status, 0):
            row.status = target
            row.updated_at = now
            updated[target] += 1
    await session.commit()
    return {
        "checked": len(rows),
        "to_downloading": updated["downloading"],
        "to_done": updated["done"],
    }


@router.get("/sent-hashes", response_model=list[str])
async def sent_hashes():
    """btih hashes of every magnet we've previously submitted to PikPak.

    Served from the download queue's process-wide btih cache (warmed at
    startup, appended on every send) — the movie page calls this on
    every load, and the previous implementation re-scanned all magnet
    strings from the table each time."""
    return sorted(await all_sent_hashes())


@router.get("/history", response_model=HistoryPage)
async def history(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    code: str | None = None,
    archived: bool | None = None,
    abandoned: bool | None = None,
    phase: str | None = None,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(OfflineTaskLog)
    count_stmt = select(func.count()).select_from(OfflineTaskLog)
    if code:
        cond = OfflineTaskLog.code == code.strip().upper()
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    if archived is not None:
        stmt = stmt.where(OfflineTaskLog.archived == archived)
        count_stmt = count_stmt.where(OfflineTaskLog.archived == archived)
    if abandoned is not None:
        stmt = stmt.where(OfflineTaskLog.abandoned == abandoned)
        count_stmt = count_stmt.where(OfflineTaskLog.abandoned == abandoned)
    if phase:
        stmt = stmt.where(OfflineTaskLog.phase == phase)
        count_stmt = count_stmt.where(OfflineTaskLog.phase == phase)
    if q and q.strip():
        like = f"%{q.strip()}%"
        cond = OfflineTaskLog.name.like(like)
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    stmt = stmt.order_by(OfflineTaskLog.created_at.desc()).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).scalars().all()
    total = (await session.execute(count_stmt)).scalar_one()
    items = [
        HistoryItem(
            id=r.id,
            code=r.code,
            magnet=r.magnet,
            task_id=r.task_id,
            file_id=r.file_id,
            name=r.name,
            phase=r.phase,
            message=r.message,
            archived=bool(r.archived),
            archived_at=r.archived_at,
            abandoned=bool(r.abandoned),
            created_at=r.created_at,
        )
        for r in rows
    ]
    return HistoryPage(items=items, total=int(total), offset=offset, limit=limit)


@router.delete("/history/{item_id}")
async def delete_history(item_id: int, session: AsyncSession = Depends(get_session)):
    row = await session.get(OfflineTaskLog, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.post("/history/batch-delete")
async def history_batch_delete(
    ids: list[int] = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
):
    if not ids:
        return {"deleted": 0}
    rows = (
        await session.execute(select(OfflineTaskLog).where(OfflineTaskLog.id.in_(ids)))
    ).scalars().all()
    for r in rows:
        await session.delete(r)
    await session.commit()
    return {"deleted": len(rows)}


@router.post("/history/batch-rearchive")
async def history_batch_rearchive(
    ids: list[int] = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
):
    """Flip selected rows back to un-archived so the archiver's next pass
    re-resolves and re-moves them. Useful after a file was manually moved
    back or landed in the wrong folder. Rows without a file can't be
    archived and are skipped."""
    if not ids:
        return {"updated": 0}
    rows = (
        await session.execute(
            select(OfflineTaskLog).where(
                OfflineTaskLog.id.in_(ids), OfflineTaskLog.file_id != ""
            )
        )
    ).scalars().all()
    for r in rows:
        r.archived = False
        r.archived_at = None
    await session.commit()
    return {"updated": len(rows)}


async def _wishlist_codes(status: str = "wishlist") -> list[str]:
    async with SessionLocal() as session:
        stmt = select(CollectedMovie.code).where(CollectedMovie.status == status)
        return (await session.execute(stmt)).scalars().all()


async def _promote_to_downloading(code: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(CollectedMovie, code)
        if row and row.status == "wishlist":
            row.status = "downloading"
            row.updated_at = datetime.utcnow()
            await session.commit()


@router.post("/send-wishlist", response_model=SendAllResult)
async def send_wishlist(options: SendAllOptions):
    codes = await _wishlist_codes()
    final = SendAllResult()
    try:
        async for event in bulk.send_codes_stream(
            codes, options, on_sent=_promote_to_downloading
        ):
            if event["type"] == "done":
                final = SendAllResult(**event["result"])
    except JavbusBlocked as exc:
        raise HTTPException(status_code=451, detail=str(exc)) from exc
    return final


@router.post("/send-wishlist/stream")
async def send_wishlist_stream(options: SendAllOptions):
    codes = await _wishlist_codes()

    async def gen():
        try:
            async for event in bulk.send_codes_stream(
                codes, options, on_sent=_promote_to_downloading
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except JavbusBlocked as exc:
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---------- batch operations on the collection ----------


@router.post("/batch/status")
async def batch_status(
    codes: list[str] = Body(..., embed=True),
    status: str = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
):
    if status not in {"wishlist", "downloading", "done"}:
        raise HTTPException(status_code=400, detail=f"不支援的 status: {status}")
    codes = [c.strip().upper() for c in codes if c.strip()]
    if not codes:
        return {"updated": 0}
    now = datetime.utcnow()
    rows = (
        await session.execute(
            select(CollectedMovie).where(CollectedMovie.code.in_(codes))
        )
    ).scalars().all()
    for r in rows:
        r.status = status
        r.updated_at = now
    await session.commit()
    return {"updated": len(rows)}


@router.post("/batch/delete")
async def batch_delete(
    codes: list[str] = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
):
    codes = [c.strip().upper() for c in codes if c.strip()]
    if not codes:
        return {"deleted": 0}
    rows = (
        await session.execute(
            select(CollectedMovie).where(CollectedMovie.code.in_(codes))
        )
    ).scalars().all()
    for r in rows:
        await session.delete(r)
    await session.commit()
    return {"deleted": len(rows)}


@router.post("/batch/add")
async def batch_add(
    items: list[CollectionIn] = Body(..., embed=True),
    session: AsyncSession = Depends(get_session),
):
    """Bulk add-to-collection (missing page multi-select). Existing codes
    are left untouched — this is add-only, not upsert."""
    now = datetime.utcnow()
    added = 0
    skipped = 0
    for item in items:
        code = item.code.strip().upper()
        if not code:
            skipped += 1
            continue
        if await session.get(CollectedMovie, code):
            skipped += 1
            continue
        session.add(
            CollectedMovie(
                code=code,
                title=item.title,
                cover=item.cover,
                release_date=item.release_date,
                duration=item.duration,
                actresses=item.actresses,
                genres=item.genres,
                note=item.note,
                status=item.status or "wishlist",
                created_at=now,
                updated_at=now,
            )
        )
        added += 1
    await session.commit()
    return {"added": added, "skipped": skipped}


@router.post("/send-by-codes/stream")
async def send_by_codes_stream(payload: dict = Body(...)):
    """Stream-submit an arbitrary list of codes to PikPak using the same
    pipeline as send-wishlist. Body shape:
        {codes: ["ABC-001", ...], ...SendAllOptions fields}
    """
    codes = payload.get("codes") or []
    if not isinstance(codes, list):
        codes = []
    options_data = {k: v for k, v in payload.items() if k != "codes"}
    try:
        options = SendAllOptions(**options_data)
    except Exception:  # noqa: BLE001 — bad options fall back to defaults
        options = SendAllOptions()

    async def gen():
        try:
            async for event in bulk.send_codes_stream(
                codes, options, on_sent=_promote_to_downloading
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except JavbusBlocked as exc:
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"
        except Exception as exc:  # noqa: BLE001
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")
