from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import CollectedMovie, OfflineTaskLog
from ..schemas import CollectionIn, CollectionOut
from ..scrapers.javbus import extract_btih

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


@router.get("/sent-hashes", response_model=list[str])
async def sent_hashes(session: AsyncSession = Depends(get_session)):
    """btih hashes of every magnet we've previously submitted to PikPak."""
    rows = (await session.execute(select(OfflineTaskLog.magnet))).scalars().all()
    seen: set[str] = set()
    for magnet in rows:
        h = extract_btih(magnet or "")
        if h:
            seen.add(h)
    return sorted(seen)
