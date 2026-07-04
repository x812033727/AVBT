"""Bulk-submit every movie of a JavBus listing to PikPak.

This module used to own its own per-stream worker pool. It now just
walks the JavBus listing pages to collect codes and pushes them into the
global ``download_queue``. Streaming callers (the ``send-all/stream``
NDJSON endpoints) ``asyncio.as_completed`` over the per-job futures so
the browser still sees per-code progress in real time.

Cancellation via the HTTP request's abort signal removes the
still-pending jobs from the queue (in-flight ones run to completion).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from ..schemas import SendAllOptions, SendAllResult
from .download_queue import Job, JobResult, download_queue, new_batch_id
from .listing_walker import walk_listing

logger = logging.getLogger(__name__)


async def _collect_codes(*, kind: str, slug: str, options: SendAllOptions) -> list[str]:
    """Walk all pages of /{kind}/{slug} and return deduped codes."""
    items, _pages = await walk_listing(
        kind, slug, uncensored=options.uncensored,
        max_pages=max(1, options.max_pages),
    )
    return [it.code for it in items if it.code]


def _apply_result(summary: SendAllResult, result: JobResult) -> None:
    if result.status == "sent":
        summary.sent += 1
    elif result.status == "skipped_no_magnet":
        summary.skipped_no_magnet += 1
    elif result.status == "skipped_already_sent":
        summary.skipped_already_sent += 1
    elif result.status == "failed":
        summary.failed += 1
        if result.message:
            summary.errors.append(f"{result.code}: {result.message}")
    # "cancelled" → don't count (caller cancelled, surface only as the
    # missing delta in the total)


async def send_codes_stream(
    codes: list[str],
    options: SendAllOptions,
    *,
    on_sent: Callable[[str], Awaitable[None]] | None = None,
    source: str = "bulk",
) -> AsyncIterator[dict]:
    """Push ``codes`` into the global download queue and stream per-code
    results as they complete. The ``on_sent`` hook fires after each
    successful submission (e.g. to update CollectedMovie status)."""
    yield {
        "type": "start",
        "total": len(codes),
        "preview": codes[:8] + (["…"] if len(codes) > 8 else []),
    }

    if not codes:
        yield {"type": "done", "result": SendAllResult().model_dump()}
        return

    batch_id = new_batch_id()
    futures: list[asyncio.Future] = []
    for code in codes:
        job = Job(
            code=code,
            options=options,
            source=source,
            batch_id=batch_id,
            on_sent=on_sent,
        )
        futures.append(await download_queue.enqueue(job))

    summary = SendAllResult(total_movies=len(codes))
    done = 0
    try:
        for coro in asyncio.as_completed(futures):
            result: JobResult = await coro
            done += 1
            _apply_result(summary, result)
            yield {
                "type": "progress",
                "current": done,
                **result.to_event(),
            }
    except asyncio.CancelledError:
        # Caller (browser) went away — drop the rest of the batch.
        try:
            await download_queue.cancel_batch(batch_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_batch %s failed: %s", batch_id, exc)
        raise

    yield {"type": "done", "result": summary.model_dump()}


async def send_all_stream(
    kind: str, slug: str, options: SendAllOptions
) -> AsyncIterator[dict]:
    """Walk JavBus listing pages first, then stream queue progress."""
    codes = await _collect_codes(kind=kind, slug=slug, options=options)
    async for event in send_codes_stream(
        codes, options, source=f"bulk:{kind}:{slug}"
    ):
        yield event


async def send_all(kind: str, slug: str, options: SendAllOptions) -> SendAllResult:
    """Drain the stream and return the final summary (non-streaming
    callers)."""
    final = SendAllResult()
    async for event in send_all_stream(kind, slug, options):
        if event["type"] == "done":
            final = SendAllResult(**event["result"])
    return final
