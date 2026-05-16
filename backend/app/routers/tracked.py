from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import TrackedListing
from ..schemas import (
    CheckListingResult,
    TrackedListingIn,
    TrackedListingOut,
)
from ..services import tracker
from ..scrapers.javbus import LISTING_KINDS

router = APIRouter(prefix="/api/tracked", tags=["tracked"])

# Subset of LISTING_KINDS that we can sensibly track for "new works".
# Genre changes too fast to be useful as a "new" feed.
_ALLOWED = {"star", "studio", "label", "series", "director"}


def _to_out(r: TrackedListing) -> TrackedListingOut:
    return TrackedListingOut(
        kind=r.kind,
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


# ---------- tracker-loop status ----------


@router.get("/status")
async def tracker_status():
    return tracker.state.to_dict()


@router.post("/status/toggle")
async def tracker_toggle(enabled: bool = Body(..., embed=True)):
    tracker.state.enabled = enabled
    return tracker.state.to_dict()


@router.post("/status/run-now")
async def tracker_run_now():
    results = await tracker.check_all()
    new_total = sum(len(r.get("new_codes") or []) for r in results)
    tracker.state.last_new_total = new_total
    tracker.state.last_run = datetime.utcnow()
    return {
        "results": results,
        "new_total": new_total,
        **tracker.state.to_dict(),
    }


# ---------- CRUD ----------


@router.get("", response_model=list[TrackedListingOut])
async def list_tracked(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(TrackedListing).order_by(TrackedListing.created_at.desc())
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=TrackedListingOut)
async def upsert_tracked(
    payload: TrackedListingIn, session: AsyncSession = Depends(get_session)
):
    kind = payload.kind.strip()
    slug = payload.id.strip()
    if kind not in _ALLOWED:
        raise HTTPException(status_code=400, detail=f"不支援的 kind: {kind}")
    if not slug:
        raise HTTPException(status_code=400, detail="missing id")

    row = await session.get(TrackedListing, (kind, slug))
    if row:
        row.name = payload.name or row.name
        row.avatar = payload.avatar or row.avatar
        row.uncensored = payload.uncensored
        row.auto_send = payload.auto_send
    else:
        row = TrackedListing(
            kind=kind,
            id=slug,
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


@router.get("/{kind}/{slug}", response_model=TrackedListingOut)
async def get_tracked(
    kind: str, slug: str, session: AsyncSession = Depends(get_session)
):
    row = await session.get(TrackedListing, (kind, slug))
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    return _to_out(row)


@router.delete("/{kind}/{slug}")
async def untrack(
    kind: str, slug: str, session: AsyncSession = Depends(get_session)
):
    row = await session.get(TrackedListing, (kind, slug))
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.post("/{kind}/{slug}/check", response_model=CheckListingResult)
async def check_now(kind: str, slug: str):
    return CheckListingResult(**await tracker.check_listing(kind, slug))


@router.post("/{kind}/{slug}/reset-new-count")
async def reset_new_count(
    kind: str, slug: str, session: AsyncSession = Depends(get_session)
):
    row = await session.get(TrackedListing, (kind, slug))
    if not row:
        raise HTTPException(status_code=404, detail="not tracked")
    row.new_count = 0
    await session.commit()
    return {"ok": True}
