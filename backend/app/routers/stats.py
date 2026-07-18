"""Dashboard statistics.

One DB-only aggregation endpoint. Storage quotas are deliberately NOT
fetched here — the frontend reads the existing /api/pikpak/status and
/api/pcloud/status in parallel so a slow cloud API can't stall the
dashboard numbers.
"""

from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_session
from ..models import CollectedMovie, OfflineTaskLog, PCloudTransfer, TrackedListing
from ..schemas import DashboardStats, TopItem, TrackedTopItem, TrendPoint

router = APIRouter(prefix="/api/stats", tags=["stats"])

_TREND_DAYS = 30
_TOP_N = 10


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard(session: AsyncSession = Depends(get_session)) -> DashboardStats:
    # ----- collection -----
    by_status = dict(
        (
            await session.execute(
                select(CollectedMovie.status, func.count()).group_by(CollectedMovie.status)
            )
        ).all()
    )
    collection_total = sum(by_status.values())

    # ----- offline downloads -----
    by_phase = dict(
        (
            await session.execute(
                select(OfflineTaskLog.phase, func.count())
                .where(OfflineTaskLog.abandoned.is_(False))
                .group_by(OfflineTaskLog.phase)
            )
        ).all()
    )
    downloads_total = sum(by_phase.values())
    # Uniform rule: every dashboard aggregate excludes abandoned rows —
    # keeping archive_rate's numerator and denominator symmetric (a rare
    # abandoned row whose file later lands can end up archived=True too).
    archived_count = (
        await session.execute(
            select(func.count())
            .select_from(OfflineTaskLog)
            .where(OfflineTaskLog.archived, OfflineTaskLog.abandoned.is_(False))
        )
    ).scalar_one()
    # Rate against rows that actually produced a file — pure failures
    # (no file_id) can never be archived and would just dilute the rate.
    # Abandoned rows are excluded too: post-#203 a dead-lettered row can
    # carry a stale nonempty file_id that will never be archived.
    with_file = (
        await session.execute(
            select(func.count())
            .select_from(OfflineTaskLog)
            .where(
                OfflineTaskLog.file_id != "",
                OfflineTaskLog.abandoned.is_(False),
            )
        )
    ).scalar_one()
    archive_rate = archived_count / with_file if with_file else 0.0

    # ----- 30-day trend -----
    cutoff = datetime.utcnow() - timedelta(days=_TREND_DAYS - 1)
    cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    sent_per_day = dict(
        (
            await session.execute(
                select(func.date(OfflineTaskLog.created_at), func.count())
                .where(
                    OfflineTaskLog.created_at >= cutoff,
                    OfflineTaskLog.abandoned.is_(False),
                )
                .group_by(func.date(OfflineTaskLog.created_at))
            )
        ).all()
    )
    archived_per_day = dict(
        (
            await session.execute(
                select(func.date(OfflineTaskLog.archived_at), func.count())
                .where(
                    OfflineTaskLog.archived_at >= cutoff,
                    OfflineTaskLog.abandoned.is_(False),
                )
                .group_by(func.date(OfflineTaskLog.archived_at))
            )
        ).all()
    )
    trend = []
    for i in range(_TREND_DAYS):
        day = (cutoff + timedelta(days=i)).date().isoformat()
        trend.append(
            TrendPoint(
                date=day,
                sent=int(sent_per_day.get(day, 0)),
                archived=int(archived_per_day.get(day, 0)),
            )
        )

    # ----- tracked listings -----
    tracked_by_kind = dict(
        (
            await session.execute(
                select(TrackedListing.kind, func.count()).group_by(TrackedListing.kind)
            )
        ).all()
    )
    tracked_new_total = (
        await session.execute(select(func.coalesce(func.sum(TrackedListing.new_count), 0)))
    ).scalar_one()
    top_new_rows = (
        (
            await session.execute(
                select(TrackedListing)
                .where(TrackedListing.new_count > 0)
                .order_by(TrackedListing.new_count.desc())
                .limit(_TOP_N)
            )
        )
        .scalars()
        .all()
    )
    tracked_top_new = [
        TrackedTopItem(kind=r.kind, id=r.id, name=r.name or r.id, new_count=r.new_count)
        for r in top_new_rows
    ]

    # ----- top actresses / genres (JSON columns, Python-side) -----
    actress_counter: Counter[str] = Counter()
    genre_counter: Counter[str] = Counter()
    rows = (
        await session.execute(select(CollectedMovie.actresses, CollectedMovie.genres))
    ).all()
    for actresses, genres in rows:
        actress_counter.update(a for a in (actresses or []) if a)
        genre_counter.update(g for g in (genres or []) if g)
    top_actresses = [TopItem(name=n, count=c) for n, c in actress_counter.most_common(_TOP_N)]
    top_genres = [TopItem(name=n, count=c) for n, c in genre_counter.most_common(_TOP_N)]

    # ----- pCloud transfers -----
    transfers_by_status = dict(
        (
            await session.execute(
                select(PCloudTransfer.status, func.count()).group_by(PCloudTransfer.status)
            )
        ).all()
    )

    return DashboardStats(
        collection_total=collection_total,
        collection_by_status=by_status,
        downloads_total=downloads_total,
        downloads_by_phase=by_phase,
        archived_count=archived_count,
        archive_rate=round(archive_rate, 4),
        trend=trend,
        tracked_total=sum(tracked_by_kind.values()),
        tracked_by_kind=tracked_by_kind,
        tracked_new_total=int(tracked_new_total),
        tracked_top_new=tracked_top_new,
        top_actresses=top_actresses,
        top_genres=top_genres,
        pcloud_transfers_by_status=transfers_by_status,
        built_at=datetime.utcnow(),
    )
