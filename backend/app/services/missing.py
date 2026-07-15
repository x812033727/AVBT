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
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

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
    MovieDetail,
    MovieListItem,
)
from ..scrapers import javbus as scraper
from .jav_code import KIND_LABELS_CH, normalize_code, safe_folder_name
from .listing_walker import walk_listing
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


# Per-call ceiling on detail-page membership probes (see
# ``_owned_listing_members``). A listing's folder should only ever hold a
# handful of works the listing walk missed; this just stops a folder that
# accidentally collected many unrelated codes from issuing a detail fetch
# for each one.
_MEMBERSHIP_PROBE_CAP = 50


def _detail_belongs_to(detail: MovieDetail, kind: str, slug: str) -> bool:
    """Whether a work's own detail page places it in ``kind``/``slug``.

    JavBus's listing index occasionally omits works that still carry the
    tag on their detail page (e.g. DAM-043 keeps series 回胴録 / 11pb but
    is absent from /series/11pb even with existmag=all). The detail page
    is then the only authoritative source of membership."""
    if kind == "series":
        return bool(detail.series and detail.series.id == slug)
    if kind == "studio":
        return bool(detail.studio and detail.studio.id == slug)
    if kind == "label":
        return bool(detail.label and detail.label.id == slug)
    if kind == "director":
        return bool(detail.director and detail.director.id == slug)
    if kind == "star":
        return any(a.id == slug for a in detail.actresses)
    if kind == "genre":
        return any(g.id == slug for g in detail.genres)
    return False


def _detail_to_list_item(detail: MovieDetail) -> MovieListItem:
    base = settings.javbus_base_url.rstrip("/")
    return MovieListItem(
        code=detail.code,
        title=detail.title,
        cover=detail.cover,
        detail_url=f"{base}/{detail.code}",
        date=detail.release_date,
    )


async def _owned_listing_members(
    kind: str,
    slug: str,
    name: str,
    base_items: list[MovieListItem],
    *,
    uncensored: bool,
    refresh: bool = False,
) -> list[MovieListItem]:
    """Owned works the magnet-only listing walk missed but that genuinely
    belong to this listing, so they can be folded into the catalog.

    A held work can be absent from the default (existmag=mag) walk for
    two reasons:

      1. its magnets aged off JavBus — it still shows in the existmag=all
         full catalog;
      2. JavBus dropped it from the listing index entirely, even though
         its own detail page keeps the tag (the 回胴録 / DAM-043 case).

    Only codes physically held under the listing's folder and absent from
    ``base_items`` are probed, so the work is bounded by what the user
    owns. Case 1 is confirmed cheaply against the full catalog (one cached
    walk); the remainder are confirmed against their detail page, which is
    the only source for case 2. Returned items are appended to the catalog
    so they count toward the total / present set instead of being flagged
    多餘.

    Skipped entirely (no JavBus round-trip) when the folder holds nothing
    beyond the listed works — the common case."""
    roots = _expected_roots(kind, slug, name)
    found = presence_index.codes_under(*roots)
    base = {normalize_code(it.code) or it.code for it in base_items}
    candidates = [c for c in found if c not in base]
    if not candidates:
        return []

    members: list[MovieListItem] = []
    claimed: set[str] = set()

    # Pass 1: the existmag=all full catalog explains held works whose only
    # problem is an aged-off magnet — one cached walk covers all of them.
    try:
        full_items, _ = await fetch_all_listing_codes(
            kind, slug, uncensored=uncensored, refresh=refresh,
            with_magnets_only=False,
        )
    except Exception as exc:  # noqa: BLE001
        # Network / geo-block: fall back to the detail probe rather than
        # hard-failing the whole missing computation.
        logger.warning(
            "full-catalog walk failed for %s/%s: %s — detail probe only",
            kind, slug, exc,
        )
        full_items = []
    full_by_code: dict[str, MovieListItem] = {}
    for it in full_items:
        full_by_code.setdefault(normalize_code(it.code) or it.code, it)
    for c in candidates:
        hit = full_by_code.get(c)
        if hit is not None:
            members.append(hit)
            claimed.add(c)

    # Pass 2: detail-page probe for the rest. JavBus dropped these from
    # the listing, so only each work's own page can confirm membership.
    rest = [c for c in candidates if c not in claimed]
    if len(rest) > _MEMBERSHIP_PROBE_CAP:
        logger.warning(
            "%s/%s: %d held codes missing from the listing exceeds probe "
            "cap %d — verifying first %d",
            kind, slug, len(rest), _MEMBERSHIP_PROBE_CAP, _MEMBERSHIP_PROBE_CAP,
        )
        rest = rest[:_MEMBERSHIP_PROBE_CAP]
    for c in rest:
        try:
            detail = await scraper.fetch_detail_resolved(c, refresh=refresh)
        except Exception:  # noqa: BLE001
            continue
        if detail and detail.title and _detail_belongs_to(detail, kind, slug):
            members.append(_detail_to_list_item(detail))

    return members


