"""Background backfill for the 女優 browse page.

Two gentle phases per cycle, both sequential with a per-item sleep so
they never starve interactive JavBus queries (the shared RateLimiter
throttles on top):

1. **Details** — downloaded codes (presence index) that have no
   ``movie_detail_cache`` row yet get a ``fetch_detail_resolved`` (its
   write-through persists the row; the actress list lives inside).
   Empty-title codes never persist, so a per-boot ``_attempted`` set
   stops them from being re-fetched every cycle — retry next boot is
   acceptable.
2. **Avatars** — aggregated actresses with a JavBus star id but no
   ``actress_avatar`` row (or an empty-avatar row older than the retry
   window) get a ``fetch_star_profile``; empty avatars are stored as a
   negative marker.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import ActressAvatar, MovieDetailCache
from ..scrapers import javbus as scraper
from . import actress_index, studio_index
from .pikpak_presence import presence_index

logger = logging.getLogger(__name__)

# Empty-avatar rows older than this get one more try.
_AVATAR_RETRY = timedelta(days=30)


class BackfillState:
    def __init__(self) -> None:
        self.enabled: bool = settings.actress_backfill_enabled
        self.pending: int = 0          # downloaded codes still missing detail rows
        self.done_total: int = 0       # successful detail fetches this boot
        self.failed_total: int = 0
        self.avatar_pending: int = 0
        self.avatar_done: int = 0
        self.last_run_at: datetime | None = None
        self.last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "pending": self.pending,
            "done_total": self.done_total,
            "failed_total": self.failed_total,
            "avatar_pending": self.avatar_pending,
            "avatar_done": self.avatar_done,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
        }


state = BackfillState()

# Codes fetched this boot whose detail came back empty (never persisted).
_attempted: set[str] = set()


async def _pick_missing_codes(limit: int) -> list[str]:
    downloaded = presence_index.peek()
    if downloaded is None:
        downloaded = await presence_index.get()
    async with SessionLocal() as session:
        cached = {
            row[0]
            for row in (await session.execute(select(MovieDetailCache.code))).all()
        }
    missing = sorted(downloaded - cached)
    state.pending = len(missing)
    picked = [c for c in missing if c not in _attempted][:limit]
    if len(picked) < limit:
        # Fill remaining slots with parse-stale rows (genres=[] — written
        # before the genre-parse fix). fetch_detail treats such a cache
        # hit as stale and refetches, healing the row; this drains the
        # whole pre-fix cache at the loop's existing gentle pacing.
        from sqlalchemy import func

        async with SessionLocal() as session:
            stale_rows = await session.execute(
                select(MovieDetailCache.code)
                .where(
                    func.json_array_length(
                        MovieDetailCache.detail, "$.genres"
                    )
                    == 0
                )
                .order_by(MovieDetailCache.fetched_at.asc())
                .limit(limit * 3)
            )
        seen = set(picked)
        for (c,) in stale_rows.all():
            if len(picked) >= limit:
                break
            if c not in seen and c not in _attempted:
                picked.append(c)
                seen.add(c)
    return picked


# Consecutive-failure circuit breaker: a JavBus outage or block makes
# every fetch in the batch fail the same way; grinding through the rest
# only burns request budget against a host that is already refusing us.
_BACKFILL_BREAKER_THRESHOLD = 5


async def _backfill_details() -> int:
    codes = await _pick_missing_codes(max(0, settings.actress_backfill_batch_limit))
    fetched = 0
    consecutive_failures = 0
    for pos, code in enumerate(codes, start=1):
        try:
            detail = await scraper.fetch_detail_resolved(code)
            _attempted.add(code)
            if detail.title:
                state.done_total += 1
                fetched += 1
                consecutive_failures = 0
            else:
                state.failed_total += 1
                consecutive_failures += 1
        except Exception as exc:  # noqa: BLE001 — one bad code must not stop the cycle
            _attempted.add(code)
            state.failed_total += 1
            consecutive_failures += 1
            logger.debug("detail backfill %s failed: %s", code, exc)
        if consecutive_failures >= _BACKFILL_BREAKER_THRESHOLD:
            logger.warning(
                "detail backfill: %d consecutive failures, aborting cycle "
                "(%d/%d codes processed)",
                consecutive_failures,
                pos,
                len(codes),
            )
            break
        await asyncio.sleep(settings.actress_backfill_spacing_seconds)
    return fetched


async def _pick_missing_avatars(limit: int) -> list[tuple[str, str]]:
    """(star_id, name) pairs needing a profile fetch."""
    agg = await actress_index.get()
    wanted = {e.id: e.name for e in agg.actresses.values() if e.id}
    if not wanted:
        state.avatar_pending = 0
        return []
    cutoff = datetime.utcnow() - _AVATAR_RETRY
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ActressAvatar.id, ActressAvatar.avatar, ActressAvatar.fetched_at)
            )
        ).all()
    have = {
        aid
        for aid, avatar, fetched_at in rows
        if avatar or (fetched_at is not None and fetched_at > cutoff)
    }
    missing = sorted(aid for aid in wanted if aid not in have)
    state.avatar_pending = len(missing)
    return [(aid, wanted[aid]) for aid in missing[:limit]]


async def _backfill_avatars() -> int:
    pairs = await _pick_missing_avatars(max(0, settings.actress_avatar_batch_limit))
    fetched = 0
    for star_id, name in pairs:
        avatar = ""
        try:
            profile = await scraper.fetch_star_profile(star_id)
            avatar = (profile.avatar or "") if profile else ""
        except Exception as exc:  # noqa: BLE001 — store the negative marker anyway
            logger.debug("avatar backfill %s failed: %s", star_id, exc)
        try:
            async with SessionLocal() as session:
                await session.merge(
                    ActressAvatar(
                        id=star_id,
                        name=name,
                        avatar=avatar,
                        fetched_at=datetime.utcnow(),
                    )
                )
                await session.commit()
            if avatar:
                state.avatar_done += 1
                fetched += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("avatar backfill store %s failed: %s", star_id, exc)
        await asyncio.sleep(settings.actress_backfill_spacing_seconds)
    return fetched


async def _run_cycle() -> None:
    new_details = await _backfill_details()
    if new_details:
        actress_index.invalidate()
        studio_index.invalidate()
    new_avatars = await _backfill_avatars()
    if new_avatars:
        actress_index.invalidate()
    state.last_run_at = datetime.utcnow()


async def run_loop() -> None:
    logger.info("actress detail backfill loop started")
    while True:
        if state.enabled:
            try:
                await _run_cycle()
                state.last_error = ""
            except Exception as exc:  # noqa: BLE001 — presence/JavBus down: skip this cycle
                state.last_error = str(exc)
                logger.warning("detail backfill cycle failed: %s", exc)
        await asyncio.sleep(max(30, settings.actress_backfill_interval_seconds))
