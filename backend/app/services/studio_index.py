"""製作商 (studio) → 系列 (series) → 影片 aggregation for the studio
browse page.

Mirrors ``actress_index``: joins the PikPak presence index (which codes
are physically downloaded) with the persistent ``movie_detail_cache``
(whose detail JSON carries ``studio`` and ``series``). Rows are read
directly, IGNORING the detail-cache TTL — a stale row's studio/series is
still correct. Unlike actresses, a movie belongs to exactly one studio
and (at most) one series, so this is a two-level GROUP BY instead of a
fan-out over a cast list. Movies with no series land in a synthetic
``未分類`` bucket keyed by ``NO_SERIES`` so the UI depth stays uniform.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from ..database import SessionLocal
from ..models import MovieDetailCache
from ..schemas import MovieDetail, MovieListItem
from .pikpak_presence import presence_index

logger = logging.getLogger(__name__)

_TTL_SECONDS = 60.0

# Sentinel series id/name for movies whose detail has no series. Kept
# distinct from a real slug (real slugs never contain a space) and
# surfaced to the frontend as the ``_none`` path segment.
NO_SERIES = "_none"
NO_SERIES_NAME = "未分類"


@dataclass
class SeriesEntry:
    id: str
    name: str
    sample_cover: str = ""
    works: list[MovieListItem] = field(default_factory=list)


@dataclass
class StudioEntry:
    id: str
    name: str
    sample_cover: str = ""
    work_count: int = 0
    series: dict[str, SeriesEntry] = field(default_factory=dict)


@dataclass
class StudioAggregation:
    studios: dict[str, StudioEntry] = field(default_factory=dict)
    downloaded_total: int = 0
    indexed_total: int = 0


_cache: StudioAggregation | None = None
_built_at = 0.0
_lock = asyncio.Lock()


def invalidate() -> None:
    global _built_at
    _built_at = 0.0


def _sort_works(works: list[MovieListItem]) -> None:
    # Newest first, undated last; stable code order within a date.
    works.sort(key=lambda w: w.code)
    works.sort(key=lambda w: w.date, reverse=True)


async def _build() -> StudioAggregation:
    downloaded = presence_index.peek()
    if downloaded is None:
        try:
            downloaded = await presence_index.get()
        except Exception as exc:  # noqa: BLE001 — presence down ≠ page down
            logger.warning("studio index: presence unavailable: %s", exc)
            return StudioAggregation()

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(MovieDetailCache.code, MovieDetailCache.detail)
            )
        ).all()

    agg = StudioAggregation(downloaded_total=len(downloaded))
    for code, detail_json in rows:
        if code not in downloaded:
            continue
        try:
            detail = MovieDetail.model_validate_json(detail_json)
        except Exception:  # noqa: BLE001 — one corrupt row must not kill the page
            logger.warning("studio index: corrupt detail row for %s", code)
            continue
        studio = detail.studio
        sid = (studio.id or "").strip() if studio else ""
        if not sid:
            # No studio → not browsable in this hierarchy.
            continue
        agg.indexed_total += 1
        item = MovieListItem(
            code=code,
            title=detail.title,
            cover=detail.cover,
            detail_url="",
            date=detail.release_date or "",
        )
        entry = agg.studios.get(sid)
        if entry is None:
            entry = StudioEntry(id=sid, name=(studio.name or "").strip() or sid)
            agg.studios[sid] = entry
        elif not entry.name or entry.name == entry.id:
            # A later row may carry the human-readable name.
            if studio.name:
                entry.name = studio.name.strip()

        series = detail.series
        ser_id = (series.id or "").strip() if series else ""
        if ser_id:
            ser_name = (series.name or "").strip() or ser_id
        else:
            ser_id, ser_name = NO_SERIES, NO_SERIES_NAME
        sentry = entry.series.get(ser_id)
        if sentry is None:
            sentry = SeriesEntry(id=ser_id, name=ser_name)
            entry.series[ser_id] = sentry
        elif ser_id != NO_SERIES and (not sentry.name or sentry.name == sentry.id):
            sentry.name = ser_name
        sentry.works.append(item)

    for entry in agg.studios.values():
        for sentry in entry.series.values():
            _sort_works(sentry.works)
            sentry.sample_cover = next((w.cover for w in sentry.works if w.cover), "")
        entry.work_count = sum(len(s.works) for s in entry.series.values())
        entry.sample_cover = next(
            (s.sample_cover for s in entry.series.values() if s.sample_cover), ""
        )
    return agg


async def get(*, force: bool = False) -> StudioAggregation:
    global _cache, _built_at
    if not force and _cache is not None and time.monotonic() - _built_at < _TTL_SECONDS:
        return _cache
    async with _lock:
        if not force and _cache is not None and time.monotonic() - _built_at < _TTL_SECONDS:
            return _cache
        _cache = await _build()
        _built_at = time.monotonic()
        return _cache


async def studio_for(studio_id: str) -> StudioEntry | None:
    agg = await get()
    return agg.studios.get((studio_id or "").strip())


async def series_for(studio_id: str, series_id: str) -> tuple[StudioEntry, SeriesEntry] | None:
    entry = await studio_for(studio_id)
    if entry is None:
        return None
    sentry = entry.series.get((series_id or "").strip())
    if sentry is None:
        return None
    return entry, sentry
