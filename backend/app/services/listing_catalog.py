"""Persistent JavBus listing-catalog cache backed by ``listing_catalog``.

missing.py's in-memory ``_listing_cache`` stays as the L1 parse cache;
this layer survives restarts so the tracked page never has to re-crawl
every listing's catalog just because the process bounced or an hourly
tick dropped the aggregate caches. Both operations swallow their own
errors — a DB hiccup must never break a missing computation, it just
degrades to a walk.
"""

import logging
from datetime import datetime

from pydantic import TypeAdapter

from ..database import SessionLocal
from ..models import ListingCatalog
from ..schemas import MovieListItem

logger = logging.getLogger(__name__)

_ITEMS = TypeAdapter(list[MovieListItem])


def _filter_key(with_magnets_only: bool) -> str:
    return "mag" if with_magnets_only else "all"


async def get(
    kind: str,
    slug: str,
    *,
    uncensored: bool,
    with_magnets_only: bool = True,
) -> tuple[list[MovieListItem], int, datetime] | None:
    """(items, pages_scanned, fetched_at), or None on any kind of miss:
    no row, uncensored flag mismatch (the row predates a flag flip and
    describes a different catalog), or unparseable JSON."""
    try:
        async with SessionLocal() as session:
            row = await session.get(
                ListingCatalog, (kind, slug, _filter_key(with_magnets_only))
            )
        if row is None or row.fetched_at is None:
            return None
        if bool(row.uncensored) != bool(uncensored):
            return None
        items = _ITEMS.validate_json(row.items or "[]")
        return items, int(row.pages_scanned or 0), row.fetched_at
    except Exception as exc:  # noqa: BLE001 — cache failure = cache miss
        logger.warning("listing catalog read failed for %s/%s: %s", kind, slug, exc)
        return None


async def put(
    kind: str,
    slug: str,
    *,
    uncensored: bool,
    with_magnets_only: bool,
    items: list[MovieListItem],
    pages_scanned: int,
) -> None:
    try:
        payload = _ITEMS.dump_json(items).decode()
        async with SessionLocal() as session:
            await session.merge(
                ListingCatalog(
                    kind=kind,
                    id=slug,
                    filter=_filter_key(with_magnets_only),
                    uncensored=bool(uncensored),
                    items=payload,
                    pages_scanned=int(pages_scanned),
                    fetched_at=datetime.utcnow(),
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        logger.warning("listing catalog write failed for %s/%s: %s", kind, slug, exc)


async def delete_listing(kind: str, slug: str) -> None:
    """Drop both filter rows — used on untrack and on uncensored flips."""
    try:
        async with SessionLocal() as session:
            for f in ("mag", "all"):
                row = await session.get(ListingCatalog, (kind, slug, f))
                if row is not None:
                    await session.delete(row)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("listing catalog delete failed for %s/%s: %s", kind, slug, exc)


async def fetched_at(kind: str, slug: str) -> datetime | None:
    """fetched_at of the mag row, one cheap SELECT — the per-listing
    /missing-codes response reports it as catalog_fetched_at."""
    try:
        async with SessionLocal() as session:
            row = await session.get(ListingCatalog, (kind, slug, "mag"))
        return row.fetched_at if row is not None else None
    except Exception:  # noqa: BLE001
        return None


async def fetched_map() -> dict[tuple[str, str], datetime]:
    """{(kind, id): fetched_at} of every mag row in one SELECT — the
    summary rebuild stamps 78 items without 78 point queries."""
    from sqlalchemy import select

    try:
        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(
                        ListingCatalog.kind,
                        ListingCatalog.id,
                        ListingCatalog.fetched_at,
                    ).where(ListingCatalog.filter == "mag")
                )
            ).all()
        return {(k, i): ts for k, i, ts in rows if ts is not None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("listing catalog fetched_map failed: %s", exc)
        return {}
