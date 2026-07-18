"""Persistent JavBus detail cache backed by the ``movie_detail_cache`` table.

The scraper's in-memory cache (30-minute TTL, per-process) already
collapses concurrent callers; this layer survives restarts so a code that
was ever scraped isn't re-fetched across tracker cycles, archiver passes
and reboots. TTL is recency-aware: a recent release keeps gaining magnets
so its row expires fast, an old release is static and stays fresh for a
month. Both operations swallow their own errors — a DB hiccup must never
break scraping.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import MovieDetailCache
from ..schemas import MovieDetail

logger = logging.getLogger(__name__)


def _effective_ttl_seconds(release_date: str) -> int:
    """Missing / unparseable release dates count as recent — refreshing
    too often is the safe failure mode."""
    try:
        released = datetime.strptime(release_date, "%Y-%m-%d")
    except ValueError:
        return settings.javbus_persist_ttl_recent_seconds
    if datetime.utcnow() - released <= timedelta(
        days=settings.javbus_persist_recent_days
    ):
        return settings.javbus_persist_ttl_recent_seconds
    return settings.javbus_persist_ttl_old_seconds


async def get(code: str) -> MovieDetail | None:
    if not settings.javbus_persist_cache_enabled:
        return None
    try:
        async with SessionLocal() as session:
            row = await session.get(MovieDetailCache, code)
        if row is None or row.fetched_at is None:
            return None
        ttl = _effective_ttl_seconds(row.release_date or "")
        if datetime.utcnow() - row.fetched_at > timedelta(seconds=ttl):
            # Stale: report a miss but keep the row — it's the upsert
            # target for the refreshing fetch that follows.
            return None
        return MovieDetail.model_validate_json(row.detail)
    except Exception as exc:  # noqa: BLE001 — cache failure = cache miss
        logger.warning("detail cache read failed for %s: %s", code, exc)
        return None


async def get_many_lite(codes: list[str]) -> dict[str, dict]:
    """Batch-read {studio, series, genres} for browse-card enrichment.

    Deliberately ignores the TTL that ``get()`` enforces: identity fields
    (studio/series/genre) don't change after release the way duration and
    the magnet list do, so a "stale" row is still an honest answer for
    this projection — the freshness cost the TTL protects against
    (a growing magnet list) doesn't apply here. One SELECT for the whole
    batch; a code with no row, or a row whose JSON fails to parse, is
    simply omitted (module convention: cache failure = miss). Never
    touches the network — this is a cache-join only, no scraper fallback.
    """
    if not settings.javbus_persist_cache_enabled or not codes:
        return {}
    try:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(MovieDetailCache).where(MovieDetailCache.code.in_(codes))
                )
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001 — cache failure = cache miss
        logger.warning("detail cache batch read failed: %s", exc)
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        try:
            detail = MovieDetail.model_validate_json(row.detail)
        except Exception as exc:  # noqa: BLE001 — bad row = skip that code
            logger.warning("detail cache batch parse failed for %s: %s", row.code, exc)
            continue
        out[row.code] = {
            "studio": detail.studio.model_dump() if detail.studio else None,
            "series": detail.series.model_dump() if detail.series else None,
            "genres": [g.name for g in detail.genres[:4]],
        }
    return out


async def touch(code: str) -> None:
    """Advance ``fetched_at`` without changing the payload — a failed
    heal must rotate out of the backfill's oldest-first window instead
    of parking there forever (integration audit 2026-07-18)."""
    if not settings.javbus_persist_cache_enabled:
        return
    try:
        async with SessionLocal() as session:
            row = await session.get(MovieDetailCache, code.strip().upper())
            if row is not None:
                row.fetched_at = datetime.utcnow()
                await session.commit()
    except Exception as exc:  # noqa: BLE001 — bookkeeping only
        logger.warning("detail cache touch failed for %s: %s", code, exc)


def _log_identity_drift(code: str, prev_raw: str, new: MovieDetail) -> None:
    """Observability only. Heal rewrites of pre-genre-fix rows import
    today's JavBus studio/series identity over what the row held when
    the code was archived; path resolution reads presence so nothing
    misfolders yet, but drifting identity deserves a trace before any
    consumer trusts rewritten rows for foldering decisions."""
    try:
        prev = MovieDetail.model_validate_json(prev_raw)
    except Exception:  # noqa: BLE001 — legacy/corrupt rows carry no signal
        return
    for field in ("studio", "series"):
        old_ref = getattr(prev, field)
        new_ref = getattr(new, field)
        old_name = old_ref.name if old_ref else None
        new_name = new_ref.name if new_ref else None
        if old_name and new_name and old_name != new_name:
            logger.warning(
                "detail cache %s: %s identity drift %r -> %r",
                code,
                field,
                old_name,
                new_name,
            )


async def put(code: str, detail: MovieDetail) -> None:
    # Empty-title details are fetch misses; negative results live in the
    # scraper's in-memory _unresolved_cache, never in this table.
    if not settings.javbus_persist_cache_enabled or not detail.title:
        return
    try:
        async with SessionLocal() as session:
            prev = await session.get(MovieDetailCache, code)
            if prev is not None:
                _log_identity_drift(code, prev.detail, detail)
            await session.merge(
                MovieDetailCache(
                    code=code,
                    detail=detail.model_dump_json(),
                    release_date=detail.release_date or "",
                    fetched_at=datetime.utcnow(),
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — cache failure must not break scraping
        logger.warning("detail cache write failed for %s: %s", code, exc)
