"""Actress aggregation for the 女優 browse page.

Joins the two sources we already maintain: the PikPak presence index
(which codes are physically downloaded) and the persistent
``movie_detail_cache`` (whose detail JSON carries ``actresses``). Rows
are read directly, IGNORING the detail-cache TTL — a stale row's cast
list is still correct. The aggregation is a millisecond-scale scan of a
few thousand JSON rows, so it lives in memory with a short TTL and an
explicit ``invalidate()`` for the backfill worker to call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from ..database import SessionLocal
from ..models import ActressAvatar, MovieDetailCache
from ..schemas import MovieDetail, MovieListItem
from .pikpak_presence import presence_index

logger = logging.getLogger(__name__)

_TTL_SECONDS = 60.0


@dataclass
class ActressEntry:
    name: str
    id: str = ""
    avatar: str = ""
    sample_cover: str = ""
    works: list[MovieListItem] = field(default_factory=list)


@dataclass
class ActressAggregation:
    actresses: dict[str, ActressEntry] = field(default_factory=dict)
    downloaded_total: int = 0
    indexed_total: int = 0


_cache: ActressAggregation | None = None
_built_at = 0.0
_lock = asyncio.Lock()

# Light per-code projection of the fields ``_build`` actually needs out
# of a ``MovieDetail`` blob: (title, cover, release_date, [(name, id), ...]).
_Projection = tuple[str, str, str, list[tuple[str, str]]]

# Row-level parse cache, keyed by code, keyed off ``fetched_at`` so a
# rebuild (triggered at least every 300s by detail_backfill) only pays
# ``model_validate_json`` for rows whose detail actually changed instead
# of re-parsing every cached row's JSON on every rebuild. Survives across
# ``invalidate()`` on purpose — that only forces ``_build`` to run again,
# it doesn't mean every row changed.
_parsed: dict[str, tuple[datetime, _Projection]] = {}


def invalidate() -> None:
    global _built_at
    _built_at = 0.0


def _sort_works(works: list[MovieListItem]) -> None:
    # Newest first, undated last; stable code order within a date.
    works.sort(key=lambda w: w.code)
    works.sort(key=lambda w: w.date, reverse=True)


async def _build() -> ActressAggregation:
    downloaded = presence_index.peek()
    if downloaded is None:
        try:
            downloaded = await presence_index.get()
        except Exception as exc:  # noqa: BLE001 — presence down ≠ page down
            logger.warning("actress index: presence unavailable: %s", exc)
            return ActressAggregation()

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    MovieDetailCache.code,
                    MovieDetailCache.detail,
                    MovieDetailCache.fetched_at,
                )
            )
        ).all()
        avatar_rows = (
            await session.execute(
                select(ActressAvatar.id, ActressAvatar.avatar)
            )
        ).all()
    avatars = {aid: av for aid, av in avatar_rows if av}

    agg = ActressAggregation(downloaded_total=len(downloaded))
    table_codes: set[str] = set()
    for code, detail_json, fetched_at in rows:
        table_codes.add(code)
        if code not in downloaded:
            continue

        cached = _parsed.get(code)
        if cached is not None and cached[0] == fetched_at:
            projection = cached[1]
        else:
            try:
                detail = MovieDetail.model_validate_json(detail_json)
            except Exception:  # noqa: BLE001 — one corrupt row must not kill the page
                logger.warning("actress index: corrupt detail row for %s", code)
                _parsed.pop(code, None)
                continue
            refs = [
                ((ref.name or ""), (ref.id or "")) for ref in detail.actresses
            ]
            projection = (detail.title, detail.cover, detail.release_date or "", refs)
            _parsed[code] = (fetched_at, projection)

        agg.indexed_total += 1
        title, cover, date, refs = projection
        item = MovieListItem(
            code=code, title=title, cover=cover, detail_url="", date=date,
        )
        for name, star_id in refs:
            name = name.strip()
            if not name:
                continue
            entry = agg.actresses.get(name)
            if entry is None:
                entry = ActressEntry(name=name)
                agg.actresses[name] = entry
            if star_id and not entry.id:
                entry.id = star_id
            entry.works.append(item)

    # Drop rows that vanished from the table entirely (deleted / expired
    # cache row) so this dict doesn't grow unbounded across restarts-free
    # uptime.
    for stale in [c for c in _parsed if c not in table_codes]:
        del _parsed[stale]

    for entry in agg.actresses.values():
        _sort_works(entry.works)
        entry.avatar = avatars.get(entry.id, "") if entry.id else ""
        entry.sample_cover = next(
            (w.cover for w in entry.works if w.cover), ""
        )
    return agg


async def get(*, force: bool = False) -> ActressAggregation:
    global _cache, _built_at
    if not force and _cache is not None and time.monotonic() - _built_at < _TTL_SECONDS:
        return _cache
    async with _lock:
        if not force and _cache is not None and time.monotonic() - _built_at < _TTL_SECONDS:
            return _cache
        _cache = await _build()
        _built_at = time.monotonic()
        return _cache


async def works_for(name: str) -> ActressEntry | None:
    agg = await get()
    return agg.actresses.get((name or "").strip())
