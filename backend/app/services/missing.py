"""Compute "what's missing in PikPak" for a tracked listing.

A listing on JavBus (e.g. all works in series MIDV) gives us the full
expected catalog. The PikPak presence index gives us which codes are
already on disk. Missing = catalog − presence.

Listings change slowly, so JavBus pagination results are cached for an
hour (per slug/uncensored) to keep ``missing_summary`` cheap.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import TrackedListing
from ..schemas import (
    AggregatedMissing,
    AggregatedMissingItem,
    MissingCodesResult,
    MissingSummary,
    MissingSummaryItem,
    MovieListItem,
)
from ..scrapers import javbus as scraper
from .jav_code import normalize_code, safe_folder_name
from .pikpak_presence import presence_index


def _expected_root(kind: str, slug: str, name: str) -> str:
    """Mirror the archiver: ``<download_folder>/<kind>/<safe_name>``.
    Uses the display name when present, falling back to the slug so the
    user always sees a path (not an empty segment)."""
    root = settings.pikpak_download_folder or "AVBT"
    safe = safe_folder_name(name, fallback=safe_folder_name(slug, fallback=slug))
    return f"{root}/{kind}/{safe}"


logger = logging.getLogger(__name__)


# (kind, slug, uncensored) → (built_at, list[MovieListItem], pages_scanned)
_listing_cache: dict[tuple[str, str, bool], tuple[datetime, list[MovieListItem], int]] = {}
_summary_lock = asyncio.Lock()


def _cache_fresh(built_at: datetime) -> bool:
    ttl = max(60, settings.missing_listing_cache_seconds)
    return datetime.utcnow() - built_at < timedelta(seconds=ttl)


async def fetch_all_listing_codes(
    kind: str,
    slug: str,
    *,
    uncensored: bool,
    refresh: bool = False,
    max_pages: int | None = None,
) -> tuple[list[MovieListItem], int]:
    """Walk JavBus pages until ``has_next == False`` (or hit the cap).

    Returns (items, pages_scanned). De-duplicates by code so the same
    work appearing on two pages (rare but possible at page boundaries)
    only counts once.
    """
    key = (kind, slug, uncensored)
    if not refresh:
        cached = _listing_cache.get(key)
        if cached and _cache_fresh(cached[0]):
            return list(cached[1]), cached[2]

    cap = max_pages or max(1, settings.missing_max_pages)
    items: list[MovieListItem] = []
    seen: set[str] = set()
    pages = 0
    page = 1
    while page <= cap:
        try:
            res = await scraper.fetch_listing(
                kind, slug, page=page, uncensored=uncensored
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fetch_listing(%s/%s p=%d) failed: %s", kind, slug, page, exc
            )
            break
        pages += 1
        if not res.items:
            break
        for it in res.items:
            if it.code and it.code not in seen:
                seen.add(it.code)
                items.append(it)
        if not res.has_next:
            break
        page += 1

    _listing_cache[key] = (datetime.utcnow(), list(items), pages)
    return items, pages


def _split_present_missing(
    items: list[MovieListItem], present: set[str]
) -> tuple[list[str], list[MovieListItem]]:
    present_codes: list[str] = []
    missing: list[MovieListItem] = []
    for it in items:
        c = normalize_code(it.code) or it.code
        if c in present:
            present_codes.append(it.code)
        else:
            missing.append(it)
    return present_codes, missing


async def missing_for_listing(
    kind: str,
    slug: str,
    *,
    uncensored: bool = False,
    refresh: bool = False,
) -> MissingCodesResult:
    items, pages = await fetch_all_listing_codes(
        kind, slug, uncensored=uncensored, refresh=refresh
    )
    presence = await presence_index.get(force=refresh)
    present_codes, missing = _split_present_missing(items, presence)

    # Pull display name from DB if available.
    name = ""
    async with SessionLocal() as session:
        row = await session.get(TrackedListing, (kind, slug))
        if row:
            name = row.name or ""

    return MissingCodesResult(
        kind=kind,
        id=slug,
        name=name,
        total=len(items),
        present_codes=present_codes,
        missing=missing,
        pages_scanned=pages,
        expected_root=_expected_root(kind, slug, name),
        built_at=datetime.utcnow(),
    )


async def _summary_item(
    row: TrackedListing, presence: set[str]
) -> MissingSummaryItem:
    expected_root = _expected_root(row.kind, row.id, row.name or "")
    try:
        items, pages = await fetch_all_listing_codes(
            row.kind, row.id, uncensored=bool(row.uncensored)
        )
    except Exception as exc:  # noqa: BLE001
        return MissingSummaryItem(
            kind=row.kind, id=row.id, name=row.name or "",
            total=0, missing_count=0, pages_scanned=0,
            expected_root=expected_root, error=str(exc),
        )
    _, missing = _split_present_missing(items, presence)
    return MissingSummaryItem(
        kind=row.kind,
        id=row.id,
        name=row.name or row.id,
        total=len(items),
        missing_count=len(missing),
        pages_scanned=pages,
        expected_root=expected_root,
    )


async def missing_summary(*, refresh: bool = False) -> MissingSummary:
    """Aggregate missing-counts for every TrackedListing row.

    Single in-flight via _summary_lock so a concurrent page load doesn't
    spawn 50 JavBus crawls twice. Listing results are themselves cached
    (1h) so subsequent calls are cheap.
    """
    async with _summary_lock:
        presence = await presence_index.get(force=refresh)
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(TrackedListing).order_by(TrackedListing.kind, TrackedListing.id)
                )
            ).scalars().all()

        # Sequential to avoid hammering JavBus. Each per-listing call is
        # cached, so a warm cache makes this loop O(N) hash-lookups.
        items: list[MissingSummaryItem] = []
        for row in rows:
            items.append(await _summary_item(row, presence))

        return MissingSummary(
            built_at=datetime.utcnow(),
            presence_built_at=presence_index._built_at,  # type: ignore[attr-defined]
            items=items,
        )


async def missing_all(*, refresh: bool = False) -> AggregatedMissing:
    """Like missing_summary but returns the full MovieListItem list for
    each tracked listing (not just counts). Powers the /missing page."""
    presence = await presence_index.get(force=refresh)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(TrackedListing).order_by(TrackedListing.kind, TrackedListing.id)
            )
        ).scalars().all()

    items: list[AggregatedMissingItem] = []
    for row in rows:
        try:
            listing, _pages = await fetch_all_listing_codes(
                row.kind, row.id, uncensored=bool(row.uncensored)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("missing_all failed for %s/%s: %s", row.kind, row.id, exc)
            continue
        _, missing = _split_present_missing(listing, presence)
        if missing:
            items.append(
                AggregatedMissingItem(
                    kind=row.kind,
                    id=row.id,
                    name=row.name or row.id,
                    missing=missing,
                )
            )

    return AggregatedMissing(
        built_at=datetime.utcnow(),
        presence_built_at=presence_index._built_at,  # type: ignore[attr-defined]
        items=items,
    )
