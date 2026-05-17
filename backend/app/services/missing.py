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
    ExtraCode,
    MissingCodesResult,
    MissingSummary,
    MissingSummaryItem,
    MovieListItem,
)
from ..scrapers import javbus as scraper
from .jav_code import KIND_LABELS_CH, normalize_code, safe_folder_name
from .pikpak_presence import presence_index


def _expected_roots(kind: str, slug: str, name: str) -> list[str]:
    """All folder paths the archiver might have used for this listing.
    Returns the canonical Chinese-kind path first, then the legacy
    English-kind path so files archived before the rename still count
    as belonging here (and don't get flagged as "extras")."""
    root = settings.pikpak_download_folder or "AVBT"
    safe = safe_folder_name(name, fallback=safe_folder_name(slug, fallback=slug))
    out: list[str] = []
    ch = KIND_LABELS_CH.get(kind, "")
    if ch:
        out.append(f"{root}/{ch}/{safe}")
    out.append(f"{root}/{kind}/{safe}")
    return out


def _expected_root(kind: str, slug: str, name: str) -> str:
    """Canonical (Chinese-kind) folder path for UI display."""
    return _expected_roots(kind, slug, name)[0]


def _compute_extras(
    kind: str, slug: str, name: str, expected_codes: set[str]
) -> list[ExtraCode]:
    """Codes physically under this listing's folder that are NOT in the
    JavBus catalog for it. ``expected_codes`` must already be normalised
    (same form the presence index uses)."""
    roots = _expected_roots(kind, slug, name)
    found = presence_index.codes_under(*roots)  # {code: [paths]}
    out = [
        ExtraCode(code=c, paths=paths)
        for c, paths in found.items()
        if c not in expected_codes
    ]
    out.sort(key=lambda e: e.code)
    return out


logger = logging.getLogger(__name__)


# (kind, slug, uncensored) → (built_at, list[MovieListItem], pages_scanned)
_listing_cache: dict[tuple[str, str, bool], tuple[datetime, list[MovieListItem], int]] = {}
_summary_lock = asyncio.Lock()

# Result caches for the aggregate views. The /tracked page hits
# missing_summary on every mount; with N=78 listings even a fully warm
# JavBus + presence cache still costs hundreds of ms (ownership map
# build + per-row extras scan). Cache the final result and only rebuild
# when an explicit invalidation fires (check / tracker tick / add /
# delete / archive / reorganize / presence refresh). Held forever
# until invalidated — the events above are reliable and the user has
# a "重算缺漏" button (refresh=true) as a manual override.
_summary_result: MissingSummary | None = None
_all_result: AggregatedMissing | None = None


def invalidate_result_caches() -> None:
    """Drop the cached missing_summary / missing_all aggregate results.
    Cheap — does not touch the JavBus listing cache or the PikPak
    presence index (call presence_index.invalidate() separately when
    PikPak state has changed)."""
    global _summary_result, _all_result
    _summary_result = None
    _all_result = None


async def _ownership_map(
    rows: list[TrackedListing],
) -> dict[str, tuple[str, str]]:
    """For dedup-aware display: walk every tracked listing in display
    order (the same ``(kind, id)`` alpha order used by missing_summary
    / missing_all) and claim each code for the FIRST listing it appears
    in. Returns ``{code: (kind, id)}``.

    The "first seen" rule means that, given the same set of tracked
    listings, every code has exactly one owner — so summing the per-
    listing deduped missing counts equals the total unique missing
    count. Listings later in alpha order may end up displaying fewer
    codes than their raw catalog when those codes are claimed earlier.

    Reuses ``fetch_all_listing_codes``'s 1h cache, so this is cheap on
    a warm cache (just hash-lookups + a single set traversal)."""
    owner: dict[str, tuple[str, str]] = {}
    for row in rows:
        try:
            items, _pages = await fetch_all_listing_codes(
                row.kind, row.id, uncensored=bool(row.uncensored)
            )
        except Exception:  # noqa: BLE001
            continue
        key = (row.kind, row.id)
        for it in items:
            if it.code and it.code not in owner:
                owner[it.code] = key
    return owner