logger = logging.getLogger(__name__)


# (kind, slug, uncensored, with_magnets_only) → (built_at, list[MovieListItem], pages_scanned).
# The ``with_magnets_only`` axis distinguishes the magnet-filtered walk
# (used for missing detection — only codes with downloadable magnets
# count) from the full-catalog walk (used for extras — codes whose
# magnets have aged off JavBus still belong in the listing).
_listing_cache: dict[
    tuple[str, str, bool, bool], tuple[datetime, list[MovieListItem], int]
] = {}
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


def invalidate_all_caches(*, presence: bool = False) -> None:
    """One-stop invalidation for callers that touched both PikPak state
    and the aggregate views. Always drops the missing aggregate caches;
    pass ``presence=True`` when the PikPak file set has materially
    changed (e.g. archiver moved files, sweep migrated orphans)."""
    invalidate_result_caches()
    if presence:
        presence_index.invalidate()


async def invalidate_all_caches_async(*, presence: bool = False) -> None:
    """Lock-coordinated variant used by background tasks. Takes the
    summary lock so an in-flight ``missing_summary`` rebuild cannot see
    half-invalidated state — it either reads the pre-invalidation cache
    or rebuilds against the post-invalidation snapshot."""
    async with _summary_lock:
        invalidate_result_caches()
        if presence:
            presence_index.invalidate()


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
    with_magnets_only: bool = True,
) -> tuple[list[MovieListItem], int]:
    """Walk JavBus pages until ``has_next == False`` (or hit the cap).

    Returns (items, pages_scanned). De-duplicates by code so the same
    work appearing on two pages (rare but possible at page boundaries)
    only counts once.

    ``with_magnets_only`` mirrors ``scraper.fetch_listing``: default
    ``True`` keeps the historical magnet-filtered view; pass ``False``
    for the full catalog walk used by extras detection.
    """
    key = (kind, slug, uncensored, with_magnets_only)
    if not refresh:
        cached = _listing_cache.get(key)
        if cached and _cache_fresh(cached[0]):
            return list(cached[1]), cached[2]

    cap = max_pages or max(1, settings.missing_max_pages)
    items, pages = await walk_listing(
        kind, slug, uncensored=uncensored, max_pages=cap,
        with_magnets_only=with_magnets_only,
    )

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
    # ``refresh`` is about the JavBus catalog, not the drive: the index is
    # persisted and each code is refreshed as the pipeline lands it (#163),
    # so forcing a walk here bought ~2.5min of PikPak calls per call for
    # data that is already current. Full walks stay on the explicit
    # /presence/refresh path.
    presence = await presence_index.get()

    # Pull display name from DB if available — needed to resolve the
    # listing's archive folder before reconciling held works.
    name = ""
    async with SessionLocal() as session:
        row = await session.get(TrackedListing, (kind, slug))
        if row:
            name = row.name or ""

    # Fold in owned works the magnet-only walk missed but that belong here
    # (aged-off magnets / listing-index gaps). They then count toward the
    # total and present set instead of looking like 多餘.
    if items:
        items = items + await _owned_listing_members(
            kind, slug, name, items, uncensored=uncensored, refresh=refresh
        )

    present_codes, missing, expected = _split_present_missing(items, presence)

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
    # an extra. ``expected`` already includes the reconciled held works
    # (see ``_owned_listing_members``) so they aren't false-flagged.
    if items:
        extras = _compute_extras(kind, slug, name, expected)
    else:
        extras = []

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


async def _parallel_map(items, fn, limit: int) -> list:
    """Order-preserving bounded-concurrency ``await fn(item)`` over
    ``items``. Rebuild paths iterate 78+ tracked listings whose work is
    IO-wait dominated (JavBus walk, presence lookups) — running them
    strictly serially made a cold rebuild take minutes."""
    sem = asyncio.Semaphore(max(1, limit))

    async def _run(item):
        async with sem:
            return await fn(item)

    return list(await asyncio.gather(*(_run(i) for i in items)))


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
    # Fold in owned works the magnet-only walk missed but that belong here
    # (see _owned_listing_members) so the total and extras match the
    # per-listing /missing-codes view.
    if items:
        items = items + await _owned_listing_members(
            row.kind, row.id, row.name or "", items,
            uncensored=bool(row.uncensored),
        )
    _, missing, expected = _split_present_missing(items, presence)
    if owned is not None:
        missing = [m for m in missing if m.code in owned]
    # See note in missing_for_listing: skip extras when we got no
    # listing data, otherwise every file in the folder appears extra.
    if items:
        extras = _compute_extras(row.kind, row.id, row.name or "", expected)
    else:
        extras = []
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
        result = await _missing_summary_locked(refresh=refresh)
        _summary_result = result
        return result


