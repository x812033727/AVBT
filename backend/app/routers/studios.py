"""製作商 (studio) browse page API.

Studios → series → downloaded works, aggregated from PikPak-present
codes joined against the persistent detail cache (see
``services.studio_index``). Coverage grows as the shared
``detail_backfill`` worker fills in missing details; this reuses the
same backfill status the actress page exposes.
"""

from fastapi import APIRouter, HTTPException

from ..schemas import (
    ActressBackfillStatus,
    StudioIndexItem,
    StudioIndexOut,
    StudioSeriesItem,
    StudioSeriesOut,
    StudioSeriesWorksOut,
)
from ..services import detail_backfill, studio_index

router = APIRouter(prefix="/api/studios", tags=["studios"])


@router.get("", response_model=StudioIndexOut)
async def studio_list():
    agg = await studio_index.get()
    return StudioIndexOut(
        studios=[
            StudioIndexItem(
                id=e.id,
                name=e.name,
                sample_cover=e.sample_cover,
                series_count=len(e.series),
                work_count=e.work_count,
            )
            for e in agg.studios.values()
        ],
        downloaded_total=agg.downloaded_total,
        indexed_total=agg.indexed_total,
        backfill=ActressBackfillStatus(**detail_backfill.state.to_dict()),
    )


@router.get("/{studio_id}/series", response_model=StudioSeriesOut)
async def studio_series(studio_id: str):
    entry = await studio_index.studio_for(studio_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="查無此製作商(或尚未建檔)")
    return StudioSeriesOut(
        studio_id=entry.id,
        studio_name=entry.name,
        sample_cover=entry.sample_cover,
        series_count=len(entry.series),
        work_count=entry.work_count,
        series=[
            StudioSeriesItem(
                id=s.id,
                name=s.name,
                sample_cover=s.sample_cover,
                work_count=len(s.works),
            )
            for s in entry.series.values()
        ],
    )


@router.get(
    "/{studio_id}/series/{series_id}/works",
    response_model=StudioSeriesWorksOut,
)
async def studio_series_works(studio_id: str, series_id: str):
    pair = await studio_index.series_for(studio_id, series_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="查無此系列(或尚未建檔)")
    entry, sentry = pair
    return StudioSeriesWorksOut(
        studio_id=entry.id,
        studio_name=entry.name,
        series_id=sentry.id,
        series_name=sentry.name,
        count=len(sentry.works),
        works=sentry.works,
    )