def _owned_by(
    kind: str, slug: str, owner: dict[str, tuple[str, str]] | None
) -> set[str]:
    """Subset of ``owner.keys()`` that belongs to this listing."""
    if owner is None:
        return set()
    return {c for c, k in owner.items() if k == (kind, slug)}


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
) -> tuple[list[str], list[MovieListItem], set[str]]:
    """Returns (present_codes, missing_items, normalised_expected_set).
    The normalised set is the union of every item's canonical code —
    callers reuse it to compute "extras" without re-walking ``items``."""
    present_codes: list[str] = []
    missing: list[MovieListItem] = []
    expected: set[str] = set()
    for it in items:
        c = normalize_code(it.code) or it.code
        expected.add(c)
        if c in present:
            present_codes.append(it.code)
        else:
            missing.append(it)
    return present_codes, missing, expected


async def missing_for_listing(
    kind: str,
    slug: str,
    *,
    uncensored: bool = False,
    refresh: bool = False,
    dedup: bool = False,
) -> MissingCodesResult:
    items, pages = await fetch_all_listing_codes(
        kind, slug, uncensored=uncensored, refresh=refresh
    )
    presence = await presence_index.get(force=refresh)
    present_codes, missing, expected = _split_present_missing(items, presence)

    # Pull display name from DB if available.
    name = ""
    async with SessionLocal() as session:
        row = await session.get(TrackedListing, (kind, slug))
        if row:
            name = row.name or ""

    # Display-side dedup: when the same code is missing from multiple
    # tracked listings (e.g. ABC-001 features star A and is also a
    # series-X entry), show it only under the listing that the global
    # ownership map claimed it for. Disabled by default so callers like
    # the tracker auto-send still see every code this listing claims
    # (the download queue handles the cross-listing dedup downstream).
    if dedup and missing:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(TrackedListing).order_by(
                        TrackedListing.kind, TrackedListing.id
                    )
                )
            ).scalars().all()
        owner = await _ownership_map(rows)
        owned = _owned_by(kind, slug, owner)
        missing = [m for m in missing if m.code in owned]

    # Only flag extras when we actually got a JavBus catalog to compare
    # against. If the listing fetch returned nothing (network, geo-block,
    # invalid slug…) every file in the folder would otherwise look like
    # an extra.
    extras = (
        _compute_extras(kind, slug, name, expected)
        if items
        else []
    )

    return MissingCodesResult(
        kind=kind,
        id=slug,
        name=name,
        total=len(items),
        present_codes=present_codes,
        missing=missing,
        extras=extras,
        pages_scanned=pages,
        expected_root=_expected_root(kind, slug, name),
        built_at=datetime.utcnow(),
    )


async def _summary_item(
    row: TrackedListing,
    presence: set[str],
    owned: set[str] | None = None,
) -> MissingSummaryItem:
    """``owned`` (when not None) restricts missing_count to codes this
    listing owns under the global first-seen ownership rule. Missing
    counts then sum to the deduped total — same numbers the /missing
    page shows after dedup."""
    expected_root = _expected_root(row.kind, row.id, row.name or "")
    try:
        items, pages = await fetch_all_listing_codes(
            row.kind, row.id, uncensored=bool(row.uncensored)
        )
    except Exception as exc:  # noqa: BLE001
        return MissingSummaryItem(
            kind=row.kind, id=row.id, name=row.name or "",
            total=0, missing_count=0, extras_count=0, pages_scanned=0,
            expected_root=expected_root, error=str(exc),
        )
    _, missing, expected = _split_present_missing(items, presence)
    if owned is not None:
        missing = [m for m in missing if m.code in owned]
    # See note in missing_for_listing: skip extras when we got no
    # listing data, otherwise every file in the folder appears extra.
    extras = (
        _compute_extras(row.kind, row.id, row.name or "", expected)
        if items
        else []
    )
    return MissingSummaryItem(
        kind=row.kind,
        id=row.id,
        name=row.name or row.id,
        total=len(items),
        missing_count=len(missing),
        extras_count=len(extras),
        pages_scanned=pages,
        expected_root=expected_root,
    )


