from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import TrackedActress
from ..schemas import CheckActressResult, TrackedActressIn, TrackedActressOut
from ..services import tracker

router = APIRouter(prefix="/api/tracked", tags=["tracked"])


def _to_out(r: TrackedActress) -> TrackedActressOut:
    return TrackedActressOut(
        id=r.id,
        name=r.name,
        avatar=r.avatar,
        uncensored=bool(r.uncensored),
        auto_send=bool(r.auto_send),
        last_seen_code=r.last_seen_code,
        last_checked_at=r.last_checked_at,
        last_error=r.last_error,
        new_count=int(r.new_count or 0),
        created_at=r.created_at,
    )


@router.get("", response_model=list[TrackedActressOut])
async def list_tracked(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(TrackedActress).order_by(TrackedActress.created_at.desc()))).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/{actress_id}", response_model=TrackedActressOut)
async def get_tracked(actress_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(TrackedActress, actress_id)
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    return _to_out(row)


@router.post("", response_model=TrackedActressOut)
async def upsert_tracked(payload: TrackedActressIn, session: AsyncSession = Depends(get_session)):
    actress_id = payload.id.strip()
    if not actress_id:
        raise HTTPException(status_code=400, detail="missing id")
    row = await session.get(TrackedActress, actress_id)
    if row:
        row.name = payload.name or row.name
        row.avatar = payload.avatar or row.avatar
        row.uncensored = payload.uncensored
        row.auto_send = payload.auto_send
    else:
        row = TrackedActress(
            id=actress_id,
            name=payload.name,
            avatar=payload.avatar,
            uncensored=payload.uncensored,
            auto_send=payload.auto_send,
            created_at=datetime.utcnow(),
        )
        session.add(row)
    await session.commit()
    await session.refresh(row)
    return _to_out(row)


@router.delete("/{actress_id}")
async def untrack(actress_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(TrackedActress, actress_id)
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.post("/{actress_id}/check", response_model=CheckActressResult)
async def check_now(actress_id: str):
    return CheckActressResult(**await tracker.check_actress(actress_id))


@router.post("/{actress_id}/reset-new-count")
async def reset_new_count(actress_id: str, session: AsyncSession = Depends(get_session)):
    row = await session.get(TrackedActress, actress_id)
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    row.new_count = 0
    await session.commit()
    return {"ok": True}
