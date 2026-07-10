"""Parallel-batch listing walker.

Shared helper for the two paths that need to walk every page of a
JavBus listing: ``services/missing.fetch_all_listing_codes`` (missing
detection) and ``services/bulk._collect_codes`` (send-all flows). Both
used to walk pages strictly sequentially, which paired with the 1.2 s
HTTP throttle made a 50-page listing cost 60+ seconds. The walker
issues pages in concurrent batches (default 3) and stops as soon as
any page in the batch reports the end of the listing.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import settings
from ..schemas import MovieListItem
from ..scrapers import javbus as scraper

logger = logging.getLogger(__name__)


async def walk_listing(
    kind: str,
    slug: str,
    *,
    uncensored: bool,
    max_pages: int,
    batch_size: int | None = None,
    with_magnets_only: bool = True,
) -> tuple[list[MovieListItem], int]:
    """Walk JavBus pages until the end (``has_next=False``), the page
    cap (``max_pages``) or a fetch error. Returns ``(items, pages_scanned)``
    with codes de-duplicated.

    Pages within each batch are fetched concurrently; results are then
    processed in page order to preserve listing ordering. If any page
    in the batch errors out or returns no items, we stop after that
    page — the next tracker tick / refresh will retry the rest. This
    pessimistic stop matches the old sequential behaviour: never
    silently skip a page in the middle of the listing.

    Worst-case waste: at the very last batch we may have issued up to
    ``batch_size - 1`` extra requests beyond the real last page. For
    the default batch=3 that's ≤2 wasted requests per listing, well
    worth halving the elapsed time on longer listings.
    """
    cap = max(1, max_pages)
    batch = max(1, batch_size if batch_size is not None else settings.javbus_page_batch_size)

    items: list[MovieListItem] = []
    seen: set[str] = set()
    pages_scanned = 0
    next_page = 1
    stop = False

    while not stop and next_page <= cap:
        batch_pages = list(range(next_page, min(next_page + batch, cap + 1)))
        tasks = [
            scraper.fetch_listing(
                kind, slug, page=p, uncensored=uncensored,
                with_magnets_only=with_magnets_only,
            )
            for p in batch_pages
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for page, res in zip(batch_pages, results, strict=True):
            if isinstance(res, BaseException):
                # A page fetch failed (429 exhausted / 5xx / network). We
                # must NOT return the pages gathered so far as if they were
                # the full catalog: a truncated listing silently under-
                # counts (real works look 缺漏 / present files look 多餘),
                # and a page-1 failure would masquerade as an empty —
                # "complete, nothing missing" — listing. Propagate so the
                # caller marks this listing errored instead of trusting a
                # partial/empty result. Transient errors recover on the
                # next tracker tick / 重算缺漏.
                logger.warning(
                    "fetch_listing(%s/%s p=%d) failed: %s", kind, slug, page, res
                )
                raise res
            pages_scanned += 1
            if not res.items:
                stop = True
                break
            for it in res.items:
                if it.code and it.code not in seen:
                    seen.add(it.code)
                    items.append(it)
            if not res.has_next:
                stop = True
                break

        next_page += batch

    return items, pages_scanned