async def missing_summary(*, refresh: bool = False) -> MissingSummary:
    """Aggregate missing-counts for every TrackedListing row.

    Deduped: codes shared by multiple listings are counted under the
    first listing that claims them (alpha kind+id order), matching how
    the /missing page displays them.

    The result is memoised; subsequent calls return the cached
    MissingSummary until ``invalidate_result_caches()`` is called or
    ``refresh=True`` is passed. Single in-flight via _summary_lock so a
    concurrent page load doesn't spawn 50 JavBus crawls twice.
    """
    global _summary_result
    if not refresh and _summary_result is not None:
        return _summary_result
    async with _summary_lock:
        # Re-check inside the lock: another caller may have just rebuilt
        # while we were waiting.
        if not refresh and _summary_result is not None:
            return _summary_result
        presence = await presence_index.get(force=refresh)
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(TrackedListing).order_by(TrackedListing.kind, TrackedListing.id)
                )
            ).scalars().all()

        owner = await _ownership_map(rows)
        # Bucket the ownership map once so per-row lookups are O(1).
        owned_by_row: dict[tuple[str, str], set[str]] = {}
        for code, key in owner.items():
            owned_by_row.setdefault(key, set()).add(code)

        items: list[MissingSummaryItem] = []
        for row in rows:
            items.append(
                await _summary_item(
                    row,
                    presence,
                    owned=owned_by_row.get((row.kind, row.id), set()),
                )
            )

        result = MissingSummary(
            built_at=datetime.utcnow(),
            presence_built_at=presence_index._built_at,  # type: ignore[attr-defined]
            items=items,
        )
        _summary_result = result
        return result


async def missing_all(*, refresh: bool = False) -> AggregatedMissing:
    """Like missing_summary but returns the full MovieListItem list for
    each tracked listing (not just counts). Powers the /missing page.

    Deduped: a movie missing from multiple tracked listings appears only
    under the first listing (alpha kind+id order) that claims it. The
    later listings simply don't include it in their card grid.

    Cached via the same invalidation events as missing_summary."""
    global _all_result
    if not refresh and _all_result is not None:
        return _all_result
    async with _summary_lock:
        if not refresh and _all_result is not None:
            return _all_result
        presence = await presence_index.get(force=refresh)
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(TrackedListing).order_by(TrackedListing.kind, TrackedListing.id)
                )
            ).scalars().all()

        owner = await _ownership_map(rows)
        owned_by_row: dict[tuple[str, str], set[str]] = {}
        for code, key in owner.items():
            owned_by_row.setdefault(key, set()).add(code)

        items: list[AggregatedMissingItem] = []
        for row in rows:
            try:
                listing, _pages = await fetch_all_listing_codes(
                    row.kind, row.id, uncensored=bool(row.uncensored)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("missing_all failed for %s/%s: %s", row.kind, row.id, exc)
                continue
            _, missing, _expected = _split_present_missing(listing, presence)
            owned = owned_by_row.get((row.kind, row.id), set())
            missing = [m for m in missing if m.code in owned]
            if missing:
                items.append(
                    AggregatedMissingItem(
                        kind=row.kind,
                        id=row.id,
                        name=row.name or row.id,
                        missing=missing,
                    )
                )

        result = AggregatedMissing(
            built_at=datetime.utcnow(),
            presence_built_at=presence_index._built_at,  # type: ignore[attr-defined]
            items=items,
        )
        _all_result = result
        return result