async def _missing_summary_locked(*, refresh: bool) -> MissingSummary:
    """Core rebuild logic, called with ``_summary_lock`` held. Shared by
    ``missing_summary`` and ``missing_summary_stream`` so both write
    identical results into ``_summary_result``."""
    # Same split as missing_for_listing: ``refresh`` re-fetches listings,
    # it does not re-walk the drive.
    presence = await presence_index.get()
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

    items: list[MissingSummaryItem] = await _parallel_map(
        rows,
        lambda row: _summary_item(
            row, presence, owned=owned_by_row.get((row.kind, row.id), set())
        ),
        settings.missing_rebuild_concurrency,
    )

    return MissingSummary(
        built_at=datetime.utcnow(),
        presence_built_at=presence_index._built_at,  # type: ignore[attr-defined]
        items=items,
    )


async def missing_summary_stream(
    *, refresh: bool = True
) -> AsyncIterator[dict]:
    """Streaming variant of ``missing_summary`` for the "重算缺漏" button.

    Yields:
      ``start``    { total }
      ``progress`` { current, total, kind, id, name, missing_count,
                     pages_scanned, error }   — one per listing
      ``done``     { result: MissingSummary.model_dump() }
      ``error``    { message }                — fatal pre-flight failure

    Writes the final result into ``_summary_result`` so any concurrent
    non-streaming ``missing_summary`` caller gets the freshly-built cache."""
    global _summary_result
    async with _summary_lock:
        # Same pre-flight as the non-streaming path. Errors during
        # the presence read / DB read are fatal — surface as a single
        # ``error`` event then bail. ``refresh`` re-fetches listings; it
        # does not re-walk the drive (#163/#169).
        try:
            presence = await presence_index.get()
            async with SessionLocal() as session:
                rows = (
                    await session.execute(
                        select(TrackedListing).order_by(
                            TrackedListing.kind, TrackedListing.id
                        )
                    )
                ).scalars().all()
            owner = await _ownership_map(rows)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": str(exc)}
            return

        owned_by_row: dict[tuple[str, str], set[str]] = {}
        for code, key in owner.items():
            owned_by_row.setdefault(key, set()).add(code)

        yield {"type": "start", "total": len(rows)}

        # Bounded-parallel, progress streamed as listings complete (so
        # events arrive out of row order); the final result is
        # reassembled in row order below.
        sem = asyncio.Semaphore(max(1, settings.missing_rebuild_concurrency))

        async def _one(idx: int, row: TrackedListing):
            async with sem:
                item = await _summary_item(
                    row,
                    presence,
                    owned=owned_by_row.get((row.kind, row.id), set()),
                )
            return idx, row, item

        tasks = [
            asyncio.create_task(_one(i, row)) for i, row in enumerate(rows)
        ]
        slots: list[MissingSummaryItem | None] = [None] * len(rows)
        try:
            done = 0
            for fut in asyncio.as_completed(tasks):
                idx, row, item = await fut
                slots[idx] = item
                done += 1
                yield {
                    "type": "progress",
                    "current": done,
                    "total": len(rows),
                    "kind": row.kind,
                    "id": row.id,
                    "name": row.name or "",
                    "missing_count": item.missing_count,
                    "pages_scanned": item.pages_scanned,
                    "error": item.error or "",
                }
        finally:
            # Client disconnect closes the generator mid-stream — don't
            # leave orphaned listing walks running.
            for t in tasks:
                t.cancel()
        items: list[MissingSummaryItem] = [i for i in slots if i is not None]

        result = MissingSummary(
            built_at=datetime.utcnow(),
            presence_built_at=presence_index._built_at,  # type: ignore[attr-defined]
            items=items,
        )
        _summary_result = result

    yield {"type": "done", "result": result.model_dump(mode="json")}


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
        # Listing refresh, not a drive walk — see missing_for_listing.
        presence = await presence_index.get()
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

        async def _one_row(row: TrackedListing) -> AggregatedMissingItem | None:
            try:
                listing, _pages = await fetch_all_listing_codes(
                    row.kind, row.id, uncensored=bool(row.uncensored)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("missing_all failed for %s/%s: %s", row.kind, row.id, exc)
                return None
            _, missing, _expected = _split_present_missing(listing, presence)
            owned = owned_by_row.get((row.kind, row.id), set())
            missing = [m for m in missing if m.code in owned]
            if not missing:
                return None
            return AggregatedMissingItem(
                kind=row.kind,
                id=row.id,
                name=row.name or row.id,
                missing=missing,
            )

        items: list[AggregatedMissingItem] = [
            item
            for item in await _parallel_map(
                rows, _one_row, settings.missing_rebuild_concurrency
            )
            if item is not None
        ]

        result = AggregatedMissing(
            built_at=datetime.utcnow(),
            presence_built_at=presence_index._built_at,  # type: ignore[attr-defined]
            items=items,
        )
        _all_result = result
        return result
