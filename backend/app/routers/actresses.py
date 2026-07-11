"""女優 browse page API.

Actresses aggregated from downloaded (PikPak-present) codes joined
against the persistent detail cache. Coverage grows as the
detail_backfill worker fills in missing details/avatars; the index
endpoint exposes that progress so the UI can show a banner.
"""

from fastapi import APIRouter, Body, HTTPException

from ..schemas import (
    ActressBackfillStatus,
    ActressIndexItem,
    ActressIndexOut,
    ActressWorksOut,
)
from ..services import actress_index, detail_backfill

router = APIRouter(prefix="/api/actresses", tags=["actresses"])


@router.get("", response_model=ActressIndexOut)
async def actress_list():
    agg = await actress_index.get()
    return ActressIndexOut(
        actresses=[
            ActressIndexItem(
                name=e.name,
                id=e.id,
                count=len(e.works),
                avatar=e.avatar,
                sample_cover=e.sample_cover,
            )
            for e in agg.actresses.values()
        ],
        downloaded_total=agg.downloaded_total,
        indexed_total=agg.indexed_total,
        backfill=ActressBackfillStatus(**detail_backfill.state.to_dict()),
    )


@router.get("/{name}/works", response_model=ActressWorksOut)
async def actress_works(name: str):
    entry = await actress_index.works_for(name)
    if entry is None:
        raise HTTPException(status_code=404, detail="查無此女優(或尚未建檔)")
    return ActressWorksOut(
        name=entry.name,
        id=entry.id,
        avatar=entry.avatar,
        count=len(entry.works),
        works=entry.works,
    )


@router.post("/backfill/toggle", response_model=ActressBackfillStatus)
async def backfill_toggle(enabled: bool = Body(..., embed=True)):
    detail_backfill.state.enabled = enabled
    return ActressBackfillStatus(**detail_backfill.state.to_dict())
